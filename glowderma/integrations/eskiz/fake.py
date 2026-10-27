"""Deterministic Eskiz adapter (``ESKIZ_MODE=fake``, customer contract §2.14.1, plan §6).

The phone decides the outcome: ending ``0000`` → :class:`EskizRejected` (definitive), ending ``9999`` → the
transport is down (:class:`EskizUnavailable`, nothing left the process), anything else → accepted with the id
``fake-<sms_code_id>``.

``sent`` keeps every accepted message (phone and rendered text, so tests read the code there) **only while**
``settings.DEV_FEATURES_ALLOWED`` (local/test settings). Under the production settings, where a fake adapter
is allowed (demo/staging posture: no SMS is delivered), it keeps nothing and logs one INFO line
``sms.fake_accepted`` with the code's id — never the text or the code (customer contract §6.4).
"""

from __future__ import annotations

import logging

from glowderma.core import integration_modes

from .ports import EskizRejected, EskizUnavailable, SmsMessage

REJECTED_SUFFIX = "0000"
DOWN_SUFFIX = "9999"

logger = logging.getLogger(__name__)


class FakeEskiz:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[SmsMessage] = []

    def send(self, message: SmsMessage) -> str:
        if message.phone.endswith(REJECTED_SUFFIX):
            raise EskizRejected("fake: number rejected")
        if message.phone.endswith(DOWN_SUFFIX):
            raise EskizUnavailable("fake: transport down", sent=False)
        if integration_modes.dev_features_allowed():
            self.sent.append(message)
        else:
            logger.info("sms.fake_accepted sms_code_id=%s", message.sms_code_id)
        return f"fake-{message.sms_code_id}"
