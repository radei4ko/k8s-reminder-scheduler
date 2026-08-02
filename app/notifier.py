"""Notification gateway stub - stands in for an email/SMS provider.

Same shape as a real one would be: an idempotency key goes in so that sending
the same key twice is safe on the provider's side too. That matters here
specifically because of the lease/fencing design - see the module docstring on
ReminderTask for why. In short: if a lease expires and gets reclaimed while the
original worker is still mid-send (just slow, not actually dead), both workers
may end up calling the notifier for the same task. The database-side fencing
token stops both of them from recording a "completed" result, but it cannot
stop a message from actually going out twice unless the notifier itself
dedupes by key - which is exactly what a real provider's idempotency key is
for.
"""

import random
import uuid
from dataclasses import dataclass
from typing import Optional

from app.config import NOTIFIER_FAILURE_RATE

TRANSIENT_ERRORS = ("provider_timeout", "rate_limited", "temporary_outage")
PERMANENT_ERRORS = ("invalid_recipient", "unsubscribed")


def is_retriable(error_code: Optional[str]) -> bool:
    if error_code is None:
        return False
    # Unknown codes are treated as transient: one wasted retry costs less than
    # silently giving up on a reminder because the provider added a new error
    # code we have not seen yet.
    return error_code not in PERMANENT_ERRORS


@dataclass(frozen=True)
class NotifierResponse:
    succeeded: bool
    error_code: Optional[str] = None
    provider_reference: Optional[str] = None


class FakeNotifier:
    def __init__(self, failure_rate: float = NOTIFIER_FAILURE_RATE, rng: Optional[random.Random] = None):
        if not 0.0 <= failure_rate <= 1.0:
            raise ValueError(f"failure_rate must be between 0 and 1, got {failure_rate}")
        self.failure_rate = failure_rate
        self._rng = rng or random.Random()

    def send(self, *, idempotency_key: str, channel: str, message: str) -> NotifierResponse:
        if self._rng.random() >= self.failure_rate:
            return NotifierResponse(
                succeeded=True,
                provider_reference=f"msg_{uuid.uuid4().hex[:16]}",
            )

        if self._rng.random() < 0.25:
            code = self._rng.choice(PERMANENT_ERRORS)
        else:
            code = self._rng.choice(TRANSIENT_ERRORS)
        return NotifierResponse(succeeded=False, error_code=code)


class ScriptedNotifier:
    """Returns a fixed list of responses, in order. For tests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def send(self, *, idempotency_key: str, channel: str, message: str) -> NotifierResponse:
        self.calls.append(idempotency_key)
        if not self._responses:
            raise AssertionError("ScriptedNotifier ran out of scripted responses")
        return self._responses.pop(0)
