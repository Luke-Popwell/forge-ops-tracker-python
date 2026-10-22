import re

from forge_ops_tracker.breadcrumb_buffer import BreadcrumbBuffer
from forge_ops_tracker.configuration import Configuration

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _configuration(max_breadcrumbs=3):
    config = Configuration()
    config.max_breadcrumbs = max_breadcrumbs
    return config


def test_returns_entries_in_insertion_order_each_carrying_the_given_fields_plus_a_timestamp():
    buffer = BreadcrumbBuffer(_configuration())

    buffer.add(category="query", message="User Load", level="info", data={"duration_ms": 1.2})

    entry = buffer.all()[0]
    assert entry["category"] == "query"
    assert entry["message"] == "User Load"
    assert entry["level"] == "info"
    assert entry["data"] == {"duration_ms": 1.2}
    assert ISO_UTC_RE.match(entry["timestamp"])


def test_defaults_level_to_info_and_data_to_an_empty_dict_when_not_given():
    buffer = BreadcrumbBuffer(_configuration())

    buffer.add(category="custom", message="checkpoint")

    entry = buffer.all()[0]
    assert entry["level"] == "info"
    assert entry["data"] == {}


def test_drops_the_oldest_entry_once_max_breadcrumbs_is_exceeded():
    buffer = BreadcrumbBuffer(_configuration(max_breadcrumbs=3))

    for n in range(4):
        buffer.add(category="custom", message=f"entry {n}")

    assert [e["message"] for e in buffer.all()] == ["entry 1", "entry 2", "entry 3"]


def test_re_reads_max_breadcrumbs_on_every_add_not_just_at_construction():
    configuration = _configuration(max_breadcrumbs=3)
    buffer = BreadcrumbBuffer(configuration)

    buffer.add(category="custom", message="one")
    buffer.add(category="custom", message="two")
    configuration.max_breadcrumbs = 1
    buffer.add(category="custom", message="three")

    assert [e["message"] for e in buffer.all()] == ["three"]


def test_adds_nothing_at_all_when_max_breadcrumbs_is_0():
    configuration = _configuration(max_breadcrumbs=0)
    buffer = BreadcrumbBuffer(configuration)

    buffer.add(category="custom", message="one")

    assert buffer.all() == []


def test_all_returns_a_copy_not_the_internal_list():
    buffer = BreadcrumbBuffer(_configuration())
    buffer.add(category="custom", message="one")

    snapshot = buffer.all()
    snapshot.append({"category": "custom", "message": "mutated externally"})

    assert len(buffer.all()) == 1


def test_non_string_category_message_and_level_are_coerced_to_strings():
    buffer = BreadcrumbBuffer(_configuration())

    buffer.add(category="custom", message=404, level="info")

    assert buffer.all()[0]["message"] == "404"
