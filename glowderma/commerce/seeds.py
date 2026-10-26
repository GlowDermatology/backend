"""Demo orders (design 03 §7.8, §7.7 Malika's timeline, 02 §4.5 customer 13), replayed through the services.

Every order is created by :func:`commerce.services.create_order`, paid by ``mark_paid`` (→
``confirm_payment_effects``), expired by ``expire_reservation``, cancelled by ``cancel_order``, moved by
``transition`` / ``apply_bts_status``; the outbox effects ``bts.create_shipment``, ``billz.push_sale``
and ``refund.submit`` are delivered **inline** by calling the ``commerce.tasks`` handler functions under the
pinned clock (their messages are then marked ``done``); ``push.order_status`` messages stay ``pending`` for
the outbox dispatcher (the inbox rows come from ``notifications``' handler, as for the appointment pushes).

One chronological replay: every step of every scenario (order steps, Malika's store sales and adjustment,
the generated customers' store sales of ``loyalty.seeds.seed_store_sales`` at 2026-09-05 12:00) is queued
with its design instant and executed in time order under ``pinned_clock`` — ``loyalty.services.post_entry``
refuses a backdated posting, so every account's ledger must be written in chronological order.

Pinned ids (03 §7.7/§7.8): the ledger and order-event sequences are parked at ≥ 999 before the replay (the
generated rows take ids from 1000); Malika's ledger entries take 100–112 and 120/121 and order №10248's seven
events take 1–7 by moving the sequence to the pinned id around exactly those steps (and back afterwards).
``seed_demo`` re-syncs every sequence at the end.

Scenarios (03 §7.8):

* the fixed design orders №10243–10252, Malika's №10198 / №10231 and her ten pre-policy orders;
* customer 13's deliberate negative balance: №10210 (pickup, paid 10 Oct, credited) is cancelled by the admin
  on 16 Oct after №10215 (14 Oct) spent the whole balance → the reversal of №10210's credit drives the
  balance below zero (L5);
* the 230 generated orders (ids 10001–10242 minus 10198, 10231 and Malika's ten), facts from ``MockRandom
  (1284)`` with the mock's distributions, ``created_at`` interpolated between the anchors in business time
  (09:00–22:00 Tashkent, monotonic with the ids). Status allocation: 10238–10242 ``awaiting_payment``,
  10227–10237 (minus 10231) ``processing``, 10220–10226 ``handed_to_courier``, the multiples of 25 up to 10200
  plus 10210 ``cancelled`` (60 % reservation expiry, the rest admin cancels after payment, refunded), the
  rest ``delivered`` → status counts ``{all 252, awaiting_payment 6, processing 12, handed_to_courier 9,
  delivered 213, cancelled 12}``.

Idempotent by order id: a scenario whose order exists is skipped entirely (store sales by Billz sale id,
Malika's adjustment by its reason). ``keep_open`` moves the open reservations of the seeded
``awaiting_payment`` orders to ``max(created + minutes, now + 7 days)`` so a running expiry beat does not
cancel them right after seeding.
"""

from __future__ import annotations

import functools
import heapq
import itertools
import logging
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from django.contrib.auth import get_user_model
from django.db import connection, transaction

from glowderma.accounts import services as account_services
from glowderma.accounts.models import Address, Customer, StaffUser
from glowderma.accounts.seeds import MALIKA_ID, CustomerFacts
from glowderma.catalog.models import Product
from glowderma.commerce import services, tasks, topics
from glowderma.commerce.models import Order, OrderEvent, StockReservation
from glowderma.core import enums, integration_modes, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.seeding import MockRandom, SeedContext, pinned_clock, register_seeder
from glowderma.core.timeutils import TASHKENT
from glowderma.integrations.bts import fake as fake_bts
from glowderma.integrations.click import fake as fake_click
from glowderma.integrations.payme import fake as fake_payme
from glowderma.loyalty import seeds as loyalty_seeds
from glowderma.loyalty import selectors as loyalty_selectors
from glowderma.loyalty import services as loyalty_services
from glowderma.payments import selectors as payment_selectors

logger = logging.getLogger(__name__)

SEED_ADMIN_EMAIL = "admin@glowderma.uz"
GENERATED_SEED = 1284
GENERATED_ID_FLOOR = 999  # generated ledger entries / order events take ids from 1000
CANCEL_NOTE = "Mijoz so‘rovi bilan"
ADJUSTMENT_REASON = "Kassada QR o‘qilmadi, chek S-87011 qo‘lda bog‘landi"
ADJUSTMENT_AMOUNT = 2_950
CUSTOMER_13 = 13
KEEP_OPEN_DAYS = 7
BUSINESS_START = time(9, 0)
BUSINESS_MINUTES = 13 * 60  # 09:00–22:00 Tashkent
CUSTOMER_13_ORDERS = (10210, 10215)

_S = enums.OrderStatus
DELIVERY = enums.FulfilmentMethod.DELIVERY
PICKUP = enums.FulfilmentMethod.PICKUP
CLICK = enums.PaymentProvider.CLICK
PAYME = enums.PaymentProvider.PAYME
IOS = enums.OrderChannel.IOS
ANDROID = enums.OrderChannel.ANDROID


def dt(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=TASHKENT)


def oct26(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return dt(2026, 10, day, hour, minute, second)


# --- outcomes and plans -------------------------------------------------------------------------------------

AWAITING = "awaiting"
PROCESSING = "processing"
HANDED = "handed"
DELIVERED = "delivered"
EXPIRED = "expired"
ADMIN_CANCEL = "admin_cancel"


@dataclass(slots=True)
class Address_:
    text: str
    region_code: str = "10"
    city_code: str = "1001"


@dataclass(slots=True)
class OrderPlan:
    """One order scenario: its checkout facts, its outcome and the instants of every step."""

    id: int
    customer_id: int
    created: datetime
    lines: list[tuple[str, int]]  # (billz id, quantity) in cart order
    fulfilment: str
    provider: str
    outcome: str
    channel: str = IOS
    address: Address_ | None = None
    cashback: int = 0  # fixed cashback_used
    cashback_draw: int = 0  # generated: min(draw, available at checkout) when > 0
    paid_after: timedelta = timedelta(minutes=3)
    bts_after: timedelta = timedelta(minutes=4)  # from created (BTS create = paid + 1 min)
    handed_at: datetime | None = None
    delivered_at: datetime | None = None
    cancel_at: datetime | None = None
    refund_at: datetime | None = None
    bts_after_cancel: bool = False  # №10244: the create message is delivered only after the cancel
    fake: dict[str, dict[str, Any]] = field(default_factory=dict)
    billz_order_uuid: UUID | None = None
    pinned_events: bool = False  # №10248: events 1–7
    ledger_ids: dict[str, int] = field(default_factory=dict)  # step → first pinned ledger id (Malika)
    dynamic_cashback: Callable[[], int] | None = None  # customer 13's №10215: the whole balance

    @property
    def paid_at(self) -> datetime:
        return self.created + self.paid_after

    @property
    def is_paid(self) -> bool:
        return self.outcome in (PROCESSING, HANDED, DELIVERED, ADMIN_CANCEL)


def _txn_id(plan: OrderPlan) -> str:
    if plan.provider == CLICK:
        return fake_click.transaction_id(plan.id, plan.fake.get("click"))
    return fake_payme.transaction_id(plan.id, plan.fake.get("payme"))


def _pay(plan: OrderPlan) -> None:
    """``mark_paid`` of the order's pending payment (M2 signature); a Click payment gets its
    ``click_paydoc_id`` first, as a Prepare would store it (refunds are keyed by it, customer contract
    §2.13.0)."""
    pending = payment_selectors.active_payment(plan.id, enums.PaymentStatus.PENDING)
    if pending is None:
        return
    raw = (
        {"click_paydoc_id": fake_click.paydoc_id(plan.id, plan.fake.get("click"))}
        if plan.provider == CLICK
        else None
    )
    services.mark_paid(plan.id, pending.id, plan.provider, _txn_id(plan), raw=raw)


def _delivery_fee(plan: OrderPlan) -> int:
    """The fee the seeder passes to ``create_order``: the design's fake BTS tariff of the address region
    (``scenario["tariff"]`` wins), read from the data — never through the port, which refuses every quote
    while BTS is fake under the production settings (``seed_demo --force`` on staging)."""
    if plan.fulfilment != DELIVERY or plan.address is None:
        return 0
    scenario = plan.fake.get("bts")
    tariff = scenario.get("tariff") if isinstance(scenario, dict) else None
    if isinstance(tariff, int):
        return tariff
    return fake_bts.TARIFFS.get(plan.address.region_code, fake_bts.DEFAULT_TARIFF)


# --- the fixed design orders (03 §7.8) ----------------------------------------------------------------------

MALIKA_ADDRESS_10248 = Address_("Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon")
BILLZ_UUID_10248 = UUID("0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10")


def fixed_orders() -> list[OrderPlan]:
    return [
        OrderPlan(
            10252,
            1,
            oct26(24, 11, 42),
            [("4607012345", 1), ("4607012399", 1), ("4607012412", 1), ("4607012415", 1)],
            DELIVERY,
            PAYME,
            AWAITING,
            address=Address_("Samarqand, Registon ko‘chasi, 15-uy", "13", "1301"),
        ),
        OrderPlan(
            10251,
            2,
            oct26(24, 11, 20),
            [("4607012399", 1), ("4607012410", 1), ("4607012416", 1), ("4607012417", 1)],
            PICKUP,
            CLICK,
            PROCESSING,
            channel=ANDROID,
        ),
        OrderPlan(
            10250,
            3,
            oct26(24, 10, 5),
            [("4607012401", 1), ("4607012416", 1)],
            DELIVERY,
            PAYME,
            PROCESSING,
            address=Address_("Guliston, Mustaqillik ko‘chasi, 8-uy", "12", "1201"),
            fake={"bts": {"fail_attempts": 1}},
        ),
        OrderPlan(
            10249,
            4,
            oct26(23, 18, 30),
            [("4607012412", 1)],
            DELIVERY,
            CLICK,
            EXPIRED,
            channel=ANDROID,
            address=Address_("Toshkent viloyati, Chirchiq, Amir Temur ko‘chasi, 21-uy", "11", "1101"),
        ),
        OrderPlan(
            10248,
            MALIKA_ID,
            oct26(23, 14, 2),
            [("4607012345", 1), ("4607012401", 1), ("4607012388", 2)],
            DELIVERY,
            CLICK,
            HANDED,
            address=MALIKA_ADDRESS_10248,
            cashback=48_600,
            handed_at=oct26(23, 16, 40),
            fake={"click": {"transaction_id": "CLK-7741920385"}, "bts": {"tracking_number": "48213907"}},
            billz_order_uuid=BILLZ_UUID_10248,
            pinned_events=True,
            ledger_ids={"pay": 120},
        ),
        OrderPlan(
            10247,
            6,
            oct26(23, 12, 15),
            [("4607012301", 1), ("4607012345", 1), ("4607012422", 1), ("4607012416", 1)],
            PICKUP,
            PAYME,
            DELIVERED,
            delivered_at=oct26(23, 17, 15),
        ),
        OrderPlan(
            10246,
            7,
            oct26(23, 9, 48),
            [("4607012415", 2), ("4607012345", 2), ("4607012399", 1), ("4607012417", 1)],
            DELIVERY,
            PAYME,
            DELIVERED,
            address=Address_("Toshkent, Yunusobod tumani, 4-kvartal, 17-uy"),
            cashback=20_000,
            handed_at=oct26(23, 12, 26),
            delivered_at=oct26(24, 10, 12),
            fake={"bts": {"tracking_number": "48211544"}},
        ),
        OrderPlan(
            10245,
            8,
            oct26(22, 19, 2),
            [("4607012399", 1), ("4607012416", 1), ("4607012417", 1)],
            DELIVERY,
            CLICK,
            HANDED,
            address=Address_("Toshkent, Mirzo Ulug‘bek tumani, Buyuk Ipak yo‘li, 63-uy"),
            handed_at=oct26(23, 11, 30),
            fake={"bts": {"tracking_number": "48209981"}},
        ),
        OrderPlan(
            10244,
            9,
            oct26(22, 16, 37),
            [("4607012411", 1), ("4607012416", 1)],
            DELIVERY,
            CLICK,
            ADMIN_CANCEL,
            address=Address_("Toshkent, Yakkasaroy tumani, Shota Rustaveli ko‘chasi, 40-uy"),
            cancel_at=oct26(22, 17, 37),
            refund_at=oct26(22, 17, 39),
            bts_after_cancel=True,
        ),
        OrderPlan(
            10243,
            10,
            oct26(22, 13, 10),
            [("4607012401", 1), ("4607012345", 1), ("4607012411", 1), ("4607012418", 1)],
            DELIVERY,
            PAYME,
            DELIVERED,
            address=Address_("Toshkent, Shayxontohur tumani, Navoiy ko‘chasi, 12-uy"),
            handed_at=oct26(22, 15, 48),
            delivered_at=oct26(23, 15, 5),
            fake={"bts": {"tracking_number": "48207320"}},
        ),
        OrderPlan(
            10231,
            MALIKA_ID,
            oct26(18, 15, 20),
            [("4607012301", 1), ("4607012416", 2), ("4607012417", 1), ("4607012401", 1)],
            PICKUP,
            PAYME,
            DELIVERED,
            cashback=20_000,
            paid_after=timedelta(minutes=5),
            delivered_at=oct26(18, 20, 25),
            ledger_ids={"pay": 110},
        ),
        OrderPlan(
            10198,
            MALIKA_ID,
            oct26(2, 14, 31),
            [("4607012422", 1)],
            DELIVERY,
            CLICK,
            ADMIN_CANCEL,
            address=MALIKA_ADDRESS_10248,
            cancel_at=oct26(2, 16, 10),
            refund_at=oct26(2, 16, 12),
            fake={"bts": {"fail_attempts": 1}},
            ledger_ids={"pay": 106, "cancel": 107},
        ),
    ]


# Malika's ten pre-policy orders (03 §7.7): delivered pickups, interpolated dates (all before June 2026).
PRE_POLICY_ORDERS: tuple[tuple[int, tuple[tuple[str, int], ...]], ...] = (
    (10012, (("4607012345", 1),)),
    (10026, (("4607012301", 1),)),
    (10040, (("4607012399", 1),)),
    (10054, (("4607012422", 1), ("4607012416", 1))),
    (10068, (("4607012412", 1),)),
    (10082, (("4607012415", 1),)),
    (10096, (("4607012410", 1),)),
    (10110, (("4607012411", 1),)),
    (10124, (("4607012419", 1), ("4607012417", 1))),
    (10138, (("4607012301", 1), ("4607012422", 1))),
)
MALIKA_ORDER_IDS = frozenset({10198, 10231, 10248, *(order_id for order_id, _ in PRE_POLICY_ORDERS)})

# Malika's store sales (03 §7.7): (Billz sale id, sold at, amount, pinned ledger id | None = pre-policy).
MALIKA_STORE_SALES: tuple[tuple[str, datetime, int, int | None], ...] = (
    ("S-60112", dt(2025, 4, 18, 12, 0), 1_240_000, None),
    ("S-61350", dt(2025, 8, 3, 12, 0), 985_000, None),
    ("S-62877", dt(2025, 12, 12, 12, 0), 864_000, None),
    ("S-63905", dt(2026, 3, 7, 12, 0), 600_000, None),
    ("S-64998", dt(2026, 6, 1, 9, 30), 361_000, 100),
    ("S-65230", dt(2026, 6, 3, 10, 55), 1_245_000, 101),
    ("S-68011", dt(2026, 6, 20, 15, 18), 987_000, 102),
    ("S-71840", dt(2026, 7, 11, 11, 42), 1_530_000, 103),
    ("S-76102", dt(2026, 8, 2, 17, 5), 621_000, 104),
    ("S-81377", dt(2026, 9, 14, 12, 20), 864_000, 105),
    ("S-86540", dt(2026, 10, 9, 13, 40), 412_000, 108),
    ("S-88213", dt(2026, 10, 21, 11, 47), 231_000, 112),
)
MALIKA_ADJUSTMENT_AT = dt(2026, 10, 12, 10, 3)
MALIKA_ADJUSTMENT_LEDGER_ID = 109

# --- created_at interpolation (03 §7.8) ---------------------------------------------------------------------

ANCHORS: tuple[tuple[int, datetime], ...] = (
    (10000, dt(2025, 3, 20, 10, 0)),
    (10198, oct26(2, 14, 31)),
    (10210, oct26(10, 11, 0)),
    (10215, oct26(14, 12, 40)),
    (10231, oct26(18, 15, 20)),
    (10243, oct26(22, 13, 10)),
)
_EPOCH = date(2025, 3, 20)


def _to_business(instant: datetime) -> float:
    local = instant.astimezone(TASHKENT)
    minutes = local.hour * 60 + local.minute + local.second / 60 - (BUSINESS_START.hour * 60)
    return (local.date() - _EPOCH).days * BUSINESS_MINUTES + minutes


def _from_business(value: float) -> datetime:
    whole = math.floor(value)
    day, minute = divmod(whole, BUSINESS_MINUTES)
    start = datetime.combine(_EPOCH + timedelta(days=day), BUSINESS_START, tzinfo=TASHKENT)
    return start + timedelta(minutes=minute)


def interpolated(order_id: int, jitter: float = 0.0) -> datetime:
    """``created_at`` of a non-fixed order: linear in business minutes between the anchors around it; a
    ``jitter`` in ``[-0.5, 0.5)`` moves it by at most 30 % of the step, so the ids stay monotonic in time."""
    for (low_id, low_at), (high_id, high_at) in itertools.pairwise(ANCHORS):
        if low_id <= order_id <= high_id:
            low, high = _to_business(low_at), _to_business(high_at)
            step = (high - low) / (high_id - low_id)
            return _from_business(low + step * (order_id - low_id + 0.6 * jitter))
    raise ValueError(f"No anchor covers order {order_id}")


# --- generated orders (03 §7.8) -----------------------------------------------------------------------------

DISTRICTS: tuple[str, ...] = (
    "Yunusobod tumani",
    "Mirzo Ulug‘bek tumani",
    "Chilonzor tumani",
    "Yakkasaroy tumani",
    "Shayxontohur tumani",
    "Olmazor tumani",
    "Uchtepa tumani",
    "Sergeli tumani",
    "Mirobod tumani",
    "Yashnobod tumani",
    "Bektemir tumani",
)
TRACKING_START = 48_207_000  # generated tracking numbers count down from here (newest order first)
POLICY_START = dt(2026, 6, 1)


def generated_ids() -> list[int]:
    return [order_id for order_id in range(10001, 10243) if order_id not in MALIKA_ORDER_IDS]


def generated_outcome(order_id: int) -> str:
    if 10238 <= order_id <= 10242:
        return AWAITING
    if 10227 <= order_id <= 10237:
        return PROCESSING
    if 10220 <= order_id <= 10226:
        return HANDED
    if order_id == 10210 or (order_id % 25 == 0 and order_id <= 10200):
        return ADMIN_CANCEL  # the expiry share is decided by the draw
    return DELIVERED


def _registered(customer_id: int) -> datetime:
    if customer_id == MALIKA_ID:
        return dt(2025, 3, 12, 10, 20)
    return CustomerFacts(customer_id).registered_at


def generated_orders(products: list[str]) -> list[OrderPlan]:
    """The 230 non-fixed orders (incl. customer 13's two), drawn from ``MockRandom(1284)`` in id order.

    Draw order per id: jitter, customer, delivery (75 %), line count 1–4, the products (distinct, active and
    in stock), quantity 2 with 15 % per line, Click 50 %, iOS 55 %, fee ``int(18, 35) × 1000``, district and
    house, cashback use (15 %) and its amount ``int(50, 500) × 100``, the expiry share of a cancelled order
    (60 %). Every draw happens for every id so the stream never depends on the outcome.
    """
    rng = MockRandom(GENERATED_SEED)
    registered = {customer_id: _registered(customer_id) for customer_id in range(1, 427)}
    candidates = [cid for cid in range(1, 427) if cid not in (MALIKA_ID, CUSTOMER_13)]
    plans: list[OrderPlan] = []
    for order_id in generated_ids():
        created = interpolated(order_id, rng.next() - 0.5)
        eligible = [cid for cid in candidates if registered[cid] < created] or candidates
        customer_id = rng.pick(eligible)
        delivery = rng.chance(0.75)
        count = rng.int(1, 4)
        chosen: list[str] = []
        while len(chosen) < count:
            billz_id = rng.pick(products)
            if billz_id not in chosen:
                chosen.append(billz_id)
        lines = [(billz_id, 2 if rng.chance(0.15) else 1) for billz_id in chosen]
        provider = CLICK if rng.chance(0.5) else PAYME
        channel = IOS if rng.chance(0.55) else ANDROID
        fee = rng.int(18, 35) * 1000
        district = rng.pick(DISTRICTS)
        quarter, house = rng.int(1, 20), rng.int(1, 60)
        use_cashback = rng.chance(0.15)
        cashback_amount = rng.int(50, 500) * 100
        expired = rng.chance(0.6)
        outcome = generated_outcome(order_id)
        if outcome == HANDED:
            delivery = True  # a pickup order is never handed to a courier
        if outcome == ADMIN_CANCEL and expired and order_id != 10210:
            outcome = EXPIRED
        if order_id in CUSTOMER_13_ORDERS:
            customer_id, delivery, provider = CUSTOMER_13, False, CLICK
        plan = OrderPlan(
            order_id,
            customer_id,
            created,
            lines,
            DELIVERY if delivery else PICKUP,
            provider,
            outcome,
            channel=channel,
        )
        if delivery:
            city = f"10{DISTRICTS.index(district) + 1:02d}"
            plan.address = Address_(f"Toshkent, {district}, {quarter}-kvartal, {house}-uy", "10", city)
            plan.fake["bts"] = {"tariff": fee}
        named = customer_id <= 12
        if use_cashback and plan.is_paid and created >= POLICY_START and not named:
            plan.cashback_draw = cashback_amount
        if outcome in (HANDED, DELIVERED) and delivery:
            plan.handed_at = created + timedelta(minutes=158)
        if outcome == DELIVERED:
            plan.delivered_at = created + (timedelta(hours=26) if delivery else timedelta(hours=5))
        if outcome == ADMIN_CANCEL:
            plan.cancel_at = created + timedelta(minutes=60)
            plan.refund_at = created + timedelta(minutes=62 if provider == CLICK else 120)
        plans.append(plan)
    _customer_13(plans)
    _assign_tracking(plans)
    return plans


def _customer_13(plans: list[OrderPlan]) -> None:
    """02 §4.5 item 4: №10210 paid 10 Oct (credit), cancelled 16 Oct after №10215 (14 Oct) spent it all."""
    by_id = {plan.id: plan for plan in plans}
    order_a, order_b = by_id[10210], by_id[10215]
    order_a.outcome = ADMIN_CANCEL
    order_a.cancel_at = oct26(16, 11, 30)
    order_a.refund_at = oct26(16, 11, 32)
    order_a.cashback_draw = 0
    order_b.outcome = DELIVERED
    order_b.cashback_draw = 0
    order_b.handed_at = None
    order_b.delivered_at = order_b.created + timedelta(hours=5)
    order_b.dynamic_cashback = lambda: max(0, loyalty_selectors.account_summary(CUSTOMER_13).available)


def _assign_tracking(plans: list[OrderPlan]) -> None:
    """Paid delivery orders get a BTS parcel: tracking numbers count down from 48207000, newest first."""
    numbered = [plan for plan in plans if plan.fulfilment == DELIVERY and plan.is_paid]
    for offset, plan in enumerate(sorted(numbered, key=lambda p: p.id, reverse=True)):
        plan.fake.setdefault("bts", {})["tracking_number"] = str(TRACKING_START - offset)


def malika_pre_policy_orders() -> list[OrderPlan]:
    plans = []
    for order_id, lines in PRE_POLICY_ORDERS:
        created = interpolated(order_id)
        plans.append(
            OrderPlan(
                order_id,
                MALIKA_ID,
                created,
                list(lines),
                PICKUP,
                CLICK if order_id % 2 else PAYME,
                DELIVERED,
                delivered_at=created + timedelta(hours=5),
            )
        )
    return plans


# --- the replay ---------------------------------------------------------------------------------------------


@contextmanager
def pinned_sequence(table: str, next_id: int | None) -> Iterator[None]:
    """Make the next id of ``table`` be ``next_id`` inside the block, then put the sequence back."""
    if next_id is None:
        yield
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
        sequence = cursor.fetchone()[0]
        cursor.execute(f"SELECT last_value, is_called FROM {sequence}")  # noqa: S608 - name from the catalog
        last_value, is_called = cursor.fetchone()
        cursor.execute("SELECT setval(%s, %s, false)", [sequence, next_id])
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT setval(%s, %s, %s)", [sequence, last_value, is_called])


def park_sequence(table: str, floor: int) -> None:
    """Move ``table``'s sequence to ``max(MAX(id), floor)`` so generated rows take ids above ``floor``."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_get_serial_sequence(%s, 'id')", [table])
        sequence = cursor.fetchone()[0]
        cursor.execute(f'SELECT GREATEST(COALESCE(MAX("id"), 0), %s) FROM "{table}"', [floor])  # noqa: S608
        value = int(cursor.fetchone()[0])
        cursor.execute("SELECT setval(%s, %s, true)", [sequence, value])


@dataclass(order=True)
class _Step:
    at: datetime
    seq: int
    action: Callable[[], object] = field(compare=False)
    ledger_id: int | None = field(default=None, compare=False)
    events_of: int | None = field(default=None, compare=False)  # pin this order's event ids from 1


class Replay:
    """A time-ordered queue of steps, each run under ``pinned_clock(at)``."""

    def __init__(self) -> None:
        self._heap: list[_Step] = []
        self._counter = itertools.count()

    def add(
        self,
        at: datetime,
        action: Callable[[], object],
        *,
        ledger_id: int | None = None,
        events_of: int | None = None,
    ) -> None:
        heapq.heappush(self._heap, _Step(at, next(self._counter), action, ledger_id, events_of))

    def run(self) -> int:
        count = 0
        while self._heap:
            step = heapq.heappop(self._heap)
            events_next = None
            if step.events_of is not None:
                events_next = OrderEvent.objects.filter(order_id=step.events_of).count() + 1
            with (
                pinned_clock(step.at),
                pinned_sequence(loyalty_selectors.ledger_table(), step.ledger_id),
                pinned_sequence(OrderEvent._meta.db_table, events_next),
            ):
                step.action()
            count += 1
        return count


def _deliver(topic: str, order_id: int, handler: Callable[[dict[str, Any]], None]) -> None:
    """Run the handler of every pending ``topic`` message of the order inline, then mark it ``done``."""
    messages = OutboxMessage.objects.filter(
        topic=topic, status="pending", payload__order_id=order_id
    ).order_by("id")
    for message in list(messages):
        handler(message.payload)
        OutboxMessage.objects.filter(pk=message.pk).update(status="done", updated_at=timeutils.now())


class Seeder:
    def __init__(self, context: SeedContext, admin: StaffUser) -> None:
        self.context = context
        self.admin = admin
        self.replay = Replay()
        self.products = {p.billz_id: p.pk for p in Product.objects.only("id", "billz_id")}
        self.existing = set(Order.objects.values_list("id", flat=True))

    # --- order scenarios ---

    def schedule(self, plan: OrderPlan) -> None:
        if plan.id in self.existing:
            return
        if any(billz_id not in self.products for billz_id, _ in plan.lines):
            return
        if not Customer.objects.filter(pk=plan.customer_id).exists():
            return
        events_of = plan.id if plan.pinned_events else None
        add = self.replay.add
        add(plan.created, lambda: self._create(plan), events_of=events_of)
        if plan.outcome == EXPIRED:
            add(
                plan.created + timedelta(minutes=15),
                lambda: services.expire_reservation(plan.id),
                events_of=events_of,
            )
            return
        if plan.outcome == AWAITING:
            return
        add(
            plan.paid_at,
            lambda: _pay(plan),
            ledger_id=plan.ledger_ids.get("pay"),
            events_of=events_of,
        )
        add(
            plan.paid_at + timedelta(seconds=20),
            lambda: _deliver(topics.BILLZ_PUSH_SALE, plan.id, tasks.push_sale),
            events_of=events_of,
        )
        if plan.fulfilment == DELIVERY:
            bts_at = plan.created + plan.bts_after
            if plan.bts_after_cancel and plan.cancel_at is not None:
                bts_at = plan.cancel_at + timedelta(seconds=30)
            add(
                bts_at,
                lambda: _deliver(topics.BTS_CREATE_SHIPMENT, plan.id, tasks.create_shipment),
                events_of=events_of,
            )
        if plan.handed_at is not None:
            add(
                plan.handed_at,
                lambda: services.apply_bts_status(plan.id, "200", enums.ShipmentVia.WEBHOOK),
                events_of=events_of,
            )
        if plan.delivered_at is not None:
            if plan.fulfilment == DELIVERY:
                add(
                    plan.delivered_at,
                    lambda: services.apply_bts_status(plan.id, "300", enums.ShipmentVia.WEBHOOK),
                )
            else:
                add(plan.delivered_at, lambda: services.transition(plan.id, _S.DELIVERED, self.admin.pk))
        if plan.cancel_at is not None:
            add(
                plan.cancel_at,
                lambda: services.cancel_order(plan.id, CANCEL_NOTE, self.admin.pk),
                ledger_id=plan.ledger_ids.get("cancel"),
            )
        if plan.refund_at is not None:
            add(plan.refund_at, lambda: self._refund(plan))

    def _create(self, plan: OrderPlan) -> None:
        cashback = plan.cashback
        if plan.dynamic_cashback is not None:
            cashback = plan.dynamic_cashback()
        elif plan.cashback_draw:
            cashback = min(
                plan.cashback_draw, max(0, loyalty_selectors.account_summary(plan.customer_id).available)
            )
        address = plan.address
        if address is not None and plan.customer_id <= 12 and plan.customer_id != MALIKA_ID:
            if not Address.objects.filter(customer_id=plan.customer_id).exists():
                account_services.add_address(
                    plan.customer_id,
                    text=address.text,
                    region_code=address.region_code,
                    city_code=address.city_code,
                    is_default=True,
                )
        services.create_order(
            plan.customer_id,
            [(self.products[billz_id], quantity) for billz_id, quantity in plan.lines],
            plan.fulfilment,
            payment_provider=plan.provider,
            channel=plan.channel,
            delivery_fee=_delivery_fee(plan),
            address_text=address.text if address else "",
            region_code=address.region_code if address else "",
            city_code=address.city_code if address else "",
            cashback_used=cashback,
            metadata={"seed": True, "fake": plan.fake} if plan.fake else {"seed": True},
            order_id=plan.id,
            billz_order_uuid=plan.billz_order_uuid,
            enforce_stock=False,
        )

    def _refund(self, plan: OrderPlan) -> None:
        if plan.provider == CLICK:
            _deliver(topics.REFUND_SUBMIT, plan.id, tasks.submit_refund)
            return
        for refund in payment_selectors.refunds_for_order(plan.id):
            if refund.state == enums.RefundState.MANUAL_REQUIRED:
                # stands for Payme's CancelTransaction after the cabinet refund (02 §2.2)
                services.resolve_refund(refund.id, enums.RefundState.SUCCEEDED, None)

    # --- Malika's store sales and adjustment, the generated customers' store sales ---

    def schedule_malika(self) -> None:
        if not Customer.objects.filter(pk=MALIKA_ID).exists():
            return
        present = loyalty_selectors.store_sale_ids(MALIKA_ID)
        for sale_id, sold_at, amount, ledger_id in MALIKA_STORE_SALES:
            if sale_id in present:
                continue
            self.replay.add(
                sold_at,
                functools.partial(_store_sale, MALIKA_ID, sale_id, amount, sold_at),
                ledger_id=ledger_id,
            )
        if not loyalty_selectors.has_adjustment(MALIKA_ID, ADJUSTMENT_REASON):
            self.replay.add(
                MALIKA_ADJUSTMENT_AT,
                lambda: loyalty_services.adjust(
                    MALIKA_ID,
                    enums.AdjustmentDirection.ADD,
                    ADJUSTMENT_AMOUNT,
                    ADJUSTMENT_REASON,
                    self.admin.pk,
                ),
                ledger_id=MALIKA_ADJUSTMENT_LEDGER_ID,
            )

    def schedule_store_sales(self) -> None:
        self.replay.add(loyalty_seeds.STORE_SALE_SOLD_AT, loyalty_seeds.seed_store_sales)

    # --- run ---

    def run(self) -> int:
        products = list(
            Product.objects.filter(status=enums.ProductStatus.ACTIVE, stock__quantity__gt=0)
            .order_by("id")
            .values_list("billz_id", flat=True)
        )
        self.schedule_malika()
        self.schedule_store_sales()
        for plan in [*fixed_orders(), *malika_pre_policy_orders(), *generated_orders(products)]:
            self.schedule(plan)
        park_sequence(loyalty_selectors.ledger_table(), GENERATED_ID_FLOOR)
        park_sequence(OrderEvent._meta.db_table, GENERATED_ID_FLOOR)
        steps = self.replay.run()
        if self.context.keep_open:
            keep_open(timeutils.now())
        return steps


def _store_sale(customer_id: int, sale_id: str, amount: int, sold_at: datetime) -> None:
    loyalty_services.record_store_purchase(customer_id, sale_id, sale_id, amount, sold_at, at=sold_at)


def keep_open(now: datetime) -> int:
    """Seeded unpaid orders keep their reservations ``max(created + minutes, now + 7 days)`` (03 §7.1)."""
    moved = 0
    floor = now + timedelta(days=KEEP_OPEN_DAYS)
    for order in Order.objects.filter(status=_S.AWAITING_PAYMENT, metadata__seed=True):
        expires = max(order.created_at + timedelta(minutes=order.reservation_minutes), floor)
        Order.objects.filter(pk=order.pk).update(reservation_expires_at=expires)
        StockReservation.objects.filter(order=order, state=StockReservation.State.ACTIVE).update(
            expires_at=expires
        )
        moved += 1
    return moved


MESSAGE_SEEDED_BEFORE_ORDERS = (
    "the database holds loyalty ledger rows but none of the demo orders (seeded before the orders slice "
    "existed, or ledger rows from seed_store_sales or manual QA); replaying the orders pins ledger ids that "
    "such rows may already hold. Run `manage.py seed_demo --reset` (it empties the database first) or seed "
    "a fresh database."
)


def preflight() -> str | None:
    """Refuse a database seeded before this seeder existed: loyalty rows present, demo orders absent (the
    replay pins ledger ids 100–121 that its earlier store sales already took — a duplicate-key failure)."""
    if loyalty_selectors.has_ledger_entries() and not Order.objects.filter(pk__in=MALIKA_ORDER_IDS).exists():
        return MESSAGE_SEEDED_BEFORE_ORDERS
    return None


@register_seeder("commerce", order=70, preflight=preflight)
def seed_commerce(context: SeedContext) -> None:
    admin: StaffUser | None = get_user_model().objects.filter(email=SEED_ADMIN_EMAIL).first()
    if not integration_modes.dev_features_allowed():
        # Under the production settings (staging, ``seed_demo --force``) the demo orders cannot be replayed:
        # their effects run through the provider ports inline, which are either live (real parcels, real
        # reversals) or fakes that report no effect (no parcel, no Billz sale, customer contract §2.14.3,
        # §6.14). The other modules' demo data and loyalty's store sales are still seeded.
        logger.warning("seed.commerce_orders_skipped %s", {"reason": "production settings"})
        loyalty_seeds.seed_store_sales()
        return
    if admin is None or not Product.objects.exists() or not Customer.objects.exists():
        # standalone run without the lower layers: still record the store sales loyalty hands to us
        loyalty_seeds.seed_store_sales()
        return
    with transaction.atomic():
        Seeder(context, admin).run()
