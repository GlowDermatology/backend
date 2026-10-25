"""Deterministic FCM adapter for tests and local runs (``FCM_MODE=fake``).

The token encodes the outcome, so the ``failed`` counters and the dead-token path are testable without a
provider:

``<anything>-bad``
    Rejected with ``unregistered`` (a dead token).
``<anything>-down``
    The provider is unreachable: the whole batch raises :class:`FcmUnavailable`.
anything else
    Accepted with the message id ``fake-<sha256(token)[:12]>-<n>`` (``n`` = position in the batch).

``sent`` keeps every accepted message in memory, so a test can assert what was handed to the provider.
"""

from collections.abc import Sequence
from hashlib import sha256

from .ports import UNREGISTERED, FcmUnavailable, PushMessage, PushResult

BAD_SUFFIX = "-bad"
DOWN_SUFFIX = "-down"


class FakeFcm:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        if any(message.token.endswith(DOWN_SUFFIX) for message in messages):
            raise FcmUnavailable("fake provider down")
        results: list[PushResult] = []
        for index, message in enumerate(messages):
            if message.token.endswith(BAD_SUFFIX):
                results.append(PushResult(ok=False, error=UNREGISTERED))
                continue
            self.sent.append(message)
            digest = sha256(message.token.encode()).hexdigest()[:12]
            results.append(PushResult(ok=True, message_id=f"fake-{digest}-{index}"))
        return results
