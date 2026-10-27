"""``AuthSession`` changes for the customer audience (design 01 §1.6).

``customer_id`` (a bare ``BigIntegerField`` since M0) becomes ``ForeignKey("accounts.Customer")`` on the
**same column**: a rename of the field in migration state followed by an ``AlterField`` that adds the FK
constraint and its index — no data copy. A ``RunPython`` precheck fails loudly if a stray non-null
``customer_id`` exists (none can: customer authentication arrives with M2). Then the M2 columns (``device``,
rotating refresh), the two ``(subject, revoked_at)`` indexes and the audience/subject check constraint.
"""

import django.db.models.deletion
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps


def assert_no_customer_sessions(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    AuthSession = apps.get_model("accounts", "AuthSession")
    stray = AuthSession.objects.filter(customer_id__isnull=False).count()
    if stray:
        raise RuntimeError(
            f"accounts.0005: {stray} AuthSession row(s) carry a customer_id before accounts.Customer exists"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_customer_device_address_smscode_social"),
    ]

    operations = [
        migrations.RunPython(assert_no_customer_sessions, migrations.RunPython.noop, elidable=False),
        migrations.RenameField(model_name="authsession", old_name="customer_id", new_name="customer"),
        migrations.AlterField(
            model_name="authsession",
            name="customer",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="sessions",
                to="accounts.customer",
            ),
        ),
        migrations.AddField(
            model_name="authsession",
            name="device",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sessions",
                to="accounts.device",
            ),
        ),
        migrations.AddField(
            model_name="authsession",
            name="previous_refresh_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="authsession",
            name="rotated_at",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddIndex(
            model_name="authsession",
            index=models.Index(fields=["customer", "revoked_at"], name="accounts_authsession_cust_rev"),
        ),
        migrations.AddIndex(
            model_name="authsession",
            index=models.Index(fields=["staff", "revoked_at"], name="accounts_authsession_staff_rev"),
        ),
        migrations.AddConstraint(
            model_name="authsession",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("audience", "staff"), ("customer__isnull", True), ("staff__isnull", False)),
                    models.Q(("audience", "customer"), ("customer__isnull", False), ("staff__isnull", True)),
                    _connector="OR",
                ),
                name="authsession_subject_matches_audience",
            ),
        ),
    ]
