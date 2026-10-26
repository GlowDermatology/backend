"""Demo loyalty data (design 03 §7.7, 02 §1.1, §4.5): policy versions v1–v3 and the generated store sales.

Wave 3 inserted only the configuration rows (policies and tiers). B4a adds the store purchases of the
generated customers 13–426 and of the named customers 1–12 **except Malika (5)**: ``store =
accounts.seeds.CustomerFacts(id).store_total`` (the mock's ``createRandom(id × 97)`` draw); when ``> 0`` the
sale ``S-{80000 + id × 7}`` of that amount, sold 2026-09-05 12:00 Tashkent, is replayed through
``loyalty.services.record_store_purchase`` under ``pinned_clock`` → one credit ``store // 100`` under v2
(1 % below 20 M, basis before_purchase), exactly the mock's "Chek … so‘m · 1%" rows. Javohir (7) gets
``max(store, 2 000 000)`` so that his order №10246 can spend 20 000 so'm of cashback (B5).

Malika's whole timeline and the deliberate negative balance of customer 13 (03 §7.7) are **not** seeded here:
the commerce seeder (B5) replays them in chronological order through ``loyalty.services`` together with the
orders, so every ``balance_after`` chain comes out of production code paths.

Posting order: ``post_entry`` refuses an entry earlier than the account's newest one, and the generated
customers' paid orders (B5) fall on both sides of 2026-09-05. So the store sales must join that chronological
replay: once a ``commerce`` seeder is registered, :func:`seed_loyalty` seeds the policies only and the
commerce seeder calls :func:`seed_store_sales` when its replay reaches :data:`STORE_SALE_SOLD_AT`. Without
one (this slice, or a standalone run) :func:`seed_loyalty` seeds them itself.

Idempotent: policies by ``version``, sales by ``billz_sale_id`` (the service itself is idempotent; the
seeder also skips the sales already present). A migrated database already holds the install's first policy
(``loyalty.0006``: v1 with the L3 bands from 2026-06-01, the design v1's period): the design v1 is then not
seeded (versions are append-only) and the run notes it; v2 and v3 follow as designed, and every credit of the
dataset stays the same (no seeded purchase of the v1 period reaches the 20 M band). Every timestamp is a
pinned design instant, so the run is anchor-independent and ``keep_open`` has nothing to move. A missing
lower-layer row (no demo admin, no customers) is tolerated: the author is ``NULL`` and absent customers are
skipped (standalone run).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction

from glowderma.accounts.models import Customer, StaffUser
from glowderma.accounts.seeds import MALIKA_ID, CustomerFacts
from glowderma.core import enums
from glowderma.core.seeding import SeedContext, pinned_clock, register_seeder, registered_seeders
from glowderma.core.timeutils import TASHKENT
from glowderma.loyalty import services
from glowderma.loyalty.models import CashbackPolicy, CashbackTier, Purchase

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
STORE_SALE_CUSTOMER_IDS = range(
    1, 427
)  # named 1–12 (except Malika) and generated 13–426; 427–432 are deleted
STORE_SALE_SOLD_AT = datetime(2026, 9, 5, 12, 0, tzinfo=TASHKENT)  # under v2: 1 % below 20 M
JAVOHIR_ID = 7
JAVOHIR_MIN_STORE_TOTAL = 2_000_000  # order №10246 spends 20 000 so'm of cashback (03 §7.7)
STORE_SALES_REPLAYED_BY = "commerce"  # the seeder that interleaves the store sales with its orders


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


def _matches(spec: PolicySpec) -> bool:
    """Whether the stored version ``spec.version`` has the tiers of ``spec``."""
    stored = CashbackTier.objects.filter(policy__version=spec.version).order_by("from_amount")
    return [(t.from_amount, t.to_amount, t.percent) for t in stored] == [
        (low, high, Decimal(percent)) for low, high, percent in spec.tiers
    ]


def store_sale_id(customer_id: int) -> str:
    """The Billz document number of the seeded store sale of a customer (``S-{80000 + id × 7}``)."""
    return f"S-{80_000 + customer_id * 7}"


def store_total(customer_id: int) -> int:
    """The mock's store total of a customer (03 §7.7), with Javohir's floor applied."""
    total = CustomerFacts(customer_id).store_total
    if customer_id == JAVOHIR_ID:
        total = max(total, JAVOHIR_MIN_STORE_TOTAL)
    return total


def store_sales() -> list[tuple[int, str, int]]:
    """``(customer_id, billz_sale_id, amount)`` of every seeded store sale (amount > 0, Malika excluded)."""
    rows: list[tuple[int, str, int]] = []
    for customer_id in STORE_SALE_CUSTOMER_IDS:
        if customer_id == MALIKA_ID:
            continue
        amount = store_total(customer_id)
        if amount > 0:
            rows.append((customer_id, store_sale_id(customer_id), amount))
    return rows


def seed_store_sales() -> int:
    """Replay the missing store sales through the service at :data:`STORE_SALE_SOLD_AT` (pinned clock).

    Idempotent; returns how many were recorded this run. Called by :func:`seed_loyalty` standalone, or by the
    commerce seeder at that point of its chronological replay (see the module docstring).
    """
    sales = store_sales()
    present = set(
        Purchase.objects.filter(billz_sale_id__in=[sale_id for _, sale_id, _ in sales]).values_list(
            "billz_sale_id", flat=True
        )
    )
    customers = set(
        Customer.objects.filter(pk__in=[customer_id for customer_id, _, _ in sales]).values_list(
            "pk", flat=True
        )
    )
    recorded = 0
    with pinned_clock(STORE_SALE_SOLD_AT):
        for customer_id, sale_id, amount in sales:
            if sale_id in present or customer_id not in customers:
                continue
            result = services.record_store_purchase(
                customer_id,
                billz_sale_id=sale_id,
                billz_sale_ref=sale_id,
                amount=amount,
                sold_at=STORE_SALE_SOLD_AT,
            )
            recorded += int(result.created)
    return recorded


@register_seeder("loyalty", order=60)
def seed_loyalty(context: SeedContext) -> None:
    admin: StaffUser | None = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
    with transaction.atomic():
        existing = set(CashbackPolicy.objects.values_list("version", flat=True))
        for spec in POLICY_VERSIONS:
            if spec.version in existing:
                if not _matches(spec):
                    context.note(
                        f"loyalty: policy v{spec.version} already exists with other tiers (the install's "
                        "first policy, loyalty.0006); the design version was not seeded"
                    )
                continue
            _insert(spec, admin)
        if STORE_SALES_REPLAYED_BY not in {name for name, _order, _seeder in registered_seeders()}:
            seed_store_sales()
        seed_pos_devices(admin)


def seed_pos_devices(admin: StaffUser | None = None) -> int:
    """The ``POS_API_KEYS`` cashier terminals (customer contract §2.10), **only** under the local/test
    settings (``DEV_FEATURES_ALLOWED``; the production settings refuse the variable), so a known demo key
    never reaches a deployed database. Idempotent by key hash; returns how many rows were created."""
    if not settings.DEV_FEATURES_ALLOWED:
        return 0
    return sum(int(services.ensure_pos_device(name, key, admin)) for name, key in settings.POS_API_KEYS)
