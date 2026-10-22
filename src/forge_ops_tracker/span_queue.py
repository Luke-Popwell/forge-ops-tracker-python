"""A small in-process background thread + bounded queue, so delivering a captured trace never
adds latency to the very request it was just measuring: the one thing that would be actively
counterproductive here, blocking an already-slow request even longer just to report that it was
slow. Ported from gems/forge_ops_tracker/lib/forge_ops_tracker/span_queue.rb.

A separate queue from DeliveryQueue (not reused): one whole trace (a trace_id plus every span
belonging to it) is one queue entry, delivered as one client call, never batched together with
other traces the way DeliveryQueue's own events, or PerformanceFlusher's own samples, get batched
over a time window. A trace is already a complete, immediately relevant unit the moment its own
request finishes, so it's delivered promptly rather than held for up to a full flush interval.
"""

import queue
import threading


class SpanQueue:
    def __init__(self, configuration, client):
        self._configuration = configuration
        self._client = client
        self._queue = queue.Queue(maxsize=max(1, configuration.queue_size))
        self._thread = None
        self._start_lock = threading.Lock()

    def push(self, trace_id, spans):
        """Dropping a whole trace silently (never blocking the caller) if the queue is already
        full, the same backpressure-must-never-reach-the-host-app posture DeliveryQueue.push
        already documents: a burst of several slow requests at once must never stall a request
        that's already been identified as slow even further."""
        self._ensure_worker_started()
        try:
            self._queue.put_nowait({"trace_id": trace_id, "spans": spans})
            return True
        except queue.Full:
            self._configuration.logger.debug("[forge_ops_tracker] span delivery queue full, dropping trace")
            return False

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
            entry = self._queue.get()
            try:
                self._client.deliver_spans(entry["trace_id"], entry["spans"])
            except Exception as e:  # noqa: BLE001 (one bad delivery must not kill the worker for every trace after it)
                self._configuration.logger.debug("[forge_ops_tracker] span delivery worker error: %s: %s", type(e).__name__, e)
