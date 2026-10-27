"""The outbox topic ``payments`` produces (design 03 §5): ``refund.submit`` (Click refunds only).

``payments.services.create_refund`` enqueues it; the handler lives in ``commerce/tasks.py`` (it writes the
order's ``refund_*`` events). ``commerce.topics`` re-exports these names.
"""

from __future__ import annotations

REFUND_SUBMIT = "refund.submit"


def refund_submit_key(refund_id: int) -> str:
    return f"{REFUND_SUBMIT}:{refund_id}"
