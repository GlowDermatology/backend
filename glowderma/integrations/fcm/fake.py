"""Deterministic FCM adapter for tests and local runs (``FCM_MODE=fake``).

The token encodes the outcome, so the ``failed`` counters and the dead-token path are testable without a
provider:

``<anything>-bad``
    Rejected with ``unregistered`` (a dead token: the device is pruned).
``<anything>-payload``
    Rejected with ``payload_invalid`` (a malformed message: the device is left alone).
``<anything>-down``
    The provider is unreachable: the whole batch raises :class:`FcmUnavailable`.
``<anything>-busy``
    Not sent (``not_sent``: FCM answered 429/5xx for that message); the caller sends it again later.
anything else
    Accepted with the message id ``fake-<sha256(token)[:12]>-<n>`` (``n`` = position in the batch).

``sent`` keeps every accepted message with all its fields — **only while** ``settings.DEV_FEATURES_ALLOWED``
(local/test settings). Under the production settings, where a fake adapter is allowed (customer contract
§6.14), it keeps nothing and logs one INFO line per message with ids only, so no push text accumulates in a
worker's memory and pushes are reported sent although no device receives them.
"""

import logging
from collections.abc import Sequence
from hashlib import sha256

from glowderma.core import integration_modes

from .ports import NOT_SENT, PAYLOAD_INVALID, UNREGISTERED, FcmUnavailable, PushMessage, PushResult

BAD_SUFFIX = "-bad"
BUSY_SUFFIX = "-busy"
PAYLOAD_SUFFIX = "-payload"
DOWN_SUFFIX = "-down"

logger = logging.getLogger(__name__)


class FakeFcm:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[PushMessage] = []

    def send(self, messages: Sequence[PushMessage]) -> list[PushResult]:
        if any(message.token.endswith(DOWN_SUFFIX) for message in messages):
            raise FcmUnavailable("fake provider down")
        record = integration_modes.dev_features_allowed()
        results: list[PushResult] = []
        for index, message in enumerate(messages):
            if message.token.endswith(BAD_SUFFIX):
                results.append(PushResult(ok=False, error=UNREGISTERED))
                continue
            if message.token.endswith(PAYLOAD_SUFFIX):
                results.append(PushResult(ok=False, error=PAYLOAD_INVALID))
                continue
            if message.token.endswith(BUSY_SUFFIX):
                results.append(PushResult(ok=False, error=NOT_SENT))
                continue
            digest = sha256(message.token.encode()).hexdigest()[:12]
            message_id = f"fake-{digest}-{index}"
            if record:
                self.sent.append(message)
            else:
                logger.info(
                    "fcm.fake_accepted message_id=%s type=%s", message_id, message.data.get("type", "")
                )
            results.append(PushResult(ok=True, message_id=message_id))
        if results and all(result.error == NOT_SENT for result in results):
            raise FcmUnavailable("fake provider busy: nothing was sent")
        return results
