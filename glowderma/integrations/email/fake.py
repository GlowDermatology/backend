"""Deterministic e-mail adapter (``EMAIL_MODE=fake``; the console of local development).

The recipient's domain decides the outcome: ``@rejected.test`` → :class:`EmailRejected` (definitive),
``@down.test`` → the relay is down (:class:`EmailUnavailable`, nothing left the process), anything else →
accepted with the id ``fake-<reference>``.

``sent`` keeps every accepted message (so tests read the code there) **only while**
``settings.DEV_FEATURES_ALLOWED`` (local/test settings); there the process also logs one INFO line
``email.fake_accepted`` with the reference and the subject (local development reads the fixed fake code,
``ESKIZ_FAKE_CODE``). Under the production settings, where a fake adapter is allowed (demo/staging posture:
nothing is delivered), it keeps nothing and logs the reference only. Never an address, never the body.
"""

from __future__ import annotations

import logging

from glowderma.core import integration_modes

from .ports import EmailMessage, EmailRejected, EmailUnavailable

REJECTED_DOMAIN = "@rejected.test"
DOWN_DOMAIN = "@down.test"

logger = logging.getLogger(__name__)


class FakeEmail:
    name = "fake"

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> str:
        if message.to.endswith(REJECTED_DOMAIN):
            raise EmailRejected("fake: recipient rejected")
        if message.to.endswith(DOWN_DOMAIN):
            raise EmailUnavailable("fake: relay down", sent=False)
        if integration_modes.dev_features_allowed():
            self.sent.append(message)
            logger.info("email.fake_accepted %s subject=%r", message.reference, message.subject)
        else:
            logger.info("email.fake_accepted %s", message.reference)
        return f"fake-{message.reference}"

    def is_configured(self) -> bool:
        return True
