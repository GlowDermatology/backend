"""How a session's sign-in was proven, and the OAuth client of a sealed provider refresh token.

``AuthSession.auth_method`` (``password`` | ``google`` | ``facebook`` | ``apple``): admin contract §2.1 lets
only a password session link a staff identity; customer sessions record the sign-in method too, and a
refresh rotation keeps it (same row). Rows from before this migration stay ``""``, which links nothing.

``CustomerSocialIdentity.provider_client_id``: the client the sealed Apple refresh token was issued to (the ID
token's ``aud`` bundle id), so the revocation of customer contract §2.1.8 posts the right ``client_id`` when
``APPLE_BUNDLE_IDS`` lists several bundle ids. ``""`` falls back to the first configured bundle id.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_customer_auth_hardening"),
    ]

    operations = [
        migrations.AddField(
            model_name="authsession",
            name="auth_method",
            field=models.CharField(
                blank=True,
                choices=[
                    ("password", "Password"),
                    ("google", "Google"),
                    ("facebook", "Facebook"),
                    ("apple", "Apple"),
                ],
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="customersocialidentity",
            name="provider_client_id",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
