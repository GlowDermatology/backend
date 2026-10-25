"""Demo cashback policy versions (design 03 §7.7, 02 §1.1): v1–v3 with their tiers.

Only the configuration rows are inserted here. Accounts, ledger entries, holds and purchases are **not**
seeded by this slice: B4a/B5 replay them through ``loyalty.services`` (store sales, Malika's timeline, the
deliberate negative balance of customer 13), so every balance chain comes out of production code paths.

Idempotent by ``version``. ``created_at`` is pinned to the design instants through
``core.seeding.pinned_clock`` (so ``effective_from > created_at`` and the derived states reproduce: v1 and v2
``expired``, v3 ``active`` at the anchor). v2/v3 are authored by the demo staff ``admin@glowderma.uz``
(accounts.seed, id 1) read through ``get_user_model()``; when the admin is absent the author is ``NULL`` so
the seeder runs standalone. The run is anchor-independent: every timestamp is an absolute design date, and
``keep_open`` has nothing to move here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction

from glowderma.accounts.models import StaffUser
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty.models import CashbackPolicy, CashbackTier

SEED_ADMIN_EMAIL = "admin@glowderma.uz"


def _dt(month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=TASHKENT)


@dataclass(frozen=True, slots=True)
class PolicySpec:
    version: int
    created_at: datetime
    effective_from: datetime
    by_admin: bool  # False = system / seed ("Tizim")
    earn_on_cashback_paid: bool
    tiers: tuple[tuple[int, int | None, str], ...]  # (from inclusive, to exclusive | None, percent)
    basis: str = enums.RateBasis.BEFORE_PURCHASE


TIER_1 = (0, 20_000_000, "1.00")
TIER_2 = (20_000_000, 30_000_000, "2.00")
TIER_3 = (30_000_000, 50_000_000, "3.00")

POLICY_VERSIONS: tuple[PolicySpec, ...] = (
    PolicySpec(1, _dt(5, 20, 10, 0), _dt(6, 1), False, False, (TIER_1,)),
    PolicySpec(2, _dt(8, 1, 10, 0), _dt(8, 15), True, False, (TIER_1, TIER_2)),
    # G-2 (plan §4.3): v3 earns on the cashback-paid part so order №10248's drawn base 568 000 reproduces.
    PolicySpec(3, _dt(9, 20, 10, 0), _dt(10, 1), True, True, (TIER_1, TIER_2, TIER_3)),
)


def _insert(spec: PolicySpec, admin: StaffUser | None) -> CashbackPolicy:
    with pinned_clock(spec.created_at):
        policy = CashbackPolicy.objects.create(
            version=spec.version,
            effective_from=spec.effective_from,
            basis=spec.basis,
            earn_on_cashback_paid=spec.earn_on_cashback_paid,
            author=admin if spec.by_admin else None,
        )
        for from_amount, to_amount, percent in spec.tiers:  # sorted by from, so id order == from order
            CashbackTier.objects.create(
                policy=policy, from_amount=from_amount, to_amount=to_amount, percent=Decimal(percent)
            )
    return policy


@register_seeder("loyalty", order=60)
def seed_loyalty(context: SeedContext) -> None:
    admin: StaffUser | None = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
    with transaction.atomic():
        existing = set(CashbackPolicy.objects.values_list("version", flat=True))
        for spec in POLICY_VERSIONS:
            if spec.version in existing:
                continue
            _insert(spec, admin)
