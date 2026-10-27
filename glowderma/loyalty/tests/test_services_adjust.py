"""loyalty.services.adjust — contract §2.6 rules (code AND message), available math under the lock, deleted
customers, the ``cashback.adjusted`` audit."""

from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest
from django.db import connection, transaction

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.loyalty import selectors, services
from glowderma.loyalty.models import LedgerEntry, LoyaltyAccount
from glowderma.loyalty.tests.factories import LoyaltyAccountFactory
from glowderma.loyalty.tests.support import OrderFactory, at, chain

pytestmark = pytest.mark.django_db

REASON = "Kassada QR o‘qilmadi, chek S-87011 qo‘lda bog‘landi"


def codes(exc: services.LoyaltyValidationError) -> dict[str, list[tuple[str, str]]]:
    return {field: [(str(e.code), str(e)) for e in details] for field, details in exc.errors.items()}


@pytest.mark.parametrize(
    ("amount", "reason", "expected", "message"),
    [
        ("5", REASON, {"amount": [("invalid", "Summani kiriting.")]}, "Summani kiriting."),
        (True, REASON, {"amount": [("invalid", "Summani kiriting.")]}, "Summani kiriting."),
        (0, REASON, {"amount": [("min_value", "Summani kiriting.")]}, "Summani kiriting."),
        (-5, REASON, {"amount": [("min_value", "Summani kiriting.")]}, "Summani kiriting."),
        (10_000_001, REASON, {"amount": [("max_value", "Summani kiriting.")]}, "Summani kiriting."),
        (100, "  ab ", {"reason": [("min_length", "Kamida 3 ta belgi kiriting.")]}, "Sababni kiriting."),
        (100, "", {"reason": [("min_length", "Kamida 3 ta belgi kiriting.")]}, "Sababni kiriting."),
        (100, None, {"reason": [("min_length", "Kamida 3 ta belgi kiriting.")]}, "Sababni kiriting."),
        (100, "x" * 301, {"reason": [("max_length", "300 belgidan oshmasin.")]}, "Sababni kiriting."),
        # both wrong: every field error is reported, the amount rule decides the top-level message
        (
            0,
            "ab",
            {
                "amount": [("min_value", "Summani kiriting.")],
                "reason": [("min_length", "Kamida 3 ta belgi kiriting.")],
            },
            "Summani kiriting.",
        ),  # fmt: skip  # noqa: E501
    ],
)
def test_adjust_validation_rows(amount: Any, reason: Any, expected: dict[str, Any], message: str) -> None:
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    with pytest.raises(services.AdjustmentValidationError) as info:
        services.adjust(customer.pk, "add", amount, reason, staff.pk)
    assert codes(info.value) == expected and info.value.message == message
    assert not LoyaltyAccount.objects.exists() and not AuditEntry.objects.exists()


def test_adjust_rejects_unknown_direction_before_touching_the_account() -> None:
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    with pytest.raises(ValueError, match="direction"):
        services.adjust(customer.pk, "remove", 100, REASON, staff.pk)
    assert not LoyaltyAccount.objects.exists()


def test_adjust_add_and_subtract_with_available_math_and_audit() -> None:
    staff = StaffUserFactory.create(full_name="Administrator")
    account = LoyaltyAccountFactory.create(balance=60_200)
    customer = account.customer
    request = SimpleNamespace(
        META={"REMOTE_ADDR": "10.0.0.7", "HTTP_USER_AGENT": "pytest"}, request_id="req-1"
    )
    added = services.adjust(customer.pk, "add", 2_950, f"  {REASON}  ", staff.pk, at=at(10, 12, 10, 3), request=request)  # fmt: skip  # noqa: E501
    assert added == services.AdjustmentResult(added.entry_id, customer.pk, account.pk, 63_150, 0, 63_150)
    entry = LedgerEntry.objects.get(pk=added.entry_id)
    assert (entry.kind, entry.source_type, entry.amount, entry.balance_after, entry.reason, entry.actor_id, entry.at) == ("adjustment", "admin", 2_950, 63_150, REASON, staff.pk, at(10, 12, 10, 3))  # fmt: skip  # noqa: E501
    assert (entry.order_id, entry.purchase_id, entry.source_ref, entry.base, entry.percent, entry.policy_id) == (None, None, "", None, None, None)  # fmt: skip  # noqa: E501
    audit = AuditEntry.objects.get(action="cashback.adjusted")
    assert (audit.actor_type, audit.actor_id, audit.actor_label, audit.entity_type, audit.entity_id) == ("staff", staff.pk, "Administrator", "customer", str(customer.pk))  # fmt: skip  # noqa: E501
    assert audit.after == {"direction": "add", "amount": 2_950, "reason": REASON, "balance_after": 63_150, "ledger_entry_id": entry.pk}  # fmt: skip  # noqa: E501
    assert (audit.ip, audit.user_agent, audit.request_id) == ("10.0.0.7", "pytest", "req-1")
    # an active hold reduces what can be subtracted; the check happens under the lock
    order = OrderFactory.create(customer=customer, cashback_used=50_000, items_subtotal=185_000)
    services.create_hold(customer.pk, order.pk, 50_000, order.reservation_expires_at)
    with pytest.raises(services.InsufficientBalance) as info:
        services.adjust(customer.pk, "subtract", 13_151, REASON, staff.pk)
    assert (info.value.available, info.value.requested) == (13_150, 13_151)
    assert codes(info.value) == {"amount": [("insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")]}
    assert info.value.message == "Balans manfiy bo‘lib qoladi."
    subtracted = services.adjust(customer.pk, enums.AdjustmentDirection.SUBTRACT, 13_150, REASON, staff.pk)
    assert (subtracted.balance, subtracted.held, subtracted.available) == (50_000, 50_000, 0)
    assert chain(account.pk) == [("adjustment", 2_950, 63_150), ("adjustment", -13_150, 50_000)]
    assert selectors.account_summary(customer.pk) == selectors.AccountSummary(50_000, 50_000, 0)
    assert AuditEntry.objects.filter(action="cashback.adjusted").count() == 2
    assert not AuditEntry.objects.filter(action="loyalty.balance_negative").exists()


def test_adjust_is_allowed_for_deleted_customers_and_creates_the_account() -> None:
    staff = StaffUserFactory.create()
    deleted = CustomerFactory.create(name="", status=enums.CustomerStatus.DELETED, deleted_at=at(10, 20, 10))
    result = services.adjust(deleted.pk, "add", 1, "qayta ro‘yxatdan o‘tish uchun", staff.pk)
    assert (result.balance, result.available) == (1, 1)
    assert LoyaltyAccount.objects.get(customer=deleted).balance == 1
    with pytest.raises(services.InsufficientBalance):
        services.adjust(deleted.pk, "subtract", 2, "ortiqcha", staff.pk)
    assert services.adjust(deleted.pk, "subtract", 1, "nolga tushirish", staff.pk).balance == 0
    assert selectors.ledger_for_customer(deleted.pk)[0].actor is not None


def test_adjust_boundaries() -> None:
    staff = StaffUserFactory.create()
    customer = CustomerFactory.create()
    top = services.adjust(customer.pk, "add", 10_000_000, "abc", staff.pk)
    assert top.balance == 10_000_000
    assert services.adjust(customer.pk, "subtract", 1, "x" * 300, staff.pk).balance == 9_999_999
    assert LedgerEntry.objects.filter(reason="x" * 300).exists()


@pytest.mark.django_db(transaction=True)
def test_insufficient_balance_is_decided_under_the_account_lock() -> None:
    """Two staff subtract 60 000 from 100 000 at once: the second waits for the lock, then sees 40 000."""
    staff = StaffUserFactory.create()
    account = LoyaltyAccountFactory.create(balance=0)
    customer_id = account.customer_id
    services.adjust(customer_id, "add", 100_000, "boshlang‘ich", staff.pk)
    first_posted, release, second_done = Event(), Event(), Event()
    outcome: dict[str, Any] = {}

    def first() -> None:
        try:
            with transaction.atomic():  # the caller's transaction keeps the account lock until it commits
                outcome["first"] = services.adjust(customer_id, "subtract", 60_000, "birinchi", staff.pk)
                first_posted.set()
                assert release.wait(timeout=10)
        except BaseException as exc:  # noqa: BLE001 - surfaced through ``outcome``
            outcome["first_error"] = exc
            first_posted.set()
        finally:
            connection.close()

    def second() -> None:
        try:
            outcome["second"] = services.adjust(customer_id, "subtract", 60_000, "ikkinchi", staff.pk)
        except BaseException as exc:  # noqa: BLE001 - the expected InsufficientBalance lands here
            outcome["second_error"] = exc
        finally:
            second_done.set()
            connection.close()

    threads = [Thread(target=first, daemon=True), Thread(target=second, daemon=True)]
    threads[0].start()
    assert first_posted.wait(timeout=10) and "first_error" not in outcome
    threads[1].start()
    assert not second_done.wait(timeout=0.5)  # blocked on the LoyaltyAccount row lock
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert outcome["first"].balance == 40_000 and "second" not in outcome
    error = outcome["second_error"]
    assert isinstance(error, services.InsufficientBalance)
    assert (error.available, error.requested) == (40_000, 60_000)
    assert LoyaltyAccount.objects.get(pk=account.pk).balance == 40_000
    assert chain(account.pk) == [("adjustment", 100_000, 100_000), ("adjustment", -60_000, 40_000)]
