import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import forge_ops_tracker
from forge_ops_tracker import delivery_status
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.metric_buffer import MAX_ENTRIES, MetricBuffer


def _configuration():
    config = Configuration()
    config.dsn = "https://key@tracker.example.com/api/v1/events"
    config.environment = "production"
    config.release = "1.2.3"
    config.enabled_environments = ["production"]
    return config


def _buffer(deliver, config=None):
    return MetricBuffer(config or _configuration(), deliver, lambda: 3600)


def test_delivers_every_entry_as_one_batch_stamped_with_recorded_at():
    delivered = []
    buffer = _buffer(lambda entries: delivered.append(entries) or True)

    buffer.record({"metric_name": "signup", "value": 1.0})
    buffer.record({"metric_name": "payment", "value": 49.5})
    buffer.flush()

    assert len(delivered) == 1
    assert [e["metric_name"] for e in delivered[0]] == ["signup", "payment"]
    assert delivered[0][0]["recorded_at"].endswith("Z") and len(delivered[0][0]["recorded_at"]) == 20


def test_a_negative_value_is_kept_since_a_refund_is_a_real_metric():
    delivered = []
    buffer = _buffer(lambda entries: delivered.append(entries) or True)
    buffer.record({"metric_name": "refund", "value": -12.0})
    buffer.flush()
    assert delivered[0][0]["value"] == -12.0


def test_drops_nan_infinite_and_non_numeric_values_so_one_bad_entry_cannot_poison_a_batch():
    buffer = _buffer(lambda entries: True)
    assert buffer.record({"metric_name": "a", "value": float("nan")}) is False
    assert buffer.record({"metric_name": "b", "value": float("inf")}) is False
    assert buffer.record({"metric_name": "c", "value": "12"}) is False
    assert buffer.record({"metric_name": "d", "value": True}) is False
    assert buffer.record({"metric_name": "e", "value": 3}) is True


def test_a_failed_delivery_keeps_every_entry_for_the_next_flush():
    outcomes = iter([False, True])
    delivered = []

    def deliver(entries):
        ok = next(outcomes)
        delivered.append(list(entries))
        return ok

    buffer = _buffer(deliver)
    buffer.record({"metric_name": "a", "value": 1})
    buffer.flush()
    buffer.record({"metric_name": "b", "value": 2})
    buffer.flush()

    assert [e["metric_name"] for e in delivered[1]] == ["a", "b"]
    buffer.flush()  # nothing left: no third delivery
    assert len(delivered) == 2


def test_a_batch_the_server_rejected_is_dropped_and_never_sent_again():
    delivered = []

    def deliver(entries):
        delivered.append([e["metric_name"] for e in entries])
        return delivery_status.REJECTED

    buffer = _buffer(deliver)
    buffer.record({"metric_name": "a", "value": 1})
    buffer.flush()
    buffer.record({"metric_name": "b", "value": 2})
    buffer.flush()
    buffer.flush()

    assert delivered == [["a"], ["b"]]


def test_a_batch_is_kept_through_four_failed_flushes_and_dropped_on_the_fifth():
    delivered = []

    def deliver(entries):
        delivered.append([e["metric_name"] for e in entries])
        return delivery_status.FAILED

    buffer = _buffer(deliver)
    buffer.record({"metric_name": "a", "value": 1})
    for _ in range(5):
        buffer.flush()
    buffer.flush()  # nothing left: the fifth failed flush dropped the batch

    assert delivered == [["a"]] * 5


def test_a_flush_that_gets_through_starts_the_count_of_failed_flushes_again():
    outcomes = [delivery_status.FAILED] * 4 + [delivery_status.DELIVERED] + [delivery_status.FAILED] * 4
    delivered = []

    def deliver(entries):
        delivered.append([e["metric_name"] for e in entries])
        return outcomes.pop(0) if outcomes else delivery_status.FAILED

    buffer = _buffer(deliver)
    buffer.record({"metric_name": "a", "value": 1})
    for _ in range(5):
        buffer.flush()
    buffer.record({"metric_name": "b", "value": 2})
    for _ in range(4):
        buffer.flush()

    # Four more failures after the success, so "b" is still held, not dropped.
    assert delivered == [["a"]] * 5 + [["b"]] * 4
    assert buffer._entries


def test_a_dropped_batch_is_logged_once_with_what_and_why(caplog):
    config = _configuration()
    buffer = _buffer(lambda entries: delivery_status.REJECTED, config)
    buffer.record({"metric_name": "a", "value": 1})

    with caplog.at_level("DEBUG", logger=config.logger.name):
        buffer.flush()
        buffer.flush()

    dropped = [r.getMessage() for r in caplog.records if "dropping buffered" in r.getMessage()]
    assert dropped == ["[forge_ops_tracker] dropping buffered metrics: the server rejected them and will not accept a retry"]


def test_an_entry_recorded_while_delivery_is_in_flight_is_never_lost():
    in_flight = threading.Event()
    release = threading.Event()
    delivered = []

    def deliver(entries):
        delivered.append(list(entries))
        in_flight.set()
        release.wait(5)
        return True

    buffer = _buffer(deliver)
    buffer.record({"metric_name": "first", "value": 1})
    flushing = threading.Thread(target=buffer.flush)
    flushing.start()
    assert in_flight.wait(5)

    buffer.record({"metric_name": "during", "value": 2})
    release.set()
    flushing.join(5)

    buffer.flush()
    assert [e["metric_name"] for e in delivered[0]] == ["first"]
    assert [e["metric_name"] for e in delivered[1]] == ["during"]


def test_the_buffer_is_capped_and_drops_further_entries_until_a_flush_succeeds():
    buffer = _buffer(lambda entries: False)
    accepted = sum(buffer.record({"metric_name": "m", "value": 1}) for _ in range(MAX_ENTRIES + 50))
    assert accepted == MAX_ENTRIES


_REQUESTS = []


class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _REQUESTS.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve():
    _REQUESTS.clear()
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_capture_metric_and_capture_infrastructure_metric_deliver_to_their_own_endpoints_end_to_end():
    server = _serve()
    try:
        forge_ops_tracker._reset_for_testing()
        forge_ops_tracker.init(
            dsn=f"http://key@127.0.0.1:{server.server_port}/api/v1/events",
            environment="production",
            release="a1b2c3d",
            enabled_environments=["production"],
        )
        forge_ops_tracker.capture_metric("signup")
        forge_ops_tracker.capture_metric("payment", 49.0)
        forge_ops_tracker.capture_infrastructure_metric("cpu", 0.42, hostname="web-1")
        forge_ops_tracker.capture_infrastructure_metric("memory", 0.7)
        forge_ops_tracker.flush_metrics()

        by_path = {path: (auth, body) for path, auth, body in _REQUESTS}
        auth, custom = by_path["/api/v1/custom_metrics"]
        assert auth == "Bearer key"
        assert [(m["metric_name"], m["value"]) for m in custom["metrics"]] == [("signup", 1.0), ("payment", 49.0)]
        assert custom["metrics"][0]["environment"] == "production"
        assert custom["metrics"][0]["release"] == "a1b2c3d"
        _, infrastructure = by_path["/api/v1/infrastructure_metrics"]
        assert infrastructure["metrics"][0] == {
            "metric_name": "cpu",
            "value": 0.42,
            "hostname": "web-1",
            "recorded_at": infrastructure["metrics"][0]["recorded_at"],
        }
        assert infrastructure["metrics"][1]["hostname"] == forge_ops_tracker._state()[0].server_name
    finally:
        server.shutdown()
        forge_ops_tracker._reset_for_testing()


def test_capture_metric_is_a_no_op_when_the_client_is_not_enabled():
    forge_ops_tracker._reset_for_testing()
    forge_ops_tracker.init(dsn="https://key@tracker.example.com/api/v1/events", environment="development")
    forge_ops_tracker.capture_metric("signup")
    forge_ops_tracker.capture_infrastructure_metric("cpu", 1.0)
    _, metrics, infrastructure = forge_ops_tracker._metric_buffers()
    assert metrics._entries == []
    assert infrastructure._entries == []
    forge_ops_tracker._reset_for_testing()


def test_metric_uris_swap_the_trailing_events_segment():
    config = _configuration()
    assert config.custom_metrics_uri() == "https://tracker.example.com/api/v1/custom_metrics"
    assert config.infrastructure_metrics_uri() == "https://tracker.example.com/api/v1/infrastructure_metrics"


def test_the_client_wraps_entries_as_a_metrics_batch():
    client = MagicMock()
    buffer = MetricBuffer(_configuration(), client.deliver_metrics, lambda: 3600)
    client.deliver_metrics.return_value = True
    buffer.record({"metric_name": "a", "value": 1})
    buffer.flush()
    client.deliver_metrics.assert_called_once()
