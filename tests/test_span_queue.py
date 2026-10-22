import queue
import threading
import time
from unittest.mock import MagicMock

from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.span_queue import SpanQueue


def _configuration(queue_size=1):
    config = Configuration()
    config.queue_size = queue_size
    return config


def test_delivers_a_pushed_trace_via_the_client_on_its_background_worker():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_spans.side_effect = lambda trace_id, spans: delivered.put((trace_id, spans)) or True

    sq = SpanQueue(_configuration(), client)
    sq.push(trace_id="abc", spans=[{"span_id": "1"}])

    trace_id, spans = delivered.get(timeout=1)
    assert trace_id == "abc"
    assert spans == [{"span_id": "1"}]


def test_drops_a_trace_without_blocking_when_the_queue_is_already_full():
    never_returns = threading.Event()
    client = MagicMock()
    client.deliver_spans.side_effect = lambda trace_id, spans: never_returns.wait()

    sq = SpanQueue(_configuration(queue_size=1), client)

    assert sq.push(trace_id="1", spans=[]) is True

    deadline = time.monotonic() + 1
    second_push_result = True
    while time.monotonic() < deadline:
        second_push_result = sq.push(trace_id="2", spans=[])
        if not second_push_result:
            break

    assert second_push_result is False
    never_returns.set()  # let the worker thread exit cleanly


def test_recovers_from_the_client_raising_instead_of_returning():
    delivered = queue.Queue()
    call_count = {"n": 0}

    def deliver_spans(trace_id, spans):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        delivered.put(trace_id)
        return True

    client = MagicMock()
    client.deliver_spans.side_effect = deliver_spans

    sq = SpanQueue(_configuration(queue_size=10), client)

    sq.push(trace_id="1", spans=[])  # raises inside the worker loop; must not kill it
    sq.push(trace_id="2", spans=[])

    assert delivered.get(timeout=1) == "2"


def test_one_whole_trace_is_delivered_as_a_single_client_call_not_split_per_span():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_spans.side_effect = lambda trace_id, spans: delivered.put(spans) or True

    sq = SpanQueue(_configuration(queue_size=10), client)
    sq.push(trace_id="abc", spans=[{"span_id": "1"}, {"span_id": "2"}, {"span_id": "3"}])

    spans = delivered.get(timeout=1)
    assert len(spans) == 3
    assert client.deliver_spans.call_count == 1
