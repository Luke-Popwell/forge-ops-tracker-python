import queue
import threading

import pytest

import forge_ops_tracker
from forge_ops_tracker.delivery_queue import DeliveryQueue
from forge_ops_tracker.delivery_status import DELIVERED
from forge_ops_tracker.span_queue import SpanQueue

# The most a test's teardown waits for one background delivery worker to finish what it holds.
SETTLE_TIMEOUT = 5.0


class _SettledClient:
    """Stands in for a queue's own client once its test is over: whatever the worker delivers from
    then on goes nowhere, and only signals that the worker got that far."""

    def __init__(self, reached):
        self._reached = reached

    def __getattr__(self, name):
        def deliver(*args, **kwargs):
            self._reached.set()
            return DELIVERED

        return deliver


def _track_delivery_queues(monkeypatch):
    """Every DeliveryQueue/SpanQueue constructed while the test runs, whoever constructed it: the
    module-level ones behind forge_ops_tracker.init(), ones a mid-test _reset_for_testing() has
    already let go of, and ones a test builds directly."""
    created = []
    for queue_class in (DeliveryQueue, SpanQueue):
        original_init = queue_class.__init__

        def tracking_init(self, *args, _original_init=original_init, **kwargs):
            _original_init(self, *args, **kwargs)
            created.append(self)

        monkeypatch.setattr(queue_class, "__init__", tracking_init)
    return created


def _settle(delivery_queue):
    """Returns once delivery_queue's worker thread can no longer reach the network (or a later
    test's fake urlopen). Each of these queues has exactly one worker, delivering one entry at a
    time, so swapping in a client that only signals and then queueing one more entry is enough:
    the worker can only reach that client after it has finished the delivery it was in the middle
    of, and nothing it picks up afterward goes anywhere. Waiting on that signal, not on the queue
    looking empty, is what covers an entry the worker has already taken off the queue but not yet
    sent."""
    worker = delivery_queue._thread
    if worker is None or not worker.is_alive():
        return
    # A test that replaced this one queue's worker loop (test_delivery_queue.py parks it to test
    # drain on its own) has no worker taking anything off the queue, so nothing to wait for.
    if "_run" in vars(delivery_queue):
        return

    reached = threading.Event()
    delivery_queue._client = _SettledClient(reached)
    try:
        delivery_queue._queue.put_nowait({"trace_id": None, "spans": []})
    except queue.Full:
        # Already full of entries the worker will hand to the settled client, any one of which
        # sets the signal just the same.
        pass
    assert reached.wait(SETTLE_TIMEOUT), "a background delivery worker was still busy after the test finished"


@pytest.fixture(autouse=True)
def reset_forge_ops_tracker_state(monkeypatch):
    # Depending on monkeypatch here (even though this fixture doesn't use
    # it directly) forces teardown ordering: this fixture's finalizer runs
    # before monkeypatch's own restoration, so monkeypatch.setattr(sys,
    # "excepthook", ...) in a test always has the final word on restoring
    # the *real* sys.excepthook, not whatever this reset last set it to.
    forge_ops_tracker._reset_for_testing()
    # init() starts the startup change snapshot, which would post from a background thread in
    # every test that initializes an enabled client (and land in whatever urlopen that test
    # captures). Only test_changes.py exercises the real thing; everything else gets a no-op.
    monkeypatch.setattr(forge_ops_tracker, "_start_change_snapshot", lambda configuration: False)
    delivery_queues = _track_delivery_queues(monkeypatch)
    yield
    # An errored request queues an error event and a trace, each sent by its own worker thread,
    # and a test rarely reads both. Client looks urlopen up at call time, so a delivery still on
    # its way when the test ends would land in the next test's fake urlopen as if that test had
    # sent it. The same teardown ordering as above keeps this test's own fake in place until
    # every worker is done.
    try:
        for delivery_queue in delivery_queues:
            _settle(delivery_queue)
    finally:
        forge_ops_tracker._reset_for_testing()
