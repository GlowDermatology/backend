"""Which orders are inside the payment grace (customer contract §2.9.1), answered by ``commerce``.

A hold's effective expiry is its ``expires_at`` (the order's ``reservation_expires_at``) plus
``PAYMENT_IN_FLIGHT_GRACE_SECONDS`` only while a transaction attached in time is still open. Only ``commerce``
can see the payments, and ``loyalty`` may not import it (layers), so ``commerce`` registers the answer at
start-up (``CommerceConfig.ready``). Without a registration every overdue hold counts as inside the grace
(the release by the expiry job is at most one beat away).
"""

from collections.abc import Callable, Collection
from datetime import datetime

type GraceOracle = Callable[[Collection[int], datetime], set[int]]

_oracle: GraceOracle | None = None


def register(oracle: GraceOracle) -> None:
    global _oracle  # noqa: PLW0603 - one process-wide registration, made once at app start-up
    _oracle = oracle


def orders_in_grace(order_ids: Collection[int], instant: datetime) -> set[int]:
    """The ``order_ids`` whose overdue hold is still kept by an open transaction attached in time."""
    if not order_ids:
        return set()
    if _oracle is None:
        return set(order_ids)
    return _oracle(order_ids, instant)
