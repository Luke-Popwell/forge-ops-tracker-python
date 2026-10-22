import queue
from unittest.mock import MagicMock

from forge_ops_tracker import delivery_status
from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.performance_flusher import PerformanceFlusher


def _configuration():
    config = Configuration()
    config.release = "1.2.3"
    config.environment = "production"
    return config


def test_buckets_by_transaction_name_and_delivers_one_aggregate_entry_per_transaction():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("GET /posts/<id>", 100)
    flusher.record("GET /posts/<id>", 200)
    flusher.record("GET /posts", 50)
    flusher.flush()

    samples = delivered.get(timeout=1)
    assert len(samples) == 2

    show = next(s for s in samples if s["transaction_name"] == "GET /posts/<id>")
    assert show["request_count"] == 2
    assert show["duration_sum_ms"] == 300
    assert show["max_duration_ms"] == 200
    assert show["release"] == "1.2.3"
    assert show["environment"] == "production"

    index = next(s for s in samples if s["transaction_name"] == "GET /posts")
    assert index["request_count"] == 1
    assert index["duration_sum_ms"] == 50

    # Nothing new was recorded since the successful flush above, so this one sends nothing.
    flusher.flush()
    assert delivered.empty()


def test_defaults_kind_to_controller_when_not_given():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("GET /posts/<id>", 100)
    flusher.flush()

    assert delivered.get(timeout=1)[0]["kind"] == "controller"


def test_buckets_separately_by_transaction_name_and_kind_so_a_job_and_a_controller_sharing_a_name_never_collide():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("Reports", 100, kind="controller")
    flusher.record("Reports", 50, kind="job")
    flusher.flush()

    samples = delivered.get(timeout=1)
    assert len(samples) == 2
    controller_sample = next(s for s in samples if s["kind"] == "controller")
    job_sample = next(s for s in samples if s["kind"] == "job")
    assert controller_sample["transaction_name"] == "Reports"
    assert controller_sample["request_count"] == 1
    assert controller_sample["duration_sum_ms"] == 100
    assert job_sample["transaction_name"] == "Reports"
    assert job_sample["request_count"] == 1
    assert job_sample["duration_sum_ms"] == 50


def test_does_nothing_on_a_flush_with_nothing_recorded():
    client = MagicMock()

    PerformanceFlusher(_configuration(), client).flush()

    client.deliver_performance_samples.assert_not_called()


def test_keeps_every_bucket_for_the_next_flush_instead_of_losing_them_when_delivery_fails():
    client = MagicMock()
    client.deliver_performance_samples.return_value = False

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("GET /posts/<id>", 100)
    flusher.flush()

    delivered = queue.Queue()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True
    flusher.record("GET /posts/<id>", 100)
    flusher.flush()

    samples = delivered.get(timeout=1)
    assert samples[0]["request_count"] == 2
    assert samples[0]["duration_sum_ms"] == 200


def test_the_background_thread_flushes_on_its_own_after_the_configured_interval():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True

    configuration = _configuration()
    configuration.performance_flush_interval = 0.05

    flusher = PerformanceFlusher(configuration, client)
    flusher.record("GET /posts/<id>", 100)

    samples = delivered.get(timeout=1)
    assert samples[0]["request_count"] == 1


def test_the_background_thread_survives_the_client_raising_instead_of_returning():
    delivered = queue.Queue()
    call_count = {"n": 0}

    def deliver(samples):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        delivered.put(samples)
        return True

    client = MagicMock()
    client.deliver_performance_samples.side_effect = deliver

    configuration = _configuration()
    configuration.performance_flush_interval = 0.05

    flusher = PerformanceFlusher(configuration, client)
    flusher.record("GET /posts/<id>", 100)  # the first flush tick raises; must not kill the thread

    # A later tick recovers and delivers on its own; a dead thread would leave this queue
    # empty forever and time out instead.
    delivered.get(timeout=2)


def test_delivers_a_latency_histogram_per_bucket_alongside_count_sum_and_max():
    delivered = queue.Queue()
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True

    flusher = PerformanceFlusher(_configuration(), client)
    for duration in (10, 40, 120, 700, 12_000):
        flusher.record("GET /posts", duration)
    flusher.flush()

    sample = delivered.get(timeout=1)[0]
    assert sample["histogram"] == {"50": 2, "250": 1, "1000": 1, "inf": 1}
    assert sum(sample["histogram"].values()) == sample["request_count"]


def test_keeps_histogram_counts_for_the_next_flush_when_delivery_fails():
    client = MagicMock()
    client.deliver_performance_samples.return_value = False

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("GET /posts", 10)
    flusher.flush()

    delivered = queue.Queue()
    client.deliver_performance_samples.side_effect = lambda samples: delivered.put(samples) or True
    flusher.record("GET /posts", 300)
    flusher.flush()

    assert delivered.get(timeout=1)[0]["histogram"] == {"50": 1, "500": 1}


def test_a_record_that_lands_during_delivery_is_never_lost_and_is_sent_on_the_next_flush():
    delivered = queue.Queue()
    flusher_holder = {}

    def deliver(samples):
        delivered.put(samples)
        # Lands in the window between the snapshot and delivery succeeding: for a bucket already
        # in the snapshot, and for a brand-new one.
        flusher_holder["flusher"].record("GET /posts", 300)
        flusher_holder["flusher"].record("GET /new", 5)
        return True

    client = MagicMock()
    client.deliver_performance_samples.side_effect = deliver

    flusher = PerformanceFlusher(_configuration(), client)
    flusher_holder["flusher"] = flusher
    flusher.record("GET /posts", 10)
    flusher.flush()

    first = delivered.get(timeout=1)
    assert first[0]["histogram"] == {"50": 1}

    flusher.flush()
    second = {s["transaction_name"]: s for s in delivered.get(timeout=1)}
    assert second["GET /posts"]["request_count"] == 1
    assert second["GET /posts"]["histogram"] == {"500": 1}
    assert second["GET /new"]["histogram"] == {"50": 1}


def test_a_batch_the_server_rejected_is_dropped_and_never_sent_again():
    sent = []
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda batch: sent.append(sum(s["request_count"] for s in batch)) or delivery_status.REJECTED

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("PostsController#show", 100.0)
    flusher.flush()
    flusher.record("PostsController#show", 100.0)
    flusher.flush()
    flusher.flush()

    assert sent == [1, 1]


def test_a_batch_is_kept_through_four_failed_flushes_and_dropped_on_the_fifth():
    sent = []
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda batch: sent.append(sum(s["request_count"] for s in batch)) or delivery_status.FAILED

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("PostsController#show", 100.0)
    for _ in range(5):
        flusher.flush()
    flusher.flush()  # nothing left: the fifth failed flush dropped the batch

    assert sent == [1, 1, 1, 1, 1]


def test_a_flush_that_gets_through_starts_the_count_of_failed_flushes_again():
    outcomes = [delivery_status.FAILED] * 4 + [delivery_status.DELIVERED] + [delivery_status.FAILED] * 4 + [delivery_status.DELIVERED]
    sent = []
    client = MagicMock()
    client.deliver_performance_samples.side_effect = lambda batch: sent.append(sum(s["request_count"] for s in batch)) or outcomes.pop(0)

    flusher = PerformanceFlusher(_configuration(), client)
    flusher.record("PostsController#show", 100.0)
    for _ in range(5):
        flusher.flush()
    flusher.record("PostsController#show", 100.0)
    for _ in range(5):
        flusher.flush()
    flusher.flush()

    assert sent == [1] * 10
