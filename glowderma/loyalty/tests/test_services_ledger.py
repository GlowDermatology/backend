"""loyalty.services — post_entry (the only balance writer), accounts, holds and the two-thread concurrency
check (design 02 §1.3–§1.5, §1.9.2)."""

from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event, Thread
from typing import Any

import pytest
from django.db import connection

from glowderma.accounts.tests.factories import CustomerFactory, StaffUserFactory
from glowderma.core import enums, timeutils
from glowderma.core.api.exceptions import InvalidTransition
from glowderma.core.models import AuditEntry
from glowderma.core.seeding import pinned_clock
from glowderma.loyalty import selectors, services
from glowderma.loyalty.models import CashbackHold, LedgerEntry, LoyaltyAccount
from glowderma.loyalty.tests.factories import CashbackPolicyFactory, LoyaltyAccountFactory
from glowderma.loyalty.tests.support import OrderFactory, at, chain

pytestmark = pytest.mark.django_db


# --- accounts and post_entry ---


def test_get_or_create_account_is_lazy_and_idempotent() -> None:
    customer = CustomerFactory.create()
    assert not LoyaltyAccount.objects.filter(customer=customer).exists()
    account_id = services.get_or_create_account(customer.pk)
    assert services.get_or_create_account(customer.pk) == account_id
    account = LoyaltyAccount.objects.get(pk=account_id)
    assert (account.customer_id, account.balance) == (customer.pk, 0)
    assert selectors.account_summary(customer.pk) == selectors.AccountSummary(0, 0, 0)


def test_post_entry_moves_the_balance_and_stores_balance_after() -> None:
    account = LoyaltyAccountFactory.create()
    policy = CashbackPolicyFactory.create(created_at=at(9, 20), effective_from=at(10, 1))
    order = OrderFactory.create(customer=account.customer, paid=True)
    with pinned_clock(at(10, 5, 12)):
        credit = services.post_entry(
            account.pk,
            kind=enums.LedgerKind.CREDIT,
            amount=1_850,
            source_type=enums.LedgerSourceType.ORDER,
            order_id=order.pk,
            source_ref=str(order.pk),
            base=185_000,
            percent=Decimal("1"),
            policy_version=policy.version,
        )
    assert credit == services.PostedEntry(
        entry_id=credit.entry_id,
        account_id=account.pk,
        kind="credit",
        amount=1_850,
        balance_after=1_850,
        at=at(10, 5, 12),
        base=185_000,
        percent=Decimal("1.00"),
        policy_version=policy.version,
        reverses_id=None,
    )
    staff = StaffUserFactory.create()
    adjustment = services.post_entry(
        account.pk,
        kind=enums.LedgerKind.ADJUSTMENT,
        amount=-350,
        source_type=enums.LedgerSourceType.ADMIN,
        reason="Qo‘lda tuzatish",
        actor_id=staff.pk,
        at=at(10, 6),
    )
    assert (adjustment.balance_after, adjustment.at, adjustment.policy_version) == (1_500, at(10, 6), None)
    account.refresh_from_db()
    assert account.balance == 1_500 and account.updated_at >= account.created_at
    entries = {e.pk: e for e in LedgerEntry.objects.all()}
    assert entries[credit.entry_id].policy_id == policy.pk and entries[credit.entry_id].percent == Decimal(
        "1.00"
    )
    assert entries[adjustment.entry_id].actor_id == staff.pk
    assert chain(account.pk) == [("credit", 1_850, 1_850), ("adjustment", -350, 1_500)]
    assert not AuditEntry.objects.filter(action="loyalty.balance_negative").exists()


def test_post_entry_refuses_zero_and_unknown_policy_versions() -> None:
    account = LoyaltyAccountFactory.create()
    with pytest.raises(ValueError, match="zero"):
        services.post_entry(account.pk, kind="credit", amount=0, source_type="order")
    with pytest.raises(services.LoyaltyError, match="Unknown cashback policy version 42"):
        services.post_entry(account.pk, kind="credit", amount=1, source_type="order", policy_version=42)
    assert account.entries.count() == 0 and LoyaltyAccount.objects.get(pk=account.pk).balance == 0


def test_post_entry_flags_the_balance_going_negative_once() -> None:
    staff = StaffUserFactory.create()
    account = LoyaltyAccountFactory.create(balance=0)
    order = OrderFactory.create(
        customer=account.customer, paid=True, cashback_used=1_000, items_subtotal=5_000
    )
    kwargs = dict(source_type=enums.LedgerSourceType.ORDER, order_id=order.pk, source_ref=str(order.pk))
    services.post_entry(account.pk, kind=enums.LedgerKind.DEBIT, amount=-1_000, **kwargs)  # 0 → -1000
    services.post_entry(account.pk, kind=enums.LedgerKind.DEBIT, amount=-500, **kwargs)  # stays negative
    flags = list(AuditEntry.objects.filter(action="loyalty.balance_negative").order_by("id"))
    assert len(flags) == 1
    assert (flags[0].actor_type, flags[0].entity_type, flags[0].entity_id) == ("system", "loyalty_account", str(account.pk))  # fmt: skip  # noqa: E501
    assert flags[0].before == {"balance": 0}
    assert flags[0].after == {
        "customer_id": account.customer_id,
        "balance": -1_000,
        "entry_id": account.entries.order_by("id").first().pk,  # type: ignore[union-attr]
        "kind": "debit",
        "amount": -1_000,
    }
    # back above zero, then below again (with an actor this time) → a second flag naming the staff
    services.post_entry(account.pk, kind="adjustment", amount=2_000, source_type="admin", reason="qaytarish", actor_id=staff.pk)  # fmt: skip  # noqa: E501
    services.post_entry(account.pk, kind="adjustment", amount=-600, source_type="admin", reason="xato", actor_id=staff.pk)  # fmt: skip  # noqa: E501
    second = AuditEntry.objects.filter(action="loyalty.balance_negative").order_by("id").last()
    assert second is not None and second.pk != flags[0].pk
    assert second.after is not None
    assert (second.actor_type, second.actor_id, second.after["balance"]) == ("staff", staff.pk, -100)


@pytest.mark.django_db(transaction=True)
def test_concurrent_postings_keep_balance_equal_to_the_ledger_sum() -> None:
    staff = StaffUserFactory.create()
    account = LoyaltyAccountFactory.create(balance=0)
    customer_id = account.customer_id
    rounds, workers = 8, 3
    barrier = Barrier(workers, timeout=10)
    failures: list[BaseException] = []
    started = Event()

    def work(index: int) -> None:
        try:
            barrier.wait()
            started.set()
            for step in range(rounds):
                amount = 1_000 * (index + 1) + step
                services.adjust(customer_id, "add", amount, f"thread {index} step {step}", staff.pk)
                services.adjust(customer_id, "subtract", amount // 2, f"thread {index} back {step}", staff.pk)
        except BaseException as exc:  # noqa: BLE001 - surfaced through ``failures``
            failures.append(exc)
        finally:
            connection.close()

    threads = [Thread(target=work, args=(i,), daemon=True) for i in range(workers)]
    for thread in threads:
        thread.start()
    assert started.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    entries = list(LedgerEntry.objects.filter(account=account).order_by("at", "id"))
    assert len(entries) == workers * rounds * 2
    expected = sum(
        1_000 * (i + 1) + s - (1_000 * (i + 1) + s) // 2 for i in range(workers) for s in range(rounds)
    )
    balance = LoyaltyAccount.objects.get(pk=account.pk).balance
    assert balance == sum(e.amount for e in entries) == expected
    running = 0
    for entry in entries:  # the balance_after chain is a strict running sum in (at, id) order
        running += entry.amount
        assert entry.balance_after == running
    assert entries[-1].balance_after == balance
    assert selectors.account_summary(customer_id).available == balance


# --- holds ---


def make_order(account: LoyaltyAccount, cashback_used: int = 20_000, **overrides: Any) -> Any:
    return OrderFactory.create(customer=account.customer, cashback_used=cashback_used, items_subtotal=185_000, **overrides)  # fmt: skip  # noqa: E501


def test_create_hold_checks_available_under_the_lock() -> None:
    account = LoyaltyAccountFactory.create(balance=50_000)
    first = make_order(account, 30_000)
    second = make_order(account, 25_000)
    held = services.create_hold(account.customer_id, first.pk, 30_000, first.reservation_expires_at)
    assert (
        held.changed and held.state == "active" and held.amount == 30_000 and held.captured_entry_id is None
    )
    assert selectors.account_summary(account.customer_id) == selectors.AccountSummary(50_000, 30_000, 20_000)
    with pytest.raises(services.InsufficientBalance) as info:
        services.create_hold(account.customer_id, second.pk, 25_000, second.reservation_expires_at)
    assert (info.value.available, info.value.requested) == (20_000, 25_000)
    assert info.value.message == "Balans manfiy bo‘lib qoladi."
    assert [(e.code, str(e)) for e in info.value.errors["amount"]] == [("insufficient_balance", "Balansdan ko‘p ayirib bo‘lmaydi.")]  # fmt: skip  # noqa: E501
    assert CashbackHold.objects.count() == 1
    # exactly the available amount is fine; a replay for the same order is a no-op
    ok = services.create_hold(account.customer_id, second.pk, 20_000, second.reservation_expires_at)
    again = services.create_hold(account.customer_id, second.pk, 20_000, second.reservation_expires_at)
    assert ok.changed and not again.changed and again.hold_id == ok.hold_id
    assert selectors.account_summary(account.customer_id).available == 0
    with pytest.raises(ValueError, match="positive"):
        services.create_hold(account.customer_id, first.pk, 0, first.reservation_expires_at)
    # the balance itself never moved: holds are not ledger entries
    assert LoyaltyAccount.objects.get(pk=account.pk).balance == 50_000 and account.entries.count() == 0


def test_create_hold_creates_the_account_lazily() -> None:
    customer = CustomerFactory.create()
    order = OrderFactory.create(customer=customer, cashback_used=1)
    with pytest.raises(services.InsufficientBalance):
        services.create_hold(customer.pk, order.pk, 1, order.reservation_expires_at)
    # the refused hold rolls the whole transaction back, the lazily created account included
    assert not LoyaltyAccount.objects.filter(customer=customer).exists()
    services.get_or_create_account(customer.pk)
    assert LoyaltyAccount.objects.get(customer=customer).balance == 0


def test_capture_hold_posts_the_debit_and_is_idempotent() -> None:
    account = LoyaltyAccountFactory.create(balance=50_000)
    order = make_order(account, 20_000)
    services.create_hold(account.customer_id, order.pk, 20_000, order.reservation_expires_at)
    paid_at = at(10, 18, 15, 25)
    captured = services.capture_hold(order.pk, at=paid_at)
    assert captured is not None and captured.changed and captured.state == "captured"
    hold = CashbackHold.objects.get(pk=captured.hold_id)
    assert captured.captured_entry_id is not None
    entry = LedgerEntry.objects.get(pk=captured.captured_entry_id)
    assert (hold.captured_entry_id, hold.captured_at) == (entry.pk, paid_at)
    assert (entry.kind, entry.amount, entry.balance_after, entry.source_type, entry.order_id, entry.source_ref, entry.at) == ("debit", -20_000, 30_000, "order", order.pk, str(order.pk), paid_at)  # fmt: skip  # noqa: E501
    # held drops, balance drops by the same amount: available unchanged
    assert selectors.account_summary(account.customer_id) == selectors.AccountSummary(30_000, 0, 30_000)
    replay = services.capture_hold(order.pk)
    assert replay is not None and not replay.changed and replay.captured_entry_id == entry.pk
    assert account.entries.count() == 1
    with pytest.raises(InvalidTransition):
        services.release_hold(order.pk, CashbackHold.ReleaseReason.ORDER_CANCELLED)


def test_release_hold_and_expiry() -> None:
    account = LoyaltyAccountFactory.create(balance=50_000)
    order = make_order(account, 20_000)
    other = make_order(account, 5_000, created_at=timeutils.now() + timedelta(hours=1))
    services.create_hold(account.customer_id, order.pk, 20_000, order.reservation_expires_at)
    services.create_hold(account.customer_id, other.pk, 5_000, other.reservation_expires_at)
    with pytest.raises(ValueError, match="release reason"):
        services.release_hold(order.pk, "because")
    released = services.release_hold(
        order.pk, CashbackHold.ReleaseReason.PAYMENT_FAILED, at=at(10, 2, 14, 50)
    )
    assert released is not None and released.changed and released.state == "released"
    hold = CashbackHold.objects.get(pk=released.hold_id)
    assert (hold.released_at, hold.release_reason, hold.captured_entry_id) == (at(10, 2, 14, 50), "payment_failed", None)  # fmt: skip  # noqa: E501
    assert selectors.account_summary(account.customer_id) == selectors.AccountSummary(50_000, 5_000, 45_000)
    again = services.release_hold(order.pk, CashbackHold.ReleaseReason.ORDER_CANCELLED)
    assert again is not None and not again.changed
    with pytest.raises(InvalidTransition):
        services.capture_hold(order.pk)
    # expire_holds releases only the holds whose expires_at has passed
    assert services.expire_holds(other.reservation_expires_at - timedelta(seconds=1)) == []
    assert services.expire_holds(other.reservation_expires_at) == [other.pk]
    assert services.expire_holds(other.reservation_expires_at) == []
    expired = CashbackHold.objects.get(order_id=other.pk)
    assert (expired.state, expired.release_reason, expired.released_at) == ("released", "reservation_expired", other.reservation_expires_at)  # fmt: skip  # noqa: E501
    assert selectors.account_summary(account.customer_id).held == 0 and account.entries.count() == 0


def test_hold_services_return_none_for_an_order_without_a_hold() -> None:
    order = OrderFactory.create()
    assert services.capture_hold(order.pk) is None
    assert services.release_hold(order.pk, CashbackHold.ReleaseReason.ORDER_CANCELLED) is None
    assert services.expire_holds() == []


def test_create_hold_never_recreates_a_captured_or_released_hold() -> None:
    """Probe P2: an at-least-once replay of checkout + capture must not spend the cashback twice."""
    account = LoyaltyAccountFactory.create(balance=50_000)
    order = make_order(account, 20_000)
    first = services.create_hold(account.customer_id, order.pk, 20_000, order.reservation_expires_at)
    captured = services.capture_hold(order.pk, at=at(10, 18, 15, 25))
    assert captured is not None and captured.changed
    replay = services.create_hold(account.customer_id, order.pk, 20_000, order.reservation_expires_at)
    assert not replay.changed and (replay.hold_id, replay.state) == (first.hold_id, "captured")
    again = services.capture_hold(order.pk)
    assert again is not None and not again.changed
    debits = list(LedgerEntry.objects.filter(order_id=order.pk).values_list("amount", flat=True))
    assert debits == [-20_000] and CashbackHold.objects.filter(order_id=order.pk).count() == 1
    assert selectors.account_summary(account.customer_id) == selectors.AccountSummary(30_000, 0, 30_000)
    # a released hold is terminal: the order was cancelled with it and is never re-paid
    cancelled = make_order(account, 5_000)
    services.create_hold(account.customer_id, cancelled.pk, 5_000, cancelled.reservation_expires_at)
    services.release_hold(cancelled.pk, CashbackHold.ReleaseReason.ORDER_CANCELLED)
    with pytest.raises(InvalidTransition, match="released"):
        services.create_hold(account.customer_id, cancelled.pk, 5_000, cancelled.reservation_expires_at)
    assert CashbackHold.objects.filter(order_id=cancelled.pk).count() == 1
    assert selectors.account_summary(account.customer_id).held == 0


def test_expire_holds_skips_a_hold_captured_meanwhile_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    account = LoyaltyAccountFactory.create(balance=50_000)
    first, second = make_order(account, 10_000), make_order(account, 5_000)
    services.create_hold(account.customer_id, first.pk, 10_000, first.reservation_expires_at)
    services.create_hold(account.customer_id, second.pk, 5_000, second.reservation_expires_at)
    due = max(first.reservation_expires_at, second.reservation_expires_at)
    original = services._lock_account_by_id
    raced: list[int] = []

    def lock_after_the_webhook(account_id: int) -> LoyaltyAccount:
        if not raced:  # the payment webhook captures the first due hold between the listing and its release
            raced.append(first.pk)
            services.capture_hold(first.pk, at=at(10, 18, 15, 25))
        return original(account_id)

    monkeypatch.setattr(services, "_lock_account_by_id", lock_after_the_webhook)
    assert services.expire_holds(due) == [second.pk]
    states = dict(CashbackHold.objects.values_list("order_id", "state"))
    assert states == {first.pk: "captured", second.pk: "released"}
    assert raced == [first.pk] and selectors.account_summary(account.customer_id) == selectors.AccountSummary(40_000, 0, 40_000)  # fmt: skip  # noqa: E501
