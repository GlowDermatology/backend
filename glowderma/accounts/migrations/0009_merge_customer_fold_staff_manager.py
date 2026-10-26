"""Joins the two accounts.0008 branches of the gap closure: the folded customer-name index (catalog package,
GC-2) and ``StaffUser.is_manager`` (staff package, GST-1).
They touch different tables, so no operation is needed."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_customer_name_fold_trgm"),
        ("accounts", "0008_staffuser_is_manager"),
    ]

    operations = []
