"""Demo seeder of the accounts module (design 03 §7.2).

Staff ``admin@glowderma.uz`` (id 1), the twelve design customers, the 420 generated customers whose names and
phones equal the frontend mock (``people.ts``: ``createRandom(4812)`` with the mock's draw order), one active
device per customer, Malika's address (through ``services.add_address`` so ``address_text`` is derived)
and the six soft-deleted customers 427–432. Idempotent by natural keys (staff ``email``, customer ``phone``,
device ``(customer, fid)``). Every bookkeeping timestamp is pinned (design 03 §7.1): a customer and its
device are created at ``registered_at``, the device was last touched at ``last_active_at``, Malika's address
dates from her registration and the deletions from ``DELETED_AT``.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from django.db import transaction

from glowderma.accounts import services
from glowderma.accounts.models import Address, Customer, Device, StaffUser
from glowderma.core import enums, timeutils
from glowderma.core.seeding import SeedContext, register_seeder
from glowderma.core.timeutils import TASHKENT

ADMIN_EMAIL = "admin@glowderma.uz"
ADMIN_FULL_NAME = "Administrator"
DEFAULT_ADMIN_PASSWORD = "glowderma-demo-2026"  # noqa: S105 - demo default, overridable by SEED_ADMIN_PASSWORD

NAMED_CUSTOMERS: tuple[tuple[int, str, str], ...] = (
    (1, "Sherzod Qodirov", "+998973014418"),
    (2, "Aziza Tursunova", "+998935551209"),
    (3, "Gulnora Ismoilova", "+998917072030"),
    (4, "Bekzod Rasulov", "+998904186602"),
    (5, "Malika Karimova", "+998901234567"),
    (6, "Nilufar Saidova", "+998942203175"),
    (7, "Javohir Ergashev", "+998998120561"),
    (8, "Kamola Nazarova", "+998909741823"),
    (9, "Dilshod Mirzayev", "+998936417740"),
    (10, "Sevara Xolmatova", "+998971553912"),
    (11, "Sardor Umarov", "+998993007215"),
    (12, "Nozima Qodirova", "+998938124460"),
)
MALIKA_ID = 5
GENERATED_COUNT = 420
GENERATED_SEED = 4812
DELETED_IDS = range(427, 433)

FEMALE = (
    "Madina", "Shahnoza", "Zarina", "Feruza", "Mohira", "Lola", "Dildora", "Gulchehra", "Munisa",
    "Sabina", "Yulduz", "Nigora", "Ozoda", "Umida", "Barno", "Iroda", "Zilola", "Rayhona", "Shoira",
    "Hulkar", "Mahliyo", "Dilfuza", "Nargiza", "Komila", "Charos",
)  # fmt: skip
MALE = (
    "Aziz", "Bobur", "Otabek", "Rustam", "Ulug‘bek", "Farrux", "Akmal", "Sanjar", "Temur", "Islom",
    "Jamshid", "Doniyor", "Shoxrux", "Abdulla", "Elyor", "Nodir",
)  # fmt: skip
SURNAMES = (
    "Qodirov", "Tursunov", "Ismoilov", "Rasulov", "Karimov", "Saidov", "Ergashev", "Nazarov", "Mirzayev",
    "Xolmatov", "Umarov", "Aliyev", "Yusupov", "Rahimov", "Abdullayev", "Hasanov", "Toshmatov", "Sobirov",
    "Jo‘rayev", "Mahmudov", "Normatov", "Usmonov", "Valiyev", "Ahmedov", "Sultonov", "Bakirov", "Shodmonov",
    "Ibragimov",
)  # fmt: skip
OPERATORS = ("90", "91", "93", "94", "95", "97", "98", "99", "33", "88")

DEVICE_LAST_ACTIVE = datetime(2026, 10, 23, 20, 15, tzinfo=TASHKENT)
DELETED_AT = datetime(2026, 10, 20, 10, 0, tzinfo=TASHKENT)
MALIKA_REGISTERED_AT = datetime(2025, 3, 12, 10, 20, tzinfo=TASHKENT)
MALIKA_LAST_ACTIVE = datetime(2026, 10, 24, 9, 12, tzinfo=TASHKENT)
MALIKA_ADDRESS = "Toshkent, Chilonzor, 9-kvartal"

_MASK = 0xFFFFFFFF


class MockRandom:
    """Python port of the frontend's ``createRandom`` (mulberry32; ``Math.imul`` semantics via 32-bit masks).

    Every intermediate is masked to 32 bits, which is what JS's ``>>> 0`` / ``imul`` / int32 ``^`` amount to.
    """

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> float:
        self.state = (self.state + 0x6D2B79F5) & _MASK
        t = self.state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t ^= (t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296

    def int(self, low: int, high: int) -> int:
        return low + math.floor(self.next() * (high - low + 1))

    def chance(self, probability: float) -> bool:
        return self.next() < probability

    def pick[T](self, items: Sequence[T]) -> T:
        return items[math.floor(self.next() * len(items))]


def generated_customers() -> list[tuple[int, str, str]]:
    """Customers 13–432 exactly as ``people.ts generateCustomers()`` (same draws, same duplicate retry)."""
    random = MockRandom(GENERATED_SEED)
    seen = {name for _, name, _ in NAMED_CUSTOMERS}
    result: list[tuple[int, str, str]] = []
    while len(result) < GENERATED_COUNT:
        female = random.chance(0.78)
        surname = random.pick(SURNAMES)
        given = random.pick(FEMALE if female else MALE)
        name = f"{given} {surname}a" if female else f"{given} {surname}"
        if name in seen:
            continue
        seen.add(name)
        digits = f"{random.int(0, 9_999_999):07d}"
        phone = f"+998{random.pick(OPERATORS)}{digits}"
        result.append((len(NAMED_CUSTOMERS) + len(result) + 1, name, phone))
    return result


def all_customers() -> list[tuple[int, str, str]]:
    return [*NAMED_CUSTOMERS, *generated_customers()]


class CustomerFacts:
    """Per-customer facts from ``createRandom(id × 97)`` in the mock's draw order (``customers.ts``)."""

    def __init__(self, customer_id: int) -> None:
        random = MockRandom(customer_id * 97)
        self.store_total = random.int(0, 60) * 50_000  # consumed here; loyalty's seeder reproduces it
        month = random.int(1, 12)
        day = random.int(1, 28)
        self.registered_at = datetime(2025, month, day, 12, 0, tzinfo=TASHKENT)
        self.language = enums.ContentLanguage.UZ if random.chance(0.75) else enums.ContentLanguage.RU
        if random.chance(0.55):
            self.device = (Device.Platform.IOS, "iPhone 13", "iOS 18.1")
        else:
            self.device = (Device.Platform.ANDROID, "Samsung Galaxy A54", "Android 14")
        self.push_enabled = random.chance(0.8)


@contextmanager
def pinned_clock(instant: datetime) -> Iterator[None]:
    """Run a service at a design instant: ``timeutils.now`` is a module attribute, as the tests patch it."""
    original: Callable[[], datetime] = timeutils.now
    timeutils.now = lambda: instant
    try:
        yield
    finally:
        timeutils.now = original


def ensure_admin() -> StaffUser:
    admin = StaffUser.objects.filter(email=ADMIN_EMAIL).first()
    if admin is None:
        password = os.environ.get("SEED_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
        extra: dict[str, object] = {"full_name": ADMIN_FULL_NAME, "is_staff": True}
        if not StaffUser.objects.filter(pk=1).exists():
            extra["id"] = 1  # every "Administrator" actor of the design dataset is staff id 1
        admin = StaffUser.objects.create_user(ADMIN_EMAIL, password, **extra)
    return admin


def _ensure_customer(customer_id: int, name: str, phone: str) -> Customer:
    existing = Customer.objects.filter(phone=phone).first()
    if existing is not None:
        return existing
    if customer_id == MALIKA_ID:
        registered_at, language = MALIKA_REGISTERED_AT, enums.ContentLanguage.UZ
    else:
        facts = CustomerFacts(customer_id)
        registered_at, language = facts.registered_at, facts.language
    with pinned_clock(registered_at):  # created_at == updated_at == registered_at
        return Customer.objects.create(
            id=customer_id, phone=phone, name=name, language=language, registered_at=registered_at
        )


def _ensure_device(customer: Customer) -> Device:
    fid = f"seed-fid-{customer.pk}"
    device = Device.objects.filter(customer=customer, fid=fid).first()
    if device is not None:
        return device
    if customer.pk == MALIKA_ID:
        platform, model, os_name = Device.Platform.IOS, "iPhone 14", "iOS 18.1"
        push_enabled, last_active_at = True, MALIKA_LAST_ACTIVE
    else:
        facts = CustomerFacts(customer.pk)
        platform, model, os_name = facts.device
        push_enabled, last_active_at = facts.push_enabled, DEVICE_LAST_ACTIVE
    with pinned_clock(last_active_at):  # updated_at == last_active_at; created_at == registration
        return Device.objects.create(
            customer=customer,
            fid=fid,
            platform=platform,
            model=model,
            os=os_name,
            app_version="1.0.0",
            fcm_token=f"seed-fcm-{customer.pk}",
            push_enabled=push_enabled,
            is_active=True,
            last_active_at=last_active_at,
            created_at=customer.registered_at,
        )


@register_seeder("accounts", order=10)
@transaction.atomic
def seed_accounts(context: SeedContext) -> None:
    ensure_admin()
    customers = [_ensure_customer(*row) for row in all_customers()]
    for customer in customers:
        if customer.status == enums.CustomerStatus.ACTIVE:
            _ensure_device(customer)
    malika = Customer.objects.get(pk=MALIKA_ID)
    if not Address.objects.filter(customer=malika, text=MALIKA_ADDRESS).exists():
        with pinned_clock(MALIKA_REGISTERED_AT):
            services.add_address(malika.pk, text=MALIKA_ADDRESS, label="Uy", is_default=True)
    for customer in Customer.objects.filter(pk__in=DELETED_IDS, status=enums.CustomerStatus.ACTIVE):
        with pinned_clock(DELETED_AT):
            services.delete_customer(customer)
