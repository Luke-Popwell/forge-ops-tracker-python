"""A small in-process background thread + bounded queue, so delivery
never blocks the caller that raised the error and never depends on the
host app having any particular job backend configured. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/delivery_queue.rb.

The worker thread is started lazily, on first push, not at import/
construction time: deliberately mirroring the Ruby gem rather than the
.NET SDK's eager start. Gunicorn (prefork) and uWSGI commonly fork worker
processes *after* the application (and this module) has already loaded,
which would leave an eagerly-started thread dead in every forked child:
the exact hazard the Ruby gem's own lazy start avoids for Puma. Starting
fresh on first push means each forked worker gets its own live thread
regardless of when it was forked relative to import time.

Anything still queued when the interpreter exits is sent by an atexit hook (see drain), so a
short-lived process (a script, a cron job, a management command) doesn't lose the errors it
reported in its last moments.
"""

import atexit
import queue
import threading
import time

from .delivery_status import FAILED, REJECTED, from_result

# The most a normal process exit waits for queued events to go out (see drain): long enough for
# one delivery at the default Configuration.timeout, short enough that an unreachable ForgeOps
# never holds up a deploy or a cron run for long.
DRAIN_TIMEOUT = 5.0


class DeliveryQueue:
    def __init__(self, configuration, client, deliver_with="deliver"):
        """deliver_with names the Client method each payload goes through: "deliver" (error
        events) by default, "deliver_change" for record_change()'s own queue."""
        self._configuration = configuration
        self._client = client
        self._deliver_with = deliver_with
        self._queue = queue.Queue(maxsize=max(1, configuration.queue_size))
        self._thread = None
        self._start_lock = threading.Lock()
        self._in_flight = 0
        self._in_flight_lock = threading.Lock()
        self._drain_registered = False

    def drain(self, timeout=DRAIN_TIMEOUT):
        """Sends whatever is still queued, and lets a delivery already under way finish, before
        the process exits, within timeout seconds. Without it a short-lived process lost every
        error it reported in its last moments: the daemon worker thread dies with the interpreter
        before it gets to them. Delivers here on the exiting thread rather than waiting on the
        worker, which in a forked child is a parent's thread that no longer runs."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                try:
                    payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._deliver(payload)
            # Only a live worker can finish what it holds; one inherited across a fork never will.
            while self._in_flight > 0 and self._worker_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
        except Exception as e:  # noqa: BLE001 (an exit hook must never turn a clean exit into a traceback)
            self._configuration.logger.debug("[forge_ops_tracker] drain failed: %s: %s", type(e).__name__, e)

    def push(self, payload):
        self._ensure_worker_started()
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self._configuration.logger.debug("[forge_ops_tracker] delivery queue full, dropping event")
            return False

    def _worker_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _ensure_worker_started(self):
        self._register_drain()
        if self._worker_alive():
            return

        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            payload = self._queue.get()
            with self._in_flight_lock:
                self._in_flight += 1
            try:
                self._deliver(payload)
            except Exception as e:  # noqa: BLE001 (per-item, so one bad delivery can't kill the worker for every event after it)
                # Per-item, not wrapping the whole loop: one bad delivery
                # must not kill the worker for every event after it.
                self._configuration.logger.debug("[forge_ops_tracker] delivery worker error: %s: %s", type(e).__name__, e)
            finally:
                with self._in_flight_lock:
                    self._in_flight -= 1

    def _deliver(self, payload):
        # One attempt per payload, as it has always been: a queued event is never held back for a
        # retry, whatever the outcome, so all that is left to do with a refusal is to say so once.
        status = from_result(getattr(self._client, self._deliver_with)(payload))
        if status is REJECTED:
            self._configuration.logger.debug("[forge_ops_tracker] dropping event: the server rejected it and will not accept a retry")
        elif status is FAILED:
            self._configuration.logger.debug("[forge_ops_tracker] dropping event: delivery failed")

    def _register_drain(self):
        # Registered on the first push, not at construction, so a process that never reports
        # anything never waits on exit at all.
        if self._drain_registered:
            return

        with self._start_lock:
            if self._drain_registered:
                return
            self._drain_registered = True
            atexit.register(self.drain)
