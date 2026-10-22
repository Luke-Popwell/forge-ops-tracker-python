import queue
from unittest.mock import MagicMock

from forge_ops_tracker import delivery_status
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.session_flusher import SessionFlusher


def _configuration():
    config = Configuration()
    config.release = "1.2.3"
    config.environment = "production"
    return config


def test_aggregates_counts_and_delivers_a_snapshot_resetting_after_a_successful_flush():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_session_checkin.side_effect = lambda payload: delivered.put(payload) or True

    flusher = SessionFlusher(_configuration(), client)
    flusher.record_session(crashed=False)
    flusher.record_session(crashed=True)
    flusher.record_session(crashed=False)
    flusher.flush()

    payload = delivered.get(timeout=1)
    assert payload["sessions_count"] == 3
    assert payload["crashed_sessions_count"] == 1
    assert payload["release"] == "1.2.3"
    assert payload["environment"] == "production"

    # Nothing new was recorded since the successful flush above, so this one sends nothing.
    flusher.flush()
    assert delivered.empty()


def test_does_nothing_on_a_flush_with_no_sessions_recorded():
    client = MagicMock()

    SessionFlusher(_configuration(), client).flush()

    client.deliver_session_checkin.assert_not_called()


def test_keeps_the_counts_for_the_next_flush_instead_of_losing_them_when_delivery_fails():
    client = MagicMock()
    client.deliver_session_checkin.return_value = False

    flusher = SessionFlusher(_configuration(), client)
    flusher.record_session(crashed=False)
    flusher.flush()

    delivered = queue.Queue()
    client.deliver_session_checkin.side_effect = lambda payload: delivered.put(payload) or True
    flusher.record_session(crashed=False)
    flusher.flush()

    payload = delivered.get(timeout=1)
    assert payload["sessions_count"] == 2


def test_the_background_thread_flushes_on_its_own_after_the_configured_interval():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_session_checkin.side_effect = lambda payload: delivered.put(payload) or True

    configuration = _configuration()
    configuration.session_flush_interval = 0.05

    flusher = SessionFlusher(configuration, client)
    flusher.record_session(crashed=False)

    payload = delivered.get(timeout=1)
    assert payload["sessions_count"] == 1


def test_the_background_thread_survives_the_client_raising_instead_of_returning():
    delivered = queue.Queue()
    call_count = {"n": 0}

    def deliver(payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        delivered.put(payload)
        return True

    client = MagicMock()
    client.deliver_session_checkin.side_effect = deliver

    configuration = _configuration()
    configuration.session_flush_interval = 0.05

    flusher = SessionFlusher(configuration, client)
    flusher.record_session(crashed=False)  # the first flush tick raises; must not kill the thread

    # A later tick recovers and delivers on its own; a dead thread would leave this queue
    # empty forever and time out instead.
    delivered.get(timeout=2)


def test_a_batch_the_server_rejected_is_dropped_and_never_sent_again():
    sent = []
    client = MagicMock()
    client.deliver_session_checkin.side_effect = lambda batch: sent.append(batch["sessions_count"]) or delivery_status.REJECTED

    flusher = SessionFlusher(_configuration(), client)
    flusher.record_session(crashed=False)
    flusher.flush()
    flusher.record_session(crashed=False)
    flusher.flush()
    flusher.flush()

    assert sent == [1, 1]


def test_a_batch_is_kept_through_four_failed_flushes_and_dropped_on_the_fifth():
    sent = []
    client = MagicMock()
    client.deliver_session_checkin.side_effect = lambda batch: sent.append(batch["sessions_count"]) or delivery_status.FAILED

    flusher = SessionFlusher(_configuration(), client)
    flusher.record_session(crashed=False)
    for _ in range(5):
        flusher.flush()
    flusher.flush()  # nothing left: the fifth failed flush dropped the batch

    assert sent == [1, 1, 1, 1, 1]


def test_a_flush_that_gets_through_starts_the_count_of_failed_flushes_again():
    outcomes = [delivery_status.FAILED] * 4 + [delivery_status.DELIVERED] + [delivery_status.FAILED] * 4 + [delivery_status.DELIVERED]
    sent = []
    client = MagicMock()
    client.deliver_session_checkin.side_effect = lambda batch: sent.append(batch["sessions_count"]) or outcomes.pop(0)

    flusher = SessionFlusher(_configuration(), client)
    flusher.record_session(crashed=False)
    for _ in range(5):
        flusher.flush()
    flusher.record_session(crashed=False)
    for _ in range(5):
        flusher.flush()
    flusher.flush()

    assert sent == [1] * 10
