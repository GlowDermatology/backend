"""Inbox rows of the ``account_security`` notices (customer contract §2.12, §2.14.2; plan §1 P0).

Source ``account`` (``source_id`` = the customer id) and the CHECK ``inbox_kind_source_fields`` extended with
``kind = 'general' AND source_type = 'account' AND template_status = ''``.
"""

from django.db import migrations, models


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
    ]
