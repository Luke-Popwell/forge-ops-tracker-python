"""Delivers a payload (an event, or a session-checkin aggregate) over HTTP. Every failure
mode (DNS, connection, timeout, TLS, a non-2xx response) is caught here and turned into a
delivery status rather than a raised exception, since a broken or unreachable tracker must
never be able to break the host app. Ported from
gems/forge_ops_tracker/lib/forge_ops_tracker/client.rb.

Every deliver_* method answers DELIVERED, REJECTED or FAILED from delivery_status.py; only
DELIVERED is truthy, so reading the result as a boolean still means "it was delivered".

Uses only the standard library (urllib), not `requests`: same reason
the Ruby gem uses plain Net::HTTP rather than a gem dependency: this has
to work in any host app without adding a dependency of its own.
"""

import json
import urllib.error
import urllib.request

from .delivery_status import FAILED, for_code


class Client:
    def __init__(self, configuration):
        self._configuration = configuration

    def deliver(self, payload):
        return self._post(self._configuration.ingestion_uri(), payload)

    def deliver_session_checkin(self, payload):
        return self._post(self._configuration.session_checkins_uri(), payload)

    def deliver_performance_samples(self, samples):
        return self._post(self._configuration.performance_samples_uri(), {"samples": samples})

    def deliver_spans(self, trace_id, spans):
        return self._post(self._configuration.spans_uri(), {"trace_id": trace_id, "spans": spans})

    def deliver_metrics(self, entries):
        return self._post(self._configuration.custom_metrics_uri(), {"metrics": entries})

    def deliver_infrastructure_metrics(self, entries):
        return self._post(self._configuration.infrastructure_metrics_uri(), {"metrics": entries})

    def deliver_change(self, payload):
        return self._post(self._configuration.changes_uri(), payload)

    def deliver_change_snapshot(self, payload):
        return self._post(self._configuration.change_snapshots_uri(), payload)

    def deliver_query_plan(self, payload):
        return self._post(self._configuration.query_plans_uri(), payload)

    def _post(self, uri, payload):
        if not uri:
            return FAILED

        try:
            body = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                uri,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._configuration.api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=self._configuration.timeout) as response:
                return for_code(response.status)
        except urllib.error.HTTPError as e:
            # urlopen raises for every 4xx and 5xx; the code is what tells a refusal from a retry.
            self._configuration.logger.debug("[forge_ops_tracker] delivery failed: HTTP %s", e.code)
            return self._status_for_http_error(e)
        except Exception as e:  # noqa: BLE001 (deliberately broad: a broken/unreachable tracker must never break the host app)
            self._configuration.logger.debug("[forge_ops_tracker] delivery failed: %s: %s", type(e).__name__, e)
            return FAILED

    @staticmethod
    def _status_for_http_error(error):
        try:
            return for_code(int(error.code))
        except (TypeError, ValueError):
            return FAILED
