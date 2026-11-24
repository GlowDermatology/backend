"""Revoking the Apple tokens a refused flow exchanged, once the transaction that refused it ended."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.db import transaction

from glowderma.accounts import services as account_services
from glowderma.core.pii import pii_ref

logger = logging.getLogger("glowderma.customers.services")


@dataclass(frozen=True, slots=True)
class RefusedAppleToken:
    """An Apple refresh token the code exchange produced for a request that was then refused."""

    sealed: str
    client_id: str
    customer_id: int | None
    subject: str


# Inside an Idempotency-Key transaction (``POST me/social/``) a refusal rolls the ``apple.revoke`` row back
# with everything else; the view collects the tokens instead and queues them after that transaction ended.
_REFUSED_APPLE_TOKENS: ContextVar[list[RefusedAppleToken] | None] = ContextVar(
    "refused_apple_tokens", default=None
)


@contextmanager
def revoke_refused_apple_tokens() -> Iterator[None]:
    """Wrap a request whose own transaction rolls back on a refusal: the Apple tokens refused inside are
    revoked (``apple.revoke``, each in its own transaction) once the block has ended, success or error."""
    pending: list[RefusedAppleToken] = []
    marker = _REFUSED_APPLE_TOKENS.set(pending)
    try:
        yield
    finally:
        _REFUSED_APPLE_TOKENS.reset(marker)
        for refused in pending:
            _enqueue_refused_apple_token(refused)


def revoke_refused_apple_token(refused: RefusedAppleToken) -> None:
    pending = _REFUSED_APPLE_TOKENS.get()
    if pending is not None:
        pending.append(refused)
    else:
        _enqueue_refused_apple_token(refused)


def _enqueue_refused_apple_token(refused: RefusedAppleToken) -> None:
    """``accounts.services.enqueue_apple_token_revoke`` in its own transaction (nothing is queued while the
    subject is linked); a failure is logged, never raised over the refusal the client receives."""
    try:
        with transaction.atomic():
            account_services.enqueue_apple_token_revoke(
                refused.sealed,
                client_id=refused.client_id,
                customer_id=refused.customer_id,
                subject=refused.subject,
            )
    except Exception:
        logger.exception(
            "apple.revoke not queued for a refused flow customer_id=%s subject_ref=%s",
            refused.customer_id,
            pii_ref(refused.subject),
        )


__all__ = [
    "RefusedAppleToken",
    "revoke_refused_apple_token",
    "revoke_refused_apple_tokens",
]
