"""Fill ``payments.Refund.manual_reason`` (payments.0004) for the refunds that existed before the column.

Commerce owns the evidence: the ``refund_requested`` order event records ``manual`` and, since M2,
``manual_reason``. A refund gets a reason when it is a Payme refund (always a cabinet refund: ``payme``), is
``manual_required`` now, or its ``refund_requested`` event says it was manual. The reason is the event's when
it has one; otherwise Payme → ``payme`` and Click → ``no_paydoc`` without a ``click_paydoc_id``, else
``fake_mode`` (the only other reason a Click refund is manual: ``commerce.services.refund_manual_reason``).
Rows that already hold a reason are left alone; reversing does nothing (0004's reversal drops the column).
"""

from django.db import migrations

BACKFILL = """
UPDATE payments_refund AS f
SET manual_reason = CASE
    WHEN p.provider = 'payme' THEN 'payme'
    ELSE COALESCE(
        (
            SELECT NULLIF(e.data ->> 'manual_reason', '')
            FROM commerce_orderevent AS e
            WHERE e.order_id = p.order_id
              AND e.type = 'refund_requested'
              AND e.data ->> 'refund_id' = f.id::text
              AND NULLIF(e.data ->> 'manual_reason', '') IN ('no_paydoc', 'fake_mode')
            ORDER BY e.id
            LIMIT 1
        ),
        CASE
            WHEN COALESCE(p.provider_state ->> 'click_paydoc_id', '') = '' THEN 'no_paydoc'
            ELSE 'fake_mode'
        END
    )
END
FROM payments_payment AS p
WHERE f.payment_id = p.id
  AND f.manual_reason = ''
  AND (
    p.provider = 'payme'
    OR f.state = 'manual_required'
    OR EXISTS (
        SELECT 1
        FROM commerce_orderevent AS e
        WHERE e.order_id = p.order_id
          AND e.type = 'refund_requested'
          AND e.data ->> 'refund_id' = f.id::text
          AND e.data ->> 'manual' = 'true'
    )
  )
"""


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0007_settle_billz_reservations"),
        ("payments", "0004_refund_manual_reason"),
    ]

    operations = [migrations.RunSQL(BACKFILL, reverse_sql=migrations.RunSQL.noop)]
