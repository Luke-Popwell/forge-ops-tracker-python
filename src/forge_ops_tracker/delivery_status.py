"""What became of one delivery attempt, as every Client.deliver_* method reports it. A bare
True/False could not tell "the server will never take this payload" apart from "try again later",
so a batch a plan or a validation rule refused was re-sent at every flush for as long as the
process lived. Ported from gems/forge_ops_tracker/lib/forge_ops_tracker/delivery_status.rb.

One deliberate difference from the Ruby gem: a status here is truthy only when it is DELIVERED, so
code that still reads a deliver_* result as a boolean (`if client.deliver(payload):`) means what it
always meant.
"""

# After this many flushes in a row that failed for now, a batch is dropped rather than kept for yet
# another attempt, so an outage (or a server answering 500 to one particular batch) can never pin
# the same data in memory for the life of the process.
MAX_FAILED_FLUSHES = 5

_RETRYABLE_CLIENT_ERRORS = (408, 429)


class DeliveryStatus(str):
    def __bool__(self):
        return self == "delivered"


# Any 2xx.
DELIVERED = DeliveryStatus("delivered")
# A 4xx other than 408 and 429: sending the same payload again can only fail the same way.
REJECTED = DeliveryStatus("rejected")
# A network error, a timeout, a 5xx, a 408 or a 429: worth another attempt later.
FAILED = DeliveryStatus("failed")


def for_code(code):
    if 200 <= code < 300:
        return DELIVERED
    if 400 <= code < 500 and code not in _RETRYABLE_CLIENT_ERRORS:
        return REJECTED
    return FAILED


def from_result(result):
    """Reads whatever a deliver callable handed back. A status passes through; anything else is
    read the way the old boolean contract was (truthy delivered, falsy failed for now), so a
    client or `deliver` callable that still answers True/False keeps working."""
    if isinstance(result, DeliveryStatus):
        return result
    return DELIVERED if result else FAILED


class FlushOutcome:
    """Decides, after each flush of one buffer/flusher, whether the batch it just sent is done
    with. Delivered and rejected batches are let go of at once; one that failed for now is kept,
    but only for MAX_FAILED_FLUSHES flushes in a row."""

    def __init__(self, configuration, what):
        """`what` names the batch in the debug log ("metrics", "performance samples")."""
        self._configuration = configuration
        self._what = what
        self._failed_flushes = 0

    def release(self, result):
        """True when the caller should remove what it just sent (delivered, or dropped), False
        when it should keep it for the next flush."""
        status = from_result(result)
        if status is DELIVERED:
            self._failed_flushes = 0
            return True
        if status is REJECTED:
            self._failed_flushes = 0
            self._configuration.logger.debug(
                "[forge_ops_tracker] dropping buffered %s: the server rejected them and will not accept a retry", self._what
            )
            return True

        self._failed_flushes += 1
        if self._failed_flushes < MAX_FAILED_FLUSHES:
            return False
        self._failed_flushes = 0
        self._configuration.logger.debug(
            "[forge_ops_tracker] dropping buffered %s: %d flushes in a row failed", self._what, MAX_FAILED_FLUSHES
        )
        return True
