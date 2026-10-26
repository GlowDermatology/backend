"""Inbox rows of the ``account_security`` notices (customer contract §2.12, §2.14.2; plan §1 P0).

Source ``account`` (``source_id`` = the customer id) and the CHECK ``inbox_kind_source_fields`` extended with
``kind = 'general' AND source_type = 'account' AND template_status = ''``.

Reversible only while no ``account`` row exists: the old CHECK cannot hold them, so the reverse refuses
with :class:`IrreversibleError` naming the count instead of failing on the constraint (or deleting notices
silently).
"""

from typing import Any

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def refuse_with_account_rows(apps: Any, schema_editor: Any) -> None:
    InboxNotification = apps.get_model("notifications", "InboxNotification")
    count = (
        InboxNotification.objects.using(schema_editor.connection.alias).filter(source_type="account").count()
    )
    if count:
        raise IrreversibleError(
            f"notifications.0003 cannot be reversed: {count} inbox row(s) with source_type='account' exist "
            "(account-security notices); delete them first if losing them is intended."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0002_seed_status_templates"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="inboxnotification",
            name="inbox_kind_source_fields",
        ),
        migrations.AlterField(
            model_name="inboxnotification",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("campaign", "Campaign"),
                    ("order", "Order"),
                    ("appointment", "Appointment"),
                    ("account", "Account"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="inboxnotification",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("kind", "order_status"),
                        ("source_type", "order"),
                        models.Q(("template_status", ""), _negated=True),
                    ),
                    models.Q(
                        ("kind", "appointment"),
                        ("source_type", "appointment"),
                        ("template_status", ""),
                    ),
                    models.Q(
                        ("kind__in", ["promo", "general"]),
                        ("source_type", "campaign"),
                        ("template_status", ""),
                    ),
                    models.Q(
                        ("kind", "general"),
                        ("source_type", "account"),
                        ("template_status", ""),
                    ),
                    _connector="OR",
                ),
                name="inbox_kind_source_fields",
            ),
        ),
        # Last, so that it runs first when the migration is reversed.
        migrations.RunPython(migrations.RunPython.noop, refuse_with_account_rows, elidable=True),
    ]
