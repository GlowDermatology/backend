"""Settle the converted reservations the availability rule no longer counts — and never will again (NEW-2).

``commerce.selectors.available_stock`` now reads only the unsettled converted reservations. This step stamps
``billz_settled_at`` on every existing one whose "stopped counting" is final under the current rule:

* its sale is in Billz (``Purchase.billz_pushed_at``) and the stock was read after that and after the
  conversion;
* its order was cancelled, its sale never reached Billz and no payment was attempted there, and the stock
  was read after the conversion;
* the past fake era: the product's stock was never read from Billz (no stock row, or ``synced_at`` null) —
  the current rule ignores those units, and a sale Billz never saw must not start counting at the first live
  read (staff entered such sales by hand, customer contract §6.14).

Everything else (a paid order whose sale is still being pushed, deferred or dead) stays unsettled and keeps
counting. Reversing does nothing (0006's reversal drops the column).
"""

from django.db import migrations

SETTLE = """
UPDATE commerce_stockreservation AS r
SET billz_settled_at = NOW()
FROM commerce_order AS o
LEFT JOIN loyalty_purchase AS p ON p.order_id = o.id
WHERE r.order_id = o.id
  AND r.state = 'converted'
  AND r.billz_settled_at IS NULL
  AND (
    NOT EXISTS (
      SELECT 1 FROM catalog_productstock AS s WHERE s.product_id = r.product_id AND s.synced_at IS NOT NULL
    )
    OR EXISTS (
      SELECT 1 FROM catalog_productstock AS s
      WHERE s.product_id = r.product_id
        AND s.synced_at IS NOT NULL
        AND r.converted_at <= s.synced_at
        AND (
          p.billz_pushed_at < s.synced_at
          OR (
            p.billz_pushed_at IS NULL
            AND p.billz_payment_attempted_at IS NULL
            AND o.status = 'cancelled'
          )
        )
    )
  )
"""


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
        ("commerce", "0006_stockreservation_billz_settled_at"),
        ("loyalty", "0004_purchase_billz_payment_attempted_at"),
    ]

    operations = [migrations.RunSQL(SETTLE, reverse_sql=migrations.RunSQL.noop)]
