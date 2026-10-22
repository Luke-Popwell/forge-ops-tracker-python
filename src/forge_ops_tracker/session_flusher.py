"""Counts requests and crashes in-process (see the Django/Flask session-tracking
integrations) and periodically flushes the totals as one small aggregate report, rather than
one network call per request. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/session_flusher.rb.

Same "lazily start a background thread on first use, not at load time" pattern DeliveryQueue
already uses, so each forked Gunicorn/uWSGI worker gets its own fresh thread instead of
inheriting a dead one across fork; here it's triggered by a sleep-loop timer instead of a
queue push, since there's no per-event signal to wait on, just a clock.
"""

import atexit
import threading
import time
from datetime import datetime, timezone

from .delivery_status import FlushOutcome


class SessionFlusher:
    def __init__(self, configuration, client):
        self._configuration = configuration
        self._client = client
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._sessions_count = 0
        self._crashed_sessions_count = 0
        self._period_started_at = _now()
        self._thread = None
        self._outcome = FlushOutcome(configuration, "session counts")

        # Flushes whatever's already tallied on a normal process exit, so the last partial
        # window (anything shorter than a full session_flush_interval) isn't silently dropped.
        atexit.register(self.flush)

    def record_session(self, crashed):
        self._ensure_worker_started()
        with self._lock:
            self._sessions_count += 1
            if crashed:
                self._crashed_sessions_count += 1

    def flush(self):
        """Snapshots and resets the in-process counters, then delivers them. A failed
        delivery keeps the counts where they are rather than resetting, so the next flush's
        window just grows instead of losing what was already tallied; there's no other copy
        of this data anywhere. Counts the server rejected, or that failed MAX_FAILED_FLUSHES
        (see delivery_status.py) flushes in a row, are reset all the same: sending them again
        could never succeed."""
        with self._lock:
            if self._sessions_count == 0:
                return
            snapshot = {
                "release": self._configuration.release,
                "environment": self._configuration.environment,
                "period_started_at": self._period_started_at,
                "period_ended_at": _now(),
                "sessions_count": self._sessions_count,
                "crashed_sessions_count": self._crashed_sessions_count,
            }

        if not self._outcome.release(self._client.deliver_session_checkin(snapshot)):
            return

        with self._lock:
            self._sessions_count = 0
            self._crashed_sessions_count = 0
            self._period_started_at = _now()

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
            time.sleep(self._configuration.session_flush_interval)
            try:
                self.flush()
            except Exception as e:  # noqa: BLE001 (the flush loop must never die from one bad flush)
                self._configuration.logger.debug("[forge_ops_tracker] session flush thread error: %s: %s", type(e).__name__, e)


def _now():
    return datetime.now(timezone.utc).isoformat()
