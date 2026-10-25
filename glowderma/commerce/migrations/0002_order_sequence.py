"""Order numbers start at 10001 (design 03 §1.3 / §9 row 10).

``START WITH`` makes the intent visible in ``pg_sequences.start_value`` (what ``core.seeding.reset_sequences``
restores on an empty table); ``RESTART`` moves the current value. The reverse puts both back to 1.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("commerce", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="ALTER SEQUENCE commerce_order_id_seq START WITH 10001 RESTART WITH 10001;",
            reverse_sql="ALTER SEQUENCE commerce_order_id_seq START WITH 1 RESTART WITH 1;",
        )
    ]
