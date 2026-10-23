import json
import os

import pytest

from forge_ops_tracker import pii_scrubber

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# A byte-identical copy of sdks/pii_scrubbing_vectors.json, generated from the server's own
# app/services/pii_scrubber.rb: card numbers, IBANs and bank-detail keys, as sdks/PII_SCRUBBING.md
# describes them.
with open(os.path.join(THIS_DIR, "fixtures", "pii_scrubbing_vectors.json"), encoding="utf-8") as vectors_file:
    VECTORS = json.load(vectors_file)


@pytest.mark.parametrize("vector", VECTORS["strings"], ids=lambda vector: vector["input"])
def test_scrubs_every_string_vector_exactly_as_the_server_does(vector):
    assert pii_scrubber.scrub(vector["input"]) == vector["scrubbed"]


@pytest.mark.parametrize("vector", VECTORS["keys"], ids=lambda vector: vector["key"])
def test_redacts_every_key_vector_exactly_as_the_server_does(vector):
    scrubbed = pii_scrubber.scrub({vector["key"]: "x"})

    assert (scrubbed[vector["key"]] == pii_scrubber.REDACTED) is vector["redacted"]


def test_redacts_an_email_address_embedded_in_free_text():
    assert (
        pii_scrubber.scrub_string("undefined method 'name' for user@example.com")
        == "undefined method 'name' for [EMAIL FILTERED]"
    )


def test_redacts_a_formatted_credit_card_number_but_leaves_an_ordinary_long_numeric_id_alone():
    assert pii_scrubber.scrub_string("card 4111-1111-1111-1111 declined") == "card [CREDIT CARD FILTERED] declined"
    assert (
        pii_scrubber.scrub_string("Couldn't find Invoice with id=8821445199")
        == "Couldn't find Invoice with id=8821445199"
    )


def test_redacts_known_api_key_and_token_formats():
    assert pii_scrubber.scrub_string("using sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc") == "using [STRIPE KEY FILTERED]"
    assert pii_scrubber.scrub_string("Authorization: Bearer abc123.def456") == "Authorization: [BEARER TOKEN FILTERED]"


def test_redacts_the_whole_value_under_a_sensitive_looking_key_regardless_of_type_or_casing():
    scrubbed = pii_scrubber.scrub({"password": "hunter2", "API-Key": "sk_live_abc", "count": 3})

    assert scrubbed == {"password": "[FILTERED]", "API-Key": "[FILTERED]", "count": 3}


def test_recurses_into_nested_dicts_and_lists():
    scrubbed = pii_scrubber.scrub({"user": {"email": "ada@example.com"}, "notes": ["no PII here"]})

    assert scrubbed == {"user": {"email": "[EMAIL FILTERED]"}, "notes": ["no PII here"]}
