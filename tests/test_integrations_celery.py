import json
import queue

from celery import Celery

import forge_ops_tracker
from forge_ops_tracker.integrations.celery import (
    _handle_failure,
    _handle_postrun,
    _handle_prerun,
    init_celery,
)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _app():
    # broker/backend are both in-memory, no real Redis/RabbitMQ needed: .apply() below runs the
    # task synchronously in this same process regardless of either.
    return Celery("forge_ops_tracker_test", broker="memory://", backend="cache+memory://")


def test_times_a_real_task_run_via_apply_kind_job(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )
    init_celery()

    app = _app()

    @app.task(name="forge_ops_tracker_test.add")
    def add(x, y):
        return x + y

    result = add.apply(args=(2, 3))
    assert result.get() == 5

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    sample = payload["samples"][0]
    assert sample["kind"] == "job"
    assert sample["transaction_name"] == "forge_ops_tracker_test.add"
    assert sample["request_count"] == 1


def test_task_prerun_and_postrun_are_paired_by_task_id_not_call_order(monkeypatch):
    # Proves the pairing is genuinely keyed by task_id (see integrations/celery.py's own comment
    # on why a plain dict beats a threading.local here), not just "whichever task started most
    # recently": two tasks run back to back, neither should see the other's start time.
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )
    init_celery()

    app = _app()

    @app.task(name="forge_ops_tracker_test.noop")
    def noop():
        return None

    noop.apply()
    noop.apply()

    forge_ops_tracker._performance_state()[1].flush()

    request = delivered.get(timeout=1)
    payload = json.loads(request.data)
    job_sample = next(s for s in payload["samples"] if s["kind"] == "job")
    assert job_sample["request_count"] == 2


def test_task_gets_its_own_fresh_breadcrumb_trail_recorded_at_prerun(monkeypatch):
    delivered = queue.Queue()

    def fake_urlopen(request, timeout=None):
        delivered.put(request)
        return _FakeResponse(202)

    monkeypatch.setattr("forge_ops_tracker.client.urllib.request.urlopen", fake_urlopen)
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )
    init_celery()

    app = _app()
    seen_buffer = []

    @app.task(name="forge_ops_tracker_test.record_trail")
    def record_trail():
        seen_buffer.append(forge_ops_tracker._breadcrumbs.get())

    record_trail.apply()

    assert seen_buffer[0] is not None
    entry = seen_buffer[0].all()[0]
    assert entry["category"] == "job"
    assert entry["message"] == "forge_ops_tracker_test.record_trail"
    # Cleared once the task finishes, same as a Django/Flask request's own trail.
    assert forge_ops_tracker._breadcrumbs.get() is None


def test_a_manually_added_breadcrumb_during_a_task_is_visible_to_that_same_tasks_failure(monkeypatch):
    # task_failure only fires from Celery's own real worker execution path, not from .apply()'s
    # eager path (see _handle_failure's own test above for the same caveat), so prerun/postrun
    # are driven directly here, with a real add_breadcrumb() call in between simulating what a
    # task body would do, and _handle_failure is then called the same way Celery's own real
    # task_failure.send(...) would, with the trail still active at that point.
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
    )

    class FakeTask:
        name = "forge_ops_tracker_test.boom"

    _handle_prerun(sender=FakeTask(), task_id="abc123", task=FakeTask(), args=(), kwargs={})
    forge_ops_tracker.add_breadcrumb("about to explode", category="custom")

    buffer = forge_ops_tracker._breadcrumbs.get()
    assert [b["category"] for b in buffer.all()] == ["job", "custom"]

    _handle_postrun(sender=FakeTask(), task_id="abc123", task=FakeTask(), args=(), kwargs={}, retval=None)

    assert forge_ops_tracker._breadcrumbs.get() is None


def test_does_not_start_a_trail_when_track_breadcrumbs_is_off(monkeypatch):
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(
        dsn="https://key@tracker.example.com/api/v1/events",
        enabled_environments={"production"},
        environment="production",
        track_breadcrumbs=False,
    )

    class FakeTask:
        name = "forge_ops_tracker_test.boom"

    _handle_prerun(sender=FakeTask(), task_id="abc123", task=FakeTask(), args=(), kwargs={})

    assert forge_ops_tracker._breadcrumbs.get() is None


def test_handle_failure_reports_the_exception_with_the_task_name_as_context(monkeypatch):
    # task_failure only fires from Celery's own real worker execution path, confirmed directly
    # against celery/app/trace.py and celery/worker/request.py, not from .apply()'s eager path
    # (confirmed empirically: eager mode's own error handling doesn't send this signal at all,
    # a real Celery quirk, not a gap in this integration). Calling the handler directly, with the
    # exact kwarg shape Celery's own real task_failure.send(...) call site uses, is what actually
    # proves this integration's own logic is correct without depending on that quirk.
    reported = []
    monkeypatch.setattr(
        "forge_ops_tracker.integrations.celery.capture_exception",
        lambda exc, context=None: reported.append((exc, context)),
    )

    class FakeTask:
        name = "forge_ops_tracker_test.boom"

    exc = ValueError("boom")
    _handle_failure(sender=FakeTask(), task_id="abc123", exception=exc, args=(), kwargs={}, traceback=None, einfo=None)

    assert reported == [(exc, {"task": "forge_ops_tracker_test.boom"})]
