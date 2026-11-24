"""Read side of `payments` (design 02 §2.1 display payment, payments list, dashboard revenue; §2.2 refunds).

Rows leave the module as frozen dataclasses with the contract's field names (``transaction_id`` = the provider
transaction id, ``""`` → ``None``). A selector may traverse ``Payment``'s own FK upward (``order__customer``
for the list and its search, 03 §0.1) but never a reverse relation of an upper module.

One subject per module: ``payment_rows`` (the frozen rows), ``display`` (the display payment and the payment
lookups), ``refund_reads``, ``listing`` (the admin payments list), ``revenue`` (the dashboard) and
``consistency`` (the nightly report). Callers import this facade.
"""

from glowderma.payments.selectors.consistency import (
    click_paid_without_paydoc,
    failed_refunds_needing_action,
    late_captures_not_refunded,
    open_refunds,
    stuck_pending_payments,
)
from glowderma.payments.selectors.display import (
    active_payment,
    display_payment,
    display_rank,
    display_status_subquery,
    get_payment,
    payme_statement,
    payment_by_txn,
    payments_of_order,
    pick_display,
)
from glowderma.payments.selectors.listing import (
    PaymentFilters,
    payments_list,
    search_q,
)
from glowderma.payments.selectors.payment_rows import (
    PaymentRow,
    RefundRow,
    click_paydoc_id,
    payment_row,
    refund_manual_reason_of,
    refund_row,
)
from glowderma.payments.selectors.refund_reads import (
    get_refund,
    refunds_by_payment,
    refunds_for_order,
    refunds_needing_action,
)
from glowderma.payments.selectors.revenue import (
    revenue_buckets,
    revenue_by_day_and_channel,
)

__all__ = [
    "PaymentFilters",
    "PaymentRow",
    "RefundRow",
    "active_payment",
    "click_paid_without_paydoc",
    "click_paydoc_id",
    "display_payment",
    "display_rank",
    "display_status_subquery",
    "failed_refunds_needing_action",
    "get_payment",
    "get_refund",
    "late_captures_not_refunded",
    "open_refunds",
    "payme_statement",
    "payment_by_txn",
    "payment_row",
    "payments_list",
    "payments_of_order",
    "pick_display",
    "refund_manual_reason_of",
    "refund_row",
    "refunds_by_payment",
    "refunds_for_order",
    "refunds_needing_action",
    "revenue_buckets",
    "revenue_by_day_and_channel",
    "search_q",
    "stuck_pending_payments",
]
