from unittest.mock import MagicMock

from forge_ops_tracker.configuration import Configuration
from forge_ops_tracker.reporter import Reporter


def _enabled_configuration():
    config = Configuration()
    config.dsn = "https://key@tracker.example.com/api/v1/events"
    config.enabled_environments = {"production"}
    config.environment = "production"
    return config


def test_builds_and_enqueues_a_payload_when_reporting_is_enabled():
    configuration = _enabled_configuration()
    built_payload = {"exception_class": "RuntimeError"}
    event_builder = MagicMock()
    event_builder.build.return_value = built_payload
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)
    error = RuntimeError("boom")
    context = {"a": 1}

    reporter.report(error, context=context)

    event_builder.build.assert_called_once_with(error, context=context, user=None, breadcrumbs=[], transaction_name=None, endpoint=None, trace_id=None)
    delivery_queue.push.assert_called_once_with(built_payload)


def test_passes_the_user_through_to_the_event_builder():
    configuration = _enabled_configuration()
    event_builder = MagicMock()
    event_builder.build.return_value = {}
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)
    error = RuntimeError("boom")
    user = {"id": 42, "email": "alice@example.com"}

    reporter.report(error, user=user)

    event_builder.build.assert_called_once_with(error, context=None, user=user, breadcrumbs=[], transaction_name=None, endpoint=None, trace_id=None)


def test_passes_the_breadcrumbs_through_to_the_event_builder():
    configuration = _enabled_configuration()
    event_builder = MagicMock()
    event_builder.build.return_value = {}
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)
    error = RuntimeError("boom")
    breadcrumbs = [{"category": "query", "message": "User Load", "level": "info", "timestamp": "2026-01-01T00:00:00Z", "data": {}}]

    reporter.report(error, breadcrumbs=breadcrumbs)

    event_builder.build.assert_called_once_with(error, context=None, user=None, breadcrumbs=breadcrumbs, transaction_name=None, endpoint=None, trace_id=None)


def test_passes_an_empty_breadcrumbs_list_when_none_were_given():
    configuration = _enabled_configuration()
    event_builder = MagicMock()
    event_builder.build.return_value = {}
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)
    error = RuntimeError("boom")

    reporter.report(error)

    event_builder.build.assert_called_once_with(error, context=None, user=None, breadcrumbs=[], transaction_name=None, endpoint=None, trace_id=None)


def test_does_nothing_when_reporting_is_disabled():
    configuration = _enabled_configuration()
    configuration.environment = "development"  # not in enabled_environments
    event_builder = MagicMock()
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)
    reporter.report(RuntimeError("boom"))

    delivery_queue.push.assert_not_called()


def test_never_raises_even_if_building_the_event_fails():
    configuration = _enabled_configuration()
    event_builder = MagicMock()
    event_builder.build.side_effect = RuntimeError("event builder exploded")
    delivery_queue = MagicMock()

    reporter = Reporter(configuration, event_builder, delivery_queue)

    reporter.report(RuntimeError("boom"))  # must not raise


def test_never_raises_even_if_enqueueing_fails():
    configuration = _enabled_configuration()
    event_builder = MagicMock()
    event_builder.build.return_value = {}
    delivery_queue = MagicMock()
    delivery_queue.push.side_effect = RuntimeError("queue exploded")

    reporter = Reporter(configuration, event_builder, delivery_queue)

    reporter.report(RuntimeError("boom"))  # must not raise
