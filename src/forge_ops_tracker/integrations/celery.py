"""Celery integration. Call once, after your Celery app is created:

    from forge_ops_tracker.integrations.celery import init_celery
    init_celery()

Not imported by forge_ops_tracker/__init__.py: Celery is an optional dependency, so importing
this submodule specifically is what pulls it in, rather than every forge_ops_tracker user needing
Celery installed. Celery's own signals are global (not tied to one Celery app instance the way
Django middleware or Flask's init_flask(app) are), so this takes no arguments either.

Unlike Django/Flask, there's no existing error-reporting integration for Celery in this SDK to
extend: this one covers both error capture and performance timing together, the same bundling
Django/Flask's own integrations already do for their own frameworks.
"""

import threading
import time

from celery.signals import task_failure, task_postrun, task_prerun

from .. import (
    _end_breadcrumb_trail,
    _record_breadcrumb,
    _record_performance,
    _start_breadcrumb_trail,
    capture_exception,
)

# task_id -> start time. A plain dict keyed by task_id, not a threading.local: task_prerun and
# task_postrun for the same task_id are guaranteed to fire on whatever thread/process actually
# ran that task, but explicitly keying by task_id (rather than assuming "whichever thread called
# prerun last is the one that'll call postrun next") is what actually stays correct if Celery's
# own eager/testing mode ever runs tasks in an order this SDK doesn't control.
_start_times = {}
# task_id -> the contextvars.Token _start_breadcrumb_trail returned for that task's own run, same
# task-id-keyed reasoning as _start_times above, not a stack: a task dispatched synchronously
# from inside another task's own run (Celery's eager mode allows this) nests correctly either way
# since each token independently captures whatever the context var held right before that
# particular task started, but keying by task_id (rather than assuming call order) is what stays
# correct if Celery's own eager/testing mode ever pairs prerun/postrun in an order this SDK
# doesn't control, the exact same case _start_times above already guards against.
_breadcrumb_tokens = {}
_lock = threading.Lock()


def init_celery():
    task_prerun.connect(_handle_prerun, weak=False)
    task_postrun.connect(_handle_postrun, weak=False)
    task_failure.connect(_handle_failure, weak=False)


def _handle_prerun(sender=None, task_id=None, task=None, args=None, kwargs=None, **extra):
    with _lock:
        _start_times[task_id] = time.monotonic()
        # Gives this task its own fresh breadcrumb trail, same "own request/task, own trail"
        # scoping Django/Flask's own breadcrumb-context middleware apply, so a task run on a
        # reused worker thread never inherits a previous, unrelated task's own breadcrumbs.
        _breadcrumb_tokens[task_id] = _start_breadcrumb_trail()

    # Recorded here, at the very start, deliberately not in _handle_postrun below once the task's
    # own duration is known: confirmed directly against celery/app/trace.py that Celery's real
    # signal order is task_prerun -> the task body -> task_failure (if it raises) -> task_postrun,
    # always last. A breadcrumb only added once the task finishes would never make it into that
    # same task's own failure report, since task_failure (and this integration's own
    # _handle_failure, which is what actually reads the trail via capture_exception) has already
    # fired by then. Recording it immediately, before the task body even runs, guarantees it's
    # already present in the trail if this exact task run is the one that goes on to fail.
    if task is not None:
        _record_breadcrumb(task.name, category="job")


def _handle_postrun(sender=None, task_id=None, task=None, args=None, kwargs=None, retval=None, **extra):
    with _lock:
        start = _start_times.pop(task_id, None)
        token = _breadcrumb_tokens.pop(task_id, None)

    if start is not None:
        duration_ms = (time.monotonic() - start) * 1000
        _record_performance(task.name, duration_ms, kind="job")

    # Always cleared, whether or not a start time was found: the same "clear even when nothing
    # else fired correctly" defensiveness _end_breadcrumb_trail's own callers all apply elsewhere.
    _end_breadcrumb_trail(token)


def _handle_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, traceback=None, einfo=None, **extra):
    # sender is the Task instance itself here (see Celery's own task_failure.send call site,
    # confirmed directly rather than assumed), so sender.name is the same registered task name
    # task_postrun's own task.name is.
    capture_exception(exception, context={"task": sender.name if sender else None})
