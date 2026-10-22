import atexit
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import MagicMock

from forge_ops_tracker import delivery_status
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.delivery_queue import DeliveryQueue


def _configuration(queue_size=1):
    config = Configuration()
    config.queue_size = queue_size
    return config


def test_delivers_a_pushed_payload_via_the_client_on_its_background_worker():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver.side_effect = lambda payload: delivered.put(payload) or True

    dq = DeliveryQueue(_configuration(), client)
    dq.push({"exception_class": "RuntimeError"})

    result = delivered.get(timeout=1)
    assert result == {"exception_class": "RuntimeError"}


def test_drops_a_payload_without_blocking_when_the_queue_is_already_full():
    # A client whose delivery blocks forever, so nothing drains the queue
    # and capacity stays exactly at queue_size (1) for a deterministic
    # full-queue test.
    never_returns = threading.Event()
    client = MagicMock()
    client.deliver.side_effect = lambda payload: never_returns.wait()

    dq = DeliveryQueue(_configuration(queue_size=1), client)

    assert dq.push({"n": 1}) is True

    # The first push may already have been picked up by the worker
    # (draining the queue before it observes "full"), so retry briefly
    # rather than asserting on a single racy attempt.
    deadline = time.monotonic() + 1
    second_push_result = True
    while time.monotonic() < deadline:
        second_push_result = dq.push({"n": 2})
        if not second_push_result:
            break

    assert second_push_result is False
    never_returns.set()  # let the worker thread exit cleanly


def test_sends_a_payload_once_and_says_why_it_was_dropped_whatever_the_client_answers(caplog):
    config = _configuration(queue_size=10)
    outcomes = [delivery_status.REJECTED, delivery_status.FAILED, delivery_status.DELIVERED]
    done = threading.Event()
    client = MagicMock()

    def deliver(payload):
        outcome = outcomes.pop(0)
        if not outcomes:
            done.set()
        return outcome

    client.deliver.side_effect = deliver

    with caplog.at_level("DEBUG", logger=config.logger.name):
        delivery_queue = DeliveryQueue(config, client)
        for n in range(3):
            delivery_queue.push({"n": n})
        assert done.wait(timeout=1)
        delivery_queue.drain(timeout=0.5)

    assert client.deliver.call_count == 3
    dropped = [r.getMessage() for r in caplog.records if "dropping event" in r.getMessage()]
    assert dropped == [
        "[forge_ops_tracker] dropping event: the server rejected it and will not accept a retry",
        "[forge_ops_tracker] dropping event: delivery failed",
    ]


def test_recovers_from_the_client_raising_instead_of_returning():
    delivered = queue.Queue()
    call_count = {"n": 0}

    def deliver(payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        delivered.put(payload)
        return True

    client = MagicMock()
    client.deliver.side_effect = deliver

    # A larger queue than the other tests, so both pushes below can be
    # enqueued regardless of exactly when the worker gets scheduled.
    dq = DeliveryQueue(_configuration(queue_size=10), client)

    dq.push({"n": 1})  # raises inside the worker loop; must not kill it
    dq.push({"n": 2})

    result = delivered.get(timeout=1)
    assert result == {"n": 2}


# On process exit. The child-process tests run a real interpreter, the only way to see what atexit
# actually does; each holds the worker thread back (its _run just waits forever), the way a
# process exiting right after reporting outruns it, so an event can only arrive through drain.

_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

_CHILD_PREAMBLE = """
import json
import threading
import forge_ops_tracker
from forge_ops_tracker.client import Client
from forge_ops_tracker.delivery_queue import DeliveryQueue

def deliver(self, payload):
    with open({out!r}, "a") as f:
        f.write(json.dumps(payload) + "\\n")
    return True

Client.deliver = deliver
DeliveryQueue._run = lambda self: threading.Event().wait()
forge_ops_tracker.init(
    dsn="https://key@tracker.example.com/api/v1/events",
    enabled_environments={{"production"}},
    environment="production",
    detect_changes=False,
)
"""


def _run_child(tmp_path, body):
    out = tmp_path / "delivered.jsonl"
    script = _CHILD_PREAMBLE.format(out=str(out)) + textwrap.dedent(body)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH")])))
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30, check=False)
    delivered = [json.loads(line) for line in out.read_text().splitlines()] if out.exists() else []
    return result, delivered


def test_sends_what_is_still_queued_before_the_process_ends(tmp_path):
    result, delivered = _run_child(
        tmp_path,
        """
        try:
            raise RuntimeError("reported just before exit")
        except RuntimeError:
            forge_ops_tracker.capture_exception()
        """,
    )

    assert result.returncode == 0, result.stderr
    assert [event["message"] for event in delivered] == ["reported just before exit"]


def test_sends_an_uncaught_crash_before_the_process_dies(tmp_path):
    result, delivered = _run_child(tmp_path, 'raise RuntimeError("crashed the script")\n')

    assert result.returncode == 1
    assert "crashed the script" in result.stderr  # the original excepthook still printed it
    assert [event["message"] for event in delivered] == ["crashed the script"]


def test_drain_delivers_whatever_is_still_queued():
    client = MagicMock()
    dq = DeliveryQueue(_configuration(queue_size=10), client)
    dq._run = lambda: threading.Event().wait()  # hold the worker back
    dq.push({"n": 1})
    dq.push({"n": 2})

    dq.drain()

    assert [c.args[0] for c in client.deliver.call_args_list] == [{"n": 1}, {"n": 2}]


def test_drain_gives_up_at_the_timeout_rather_than_holding_the_process_open():
    client = MagicMock()
    client.deliver.side_effect = lambda payload: time.sleep(0.5)
    dq = DeliveryQueue(_configuration(queue_size=10), client)
    dq._run = lambda: threading.Event().wait()
    for n in range(3):
        dq.push({"n": n})

    started = time.monotonic()
    dq.drain(timeout=0.2)

    assert time.monotonic() - started < 1.0
    assert client.deliver.call_count == 1


def test_drain_waits_for_a_delivery_already_under_way():
    finished = threading.Event()
    client = MagicMock()
    client.deliver.side_effect = lambda payload: time.sleep(0.2) or finished.set()
    dq = DeliveryQueue(_configuration(), client)
    dq.push({"n": 1})
    deadline = time.monotonic() + 1
    while dq._in_flight == 0 and time.monotonic() < deadline:
        time.sleep(0.005)

    dq.drain()

    assert finished.is_set()


def test_drain_waits_for_nothing_when_nothing_was_ever_reported(monkeypatch):
    registered = []
    monkeypatch.setattr(atexit, "register", registered.append)
    dq = DeliveryQueue(_configuration(), MagicMock())

    started = time.monotonic()
    dq.drain()

    assert time.monotonic() - started < 0.1
    assert registered == []  # and it isn't hooked into exit until its first push
