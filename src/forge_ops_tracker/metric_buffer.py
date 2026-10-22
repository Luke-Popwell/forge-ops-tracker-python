"""Collects individual `capture_metric`/`capture_infrastructure_metric` calls in-process and
periodically flushes them as one batch, rather than one network call per capture. Unlike
performance_flusher.py this keeps a *list* of individually meaningful entries instead of summing
them into buckets: a customer's own signup or payment is exactly the kind of thing they will want
a genuinely accurate count/sum of later, so the server stores one row per entry as-is. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/metric_buffer.rb and infrastructure_metric_buffer.rb,
which are the same class twice; here it is one class instantiated twice, told which delivery method
and flush interval to use.

Three deliberate differences from the Ruby buffers:

- A flush snapshots the first N entries and, on success, removes exactly those N, instead of
  resetting the whole list. The Ruby version's own comment accepts that an entry recorded while the
  HTTP request is in flight is lost; here it is kept for the next flush.
- The buffer is capped at MAX_ENTRIES, and once full further entries are dropped until a flush
  lets go of them: an unreachable server fails every flush, and an uncapped buffer would keep
  growing until the batch is given up on (see FlushOutcome in delivery_status.py). Dropping the newest rather than the oldest keeps the
  entries a flush is delivering at the front of the list, which is what makes removing exactly
  those afterward exact.
- A NaN or infinite value is dropped at record time: `json.dumps` would emit invalid JSON for it
  and the server would reject the whole batch behind it.
"""

import atexit
import math
import threading
import time
from datetime import datetime, timezone

from .delivery_status import FlushOutcome

MAX_ENTRIES = 1000


class MetricBuffer:
    def __init__(self, configuration, deliver, interval):
        """`deliver` takes a list of entries and returns a delivery status (see delivery_status.py;
        a plain True/False is still read as delivered/failed for now); `interval` is
        a callable returning the current flush interval in seconds (read fresh each cycle, so a
        configuration change takes effect)."""
        self._configuration = configuration
        self._deliver = deliver
        self._interval = interval
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._entries = []
        self._thread = None
        self._outcome = FlushOutcome(configuration, "metrics")

        # Flushes whatever is still buffered on a normal process exit: the real use of the
        # infrastructure endpoint is a short-lived cron/script that captures a few readings and exits.
        atexit.register(self.flush)

    def record(self, entry):
        """Adds one entry (a dict, already carrying everything but `recorded_at`); returns False if it was dropped."""
        value = entry.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self._configuration.logger.debug("[forge_ops_tracker] dropped a metric with a non-numeric or non-finite value")
            return False

        self._ensure_worker_started()
        with self._lock:
            if len(self._entries) >= MAX_ENTRIES:
                self._configuration.logger.debug("[forge_ops_tracker] metric buffer full, dropping a metric")
                return False
            self._entries.append({**entry, "recorded_at": _now()})
        return True

    def flush(self):
        """Delivers everything buffered so far as one batch. A delivery that failed for now keeps
        every entry, so the next flush's batch just grows; a batch the server rejected, or one that
        failed MAX_FAILED_FLUSHES (see delivery_status.py) flushes in a row, is dropped."""
        with self._lock:
            if not self._entries:
                return
            snapshot = list(self._entries)

        if not self._outcome.release(self._deliver(snapshot)):
            return

        with self._lock:
            # Exactly the entries just sent: anything recorded while the request was in
            # flight sits after them and stays for the next flush.
            del self._entries[: len(snapshot)]

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
            time.sleep(self._interval())
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 (the flush loop must never die from one bad flush)
                self._configuration.logger.debug("[forge_ops_tracker] metric flush thread error: %s: %s", type(e).__name__, e)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
