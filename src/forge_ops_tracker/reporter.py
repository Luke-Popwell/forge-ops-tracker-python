"""Ties Configuration, EventBuilder, and DeliveryQueue together into the
one thing callers actually need: report an exception. Mirrors
gems/forge_ops_tracker's ErrorSubscriber#report: never raises. An error
reporter that itself raises while reporting an error is the worst
possible failure mode, so every path here is wrapped to guarantee this
never propagates back into the host app.
"""


class Reporter:
    def __init__(self, configuration, event_builder, delivery_queue):
        self._configuration = configuration
        self._event_builder = event_builder
        self._delivery_queue = delivery_queue

    def report(self, exc, context=None, user=None, breadcrumbs=None, transaction_name=None, endpoint=None, trace_id=None):
        try:
            if not self._configuration.is_enabled():
                return

            payload = self._event_builder.build(
                exc,
                context=context,
                user=user,
                breadcrumbs=breadcrumbs or [],
                transaction_name=transaction_name,
                endpoint=endpoint,
                trace_id=trace_id,
            )
            self._delivery_queue.push(payload)
        except Exception as e:  # noqa: BLE001 (deliberately broad: this must never raise back into the host app)
            self._configuration.logger.debug("[forge_ops_tracker] report failed: %s: %s", type(e).__name__, e)
