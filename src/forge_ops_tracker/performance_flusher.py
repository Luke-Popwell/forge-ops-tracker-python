"""Times requests/jobs/queries in-process, bucketed by (transaction_name, kind) (see the Django/
Flask/Celery/SQLAlchemy performance-tracking integrations), and periodically flushes each distinct
bucket as one small aggregate report, rather than one network call per event. One shared flusher
for every kind, not one per kind: the aggregation shape (count + duration sum + max over a period)
is identical regardless of what's actually being timed. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/performance_flusher.rb; structurally the same shape
as session_flusher.py right next to it, just keyed by a dict of buckets instead of two scalar
counters.
"""

import atexit
import threading
import time
from datetime import datetime, timezone

from . import strict_privacy
from .delivery_status import FlushOutcome
from .histogram_bucketer import bucket_for


class PerformanceFlusher:
    def __init__(self, configuration, client):
        self._configuration = configuration
        self._client = client
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._buckets = {}
        self._period_started_at = _now()
        self._thread = None
        self._outcome = FlushOutcome(configuration, "performance samples")

        # Flushes whatever's already buffered on a normal process exit, same reasoning
        # session_flusher.py's own atexit registration documents.
        atexit.register(self.flush)

    def record(self, transaction_name, duration_ms, kind="controller"):
        self._ensure_worker_started()
        # Under strict privacy a raw-path name (a request no route matched) keeps only its first
        # segment; a route is kept.
        if self._configuration.strict_privacy:
            transaction_name = strict_privacy.transaction_name(transaction_name)
        with self._lock:
            bucket = self._buckets.setdefault(
                (transaction_name, kind),
                {"count": 0, "duration_sum_ms": 0.0, "max_duration_ms": 0.0, "histogram": {}},
            )
            bucket["count"] += 1
            bucket["duration_sum_ms"] += duration_ms
            bucket["max_duration_ms"] = max(bucket["max_duration_ms"], duration_ms)
            # The distribution count/sum/max can't reconstruct: see histogram_bucketer.py for why
            # the server approximates a percentile from these bucket counts.
            label = bucket_for(duration_ms)
            bucket["histogram"][label] = bucket["histogram"].get(label, 0) + 1

    def flush(self):
        """Snapshots the buffered buckets, then delivers them as one batch. A failed delivery
        keeps every bucket where it is rather than resetting, so the next flush's batch just
        grows instead of losing what was already tallied; same reasoning session_flusher.py's
        own flush() documents. That only holds for a delivery that failed for now, and only
        MAX_FAILED_FLUSHES (see delivery_status.py) times in a row: a batch the server rejected,
        or one that keeps failing, is removed exactly the way a delivered one is.

        Only exactly what this snapshot delivered is removed afterward, subtracted from whatever
        is in each bucket by then, never the whole dict reset: record() can run on another thread
        while delivery is in flight (the lock is released around the network call), so a record
        for a bucket already in the snapshot, or a brand-new one, can land between the snapshot
        and delivery succeeding, and resetting afterward would silently discard it.
        max_duration_ms is left as whatever is currently on the bucket, sent or not: a max can't
        be "subtracted" back out, and leaving it never overstates the next period's own max."""
        with self._lock:
            if not self._buckets:
                return
            period_ended_at = _now()
            sent = {
                key: {
                    "count": bucket["count"],
                    "duration_sum_ms": bucket["duration_sum_ms"],
                    "histogram": dict(bucket["histogram"]),
                }
                for key, bucket in self._buckets.items()
            }
            samples = [
                {
                    "transaction_name": transaction_name,
                    "kind": kind,
                    "environment": self._configuration.environment,
                    "release": self._configuration.release,
                    "period_started_at": self._period_started_at,
                    "period_ended_at": period_ended_at,
                    "request_count": bucket["count"],
                    "duration_sum_ms": bucket["duration_sum_ms"],
                    "max_duration_ms": bucket["max_duration_ms"],
                    "histogram": dict(bucket["histogram"]),
                }
                for (transaction_name, kind), bucket in self._buckets.items()
            ]

        if not self._outcome.release(self._client.deliver_performance_samples(samples)):
            return

        with self._lock:
            for key, delivered in sent.items():
                current = self._buckets.get(key)
                if current is None:
                    continue
                current["count"] -= delivered["count"]
                current["duration_sum_ms"] = max(current["duration_sum_ms"] - delivered["duration_sum_ms"], 0.0)
                for label, count in delivered["histogram"].items():
                    remaining = current["histogram"].get(label, 0) - count
                    if remaining > 0:
                        current["histogram"][label] = remaining
                    else:
                        current["histogram"].pop(label, None)
                if current["count"] <= 0:
                    del self._buckets[key]
            self._period_started_at = period_ended_at

    def _ensure_worker_started(self):
        if self._thread is not None and self._thread.is_alive():
            return

        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            time.sleep(self._configuration.performance_flush_interval)
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 (the flush loop must never die from one bad flush)
                self._configuration.logger.debug("[forge_ops_tracker] performance flush thread error: %s: %s", type(e).__name__, e)


def _now():
    return datetime.now(timezone.utc).isoformat()
