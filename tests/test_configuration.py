from forge_ops_tracker.configuration import Configuration


def _configuration():
    config = Configuration()
    config.dsn = None
    config.enabled_environments = {"production", "staging"}
    config.environment = "production"
    return config


def test_extracts_api_key_and_credential_free_ingestion_uri_from_dsn():
    config = _configuration()
    config.dsn = "https://secret-key@tracker.example.com/api/v1/events"

    assert config.api_key == "secret-key"
    assert config.ingestion_uri() == "https://tracker.example.com/api/v1/events"


def test_returns_none_for_both_when_there_is_no_dsn():
    config = _configuration()
    config.dsn = None

    assert config.api_key is None
    assert config.ingestion_uri() is None


def test_returns_none_for_both_when_the_dsn_is_malformed():
    config = _configuration()
    config.dsn = "not a uri :: at all"

    assert config.api_key is None
    assert config.ingestion_uri() is None


def test_is_enabled_with_a_valid_dsn_in_an_enabled_environment():
    config = _configuration()
    config.dsn = "https://key@tracker.example.com/api/v1/events"
    config.environment = "production"

    assert config.is_enabled() is True


def test_is_not_enabled_with_no_dsn_configured():
    config = _configuration()
    config.dsn = None

    assert config.is_enabled() is False


def test_is_not_enabled_when_the_dsn_has_no_api_key():
    config = _configuration()
    config.dsn = "https://tracker.example.com/api/v1/events"

    assert config.is_enabled() is False


def test_is_not_enabled_outside_the_configured_enabled_environments():
    config = _configuration()
    config.dsn = "https://key@tracker.example.com/api/v1/events"
    config.environment = "development"

    assert config.is_enabled() is False


def test_capture_source_context_defaults_to_true():
    assert Configuration().capture_source_context is True


def test_track_sessions_defaults_to_true():
    assert Configuration().track_sessions is True


def test_track_breadcrumbs_defaults_to_true():
    assert Configuration().track_breadcrumbs is True


def test_max_breadcrumbs_defaults_to_30():
    assert Configuration().max_breadcrumbs == 30


def test_session_flush_interval_defaults_to_60():
    assert Configuration().session_flush_interval == 60


def test_session_checkins_uri_swaps_events_for_session_checkins():
    config = _configuration()
    config.dsn = "https://secret-key@tracker.example.com/api/v1/events"

    assert config.session_checkins_uri() == "https://tracker.example.com/api/v1/session_checkins"


def test_session_checkins_uri_is_none_with_no_dsn():
    config = _configuration()
    config.dsn = None

    assert config.session_checkins_uri() is None


def test_spans_uri_swaps_events_for_spans():
    config = _configuration()
    config.dsn = "https://secret-key@tracker.example.com/api/v1/events"

    assert config.spans_uri() == "https://tracker.example.com/api/v1/spans"


def test_spans_uri_is_none_with_no_dsn():
    config = _configuration()
    config.dsn = None

    assert config.spans_uri() is None


def test_track_tracing_defaults_to_true():
    assert Configuration().track_tracing is True


def test_trace_capture_threshold_ms_defaults_to_1000():
    assert Configuration().trace_capture_threshold_ms == 1000


def test_propagates_traces_to_every_host_by_default():
    config = _configuration()

    assert config.propagate_traces is True
    assert config.trace_propagation_targets is None
    assert config.propagates_trace_to("api.example.com") is True


def test_propagate_traces_false_turns_the_header_off_everywhere():
    config = _configuration()
    config.propagate_traces = False
    config.trace_propagation_targets = ["api.example.com"]

    assert config.propagates_trace_to("api.example.com") is False


def test_a_string_target_matches_that_host_and_its_subdomains_on_a_dot_boundary_ignoring_case():
    config = _configuration()
    config.trace_propagation_targets = [".Example.com"]

    assert config.propagates_trace_to("example.com") is True
    assert config.propagates_trace_to("API.example.COM") is True
    assert config.propagates_trace_to("badexample.com") is False
    assert config.propagates_trace_to("example.com.evil.net") is False


def test_a_regular_expression_target_is_searched_against_the_host():
    import re

    config = _configuration()
    config.trace_propagation_targets = [re.compile(r"^10\.0\.")]

    assert config.propagates_trace_to("10.0.0.5") is True
    assert config.propagates_trace_to("110.0.0.5") is False


def test_an_empty_target_list_or_an_empty_string_target_matches_nothing():
    config = _configuration()
    config.trace_propagation_targets = []
    assert config.propagates_trace_to("api.example.com") is False

    config.trace_propagation_targets = ["", "."]
    assert config.propagates_trace_to("api.example.com") is False


def test_recognizes_its_own_ingestion_host_and_port():
    config = _configuration()
    config.dsn = "https://key@tracker.example.com/api/v1/events"

    assert config.is_own_request("https://tracker.example.com/api/v1/spans") is True
    assert config.is_own_request("https://tracker.example.com:443/api/v1/events") is True
    assert config.is_own_request("https://tracker.example.com:8443/api/v1/events") is False
    assert config.is_own_request("http://tracker.example.com/api/v1/events") is False
    assert config.is_own_request("https://api.example.com/v1/orders") is False


def test_nothing_is_its_own_request_without_a_dsn():
    config = _configuration()

    assert config.is_own_request("https://tracker.example.com/api/v1/events") is False
