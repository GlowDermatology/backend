"""M2 customer auth (customer contract §1.9, §2.1.6, §2.1.8, §2.2.3; plan §1 P0).

Device keys, the refresh rotation counter and its confirmation, the ``refresh_reused`` revoke reason, the
partial index for the previous-refresh lookup, Sign in with Apple (provider + sealed refresh token) and the
history horizon ``Customer.history_from``, back-filled from ``reactivated_at`` for accounts reactivated before
M2.

Reversible only while it loses nothing: the reverse refuses (:class:`IrreversibleError`) when an Apple
identity exists (its sealed refresh token could no longer be revoked and ``apple`` is not a 0005 choice) or
when a ``history_from`` differs from ``reactivated_at`` (a dormant-reset horizon a re-forward could not
rebuild, which would reopen the recycled-number history).
"""

from typing import Any

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models import F, Q


def backfill_history_from(apps: Any, schema_editor: Any) -> None:
    Customer = apps.get_model("accounts", "Customer")
    Customer.objects.using(schema_editor.connection.alias).filter(
        reactivated_at__isnull=False, history_from__isnull=True
    ).update(history_from=F("reactivated_at"))


def refuse_lossy_reverse(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    Customer = apps.get_model("accounts", "Customer")
    CustomerSocialIdentity = apps.get_model("accounts", "CustomerSocialIdentity")
    apple = CustomerSocialIdentity.objects.using(alias).filter(provider="apple").count()
    horizons = (
        Customer.objects.using(alias)
        .filter(history_from__isnull=False)
        .filter(Q(reactivated_at__isnull=True) | ~Q(history_from=F("reactivated_at")))
        .count()
    )
    if apple or horizons:
        raise IrreversibleError(
            f"accounts.0006 cannot be reversed without data loss: {apple} Apple identity row(s), {horizons} "
            "customer(s) whose history_from is not their reactivated_at."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_authsession_customer_fk"),
    ]

    operations = [
        migrations.AddField(
            model_name="authsession",
            name="rotation",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="authsession",
            name="rotation_confirmed",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="customer",
            name="history_from",
            field=models.DateTimeField(null=True),
        ),
        migrations.AddField(
            model_name="customersocialidentity",
            name="provider_refresh_token",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="device",
            name="key_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AlterField(
            model_name="authsession",
            name="revoke_reason",
            field=models.CharField(
                blank=True,
                choices=[
                    ("logout", "logout"),
                    ("expired", "expired"),
                    ("password_changed", "password_changed"),
                    ("deactivated", "deactivated"),
                    ("replaced", "replaced"),
                    ("admin_revoked", "admin_revoked"),
                    ("refresh_reused", "refresh_reused"),
                ],
                max_length=24,
            ),
        ),
        migrations.AlterField(
            model_name="customersocialidentity",
            name="provider",
            field=models.CharField(
                choices=[
                    ("google", "google"),
                    ("facebook", "facebook"),
                    ("apple", "apple"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddIndex(
            model_name="authsession",
            index=models.Index(
                condition=models.Q(("previous_refresh_hash", ""), _negated=True),
                fields=["previous_refresh_hash"],
                name="authsession_prev_refresh_idx",
            ),
        ),
        migrations.RunPython(backfill_history_from, refuse_lossy_reverse, elidable=True),
    ]
