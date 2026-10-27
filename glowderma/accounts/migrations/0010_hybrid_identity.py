"""Hybrid identity: ``Customer.phone`` nullable (unique when set), ``Customer.email`` (normalised, unique,
verified) + ``email_verified_at``; ``SmsCode`` holds a phone **or** an e-mail target and gains the purposes
``verify_email`` and ``attach_phone``. No data migration: every existing customer and code has a phone.

Reversible only while it loses nothing: the reverse refuses (:class:`IrreversibleError`) while a customer
has no phone or has an e-mail address, or an e-mail code (or a code of a new purpose) exists.
"""

from typing import Any

import django.db.models.functions.text
from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import Q


def refuse_lossy_reverse(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    Customer = apps.get_model("accounts", "Customer")
    SmsCode = apps.get_model("accounts", "SmsCode")
    customers = Customer.objects.using(alias).filter(Q(phone__isnull=True) | Q(email__isnull=False)).count()
    codes = (
        SmsCode.objects.using(alias)
        .filter(Q(phone="") | Q(purpose__in=("verify_email", "attach_phone")))
        .count()
    )
    if customers or codes:
        raise IrreversibleError(
            f"accounts.0010 cannot be reversed without data loss: {customers} customer(s) without a phone or "
            f"with an e-mail, {codes} e-mail or attach code(s)."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_merge_customer_fold_staff_manager"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="smscode",
            name="smscode_phone_e164",
        ),
        migrations.AddField(
            model_name="customer",
            name="email",
            field=models.CharField(blank=True, max_length=254, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="email_verified_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="smscode",
            name="email",
            field=models.CharField(blank=True, db_default="", default="", max_length=254),
        ),
        migrations.AlterField(
            model_name="customer",
            name="phone",
            field=models.CharField(blank=True, max_length=13, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="smscode",
            name="phone",
            field=models.CharField(blank=True, max_length=13),
        ),
        migrations.AlterField(
            model_name="smscode",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("signup", "Signup"),
                    ("reset", "Reset"),
                    ("new_device", "New Device"),
                    ("social_link", "Social Link"),
                    ("verify_email", "Verify Email"),
                    ("attach_phone", "Attach Phone"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddIndex(
            model_name="smscode",
            index=models.Index(
                condition=models.Q(("email", ""), _negated=True),
                fields=["email", "purpose", "created_at"],
                name="accounts_smscode_email_purpose",
            ),
        ),
        migrations.AddConstraint(
            model_name="customer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("email__isnull", True),
                    models.Q(
                        ("email", django.db.models.functions.text.Lower("email")),
                        ("email__contains", "@"),
                        ("email_verified_at__isnull", False),
                        models.Q(("email__regex", "\\s"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="customer_email_normalised_verified",
            ),
        ),
        migrations.AddConstraint(
            model_name="smscode",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("phone__regex", "^\\+998\\d{9}$"), ("email", "")),
                    models.Q(("phone", ""), ("email__contains", "@")),
                    _connector="OR",
                ),
                name="smscode_one_target",
            ),
        ),
        # Last, so that its reverse runs first.
        migrations.RunPython(migrations.RunPython.noop, refuse_lossy_reverse),
    ]
