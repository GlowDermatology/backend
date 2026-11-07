"""notifications.0009 — the campaign approval workflow, schema step (admin contract §2.8). The old
``campaign_state_fields`` CHECK and the ``campaign_due_idx`` partial index go first so that
``0010_campaign_approval_data`` can rewrite the states (``queued`` → ``sending``, ``scheduled`` → ``draft``);
``0011_campaign_approval_constraints`` adds the rebuilt CHECK and the ``dispatch_after`` index afterwards.
Adds the revision / test / approval / stop columns of ``Campaign``, the recipient state ``stopped`` and the
``TestDevice`` and ``CampaignTest`` models (Uzbek choice labels inline)."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import glowderma.core.models


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_uz_field_labels"),
        ("catalog", "0010_uz_field_labels"),
        ("notifications", "0008_uz_field_labels"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CampaignTest",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "revision",
                    models.PositiveIntegerField(verbose_name="matn versiyasi"),
                ),
                (
                    "device_label",
                    models.CharField(max_length=60, verbose_name="qurilma nomi"),
                ),
                ("phone", models.CharField(max_length=13, verbose_name="telefon")),
                (
                    "sent_at",
                    models.DateTimeField(
                        default=glowderma.core.models.timestamp_default,
                        editable=False,
                        verbose_name="yuborilgan",
                    ),
                ),
                (
                    "fcm_ok",
                    models.BooleanField(null=True, verbose_name="FCM qabul qildi"),
                ),
                (
                    "fcm_error",
                    models.CharField(blank=True, max_length=254, verbose_name="FCM xatosi"),
                ),
                (
                    "provider_message_id",
                    models.CharField(blank=True, max_length=128, verbose_name="provayder xabar ID"),
                ),
                (
                    "confirmed_at",
                    models.DateTimeField(null=True, verbose_name="tasdiqlangan"),
                ),
            ],
            options={
                "verbose_name": "kampaniya testi",
                "verbose_name_plural": "kampaniya testlari",
                "ordering": ["campaign_id", "id"],
            },
        ),
        migrations.CreateModel(
            name="TestDevice",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=glowderma.core.models.timestamp_default,
                        editable=False,
                        verbose_name="yaratilgan",
                    ),
                ),
                (
                    "updated_at",
                    models.DateTimeField(
                        default=glowderma.core.models.timestamp_default,
                        verbose_name="yangilangan",
                    ),
                ),
                ("phone", models.CharField(max_length=13, verbose_name="telefon")),
                ("label", models.CharField(max_length=60, verbose_name="nomi")),
                ("is_active", models.BooleanField(default=True, verbose_name="faol")),
            ],
            options={
                "verbose_name": "test qurilmasi",
                "verbose_name_plural": "test qurilmalari",
                "ordering": ["id"],
            },
        ),
        migrations.RemoveConstraint(
            model_name="campaign",
            name="campaign_state_fields",
        ),
        migrations.RemoveIndex(
            model_name="campaign",
            name="campaign_due_idx",
        ),
        migrations.AddField(
            model_name="campaign",
            name="approved_at",
            field=models.DateTimeField(null=True, verbose_name="tasdiqlangan"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="approved_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="tasdiqlagan",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="approved_revision",
            field=models.PositiveIntegerField(null=True, verbose_name="tasdiqlangan versiya"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="cancelled_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="bekor qilgan",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="content_revision",
            field=models.PositiveIntegerField(default=1, verbose_name="matn versiyasi"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="dispatch_after",
            field=models.DateTimeField(null=True, verbose_name="yuborish vaqti"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="failed_at",
            field=models.DateTimeField(null=True, verbose_name="yuborilmagan"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="stop_requested_at",
            field=models.DateTimeField(null=True, verbose_name="to‘xtatish so‘ralgan"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="stop_requested_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="to‘xtatishni so‘ragan",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="stopped_at",
            field=models.DateTimeField(null=True, verbose_name="to‘xtatilgan"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="test_confirmed_at",
            field=models.DateTimeField(null=True, verbose_name="test tasdiqlangan"),
        ),
        migrations.AddField(
            model_name="campaign",
            name="test_confirmed_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="testni tasdiqlagan",
            ),
        ),
        migrations.AddField(
            model_name="campaign",
            name="tested_revision",
            field=models.PositiveIntegerField(null=True, verbose_name="test tasdiqlangan versiya"),
        ),
        migrations.AlterField(
            model_name="campaign",
            name="scheduled_at",
            field=models.DateTimeField(null=True, verbose_name="so‘ralgan vaqt"),
        ),
        migrations.AlterField(
            model_name="campaign",
            name="state",
            field=models.CharField(
                choices=[
                    ("draft", "Qoralama"),
                    ("tested", "Test tasdiqlangan"),
                    ("scheduled", "Rejalashtirilgan"),
                    ("sending", "Yuborilmoqda"),
                    ("stopping", "To‘xtatilmoqda"),
                    ("sent", "Yuborildi"),
                    ("partially_sent", "Qisman yuborildi"),
                    ("cancelled", "Bekor qilindi"),
                    ("failed", "Yuborilmadi"),
                ],
                default="draft",
                max_length=16,
                verbose_name="holat",
            ),
        ),
        migrations.AlterField(
            model_name="campaignrecipient",
            name="state",
            field=models.CharField(
                choices=[
                    ("pending", "Kutilmoqda"),
                    ("sent", "Yuborildi"),
                    ("failed", "Xato"),
                    ("skipped", "O‘tkazib yuborildi"),
                    ("stopped", "To‘xtatildi"),
                ],
                default="pending",
                max_length=8,
                verbose_name="holat",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="campaign",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tests",
                to="notifications.campaign",
                verbose_name="kampaniya",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="confirmed_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="tasdiqlagan",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="device",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="accounts.device",
                verbose_name="qurilma",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="inbox",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="campaign_test",
                to="notifications.inboxnotification",
                verbose_name="inbox xabari",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="sent_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="yuborgan",
            ),
        ),
        migrations.AddField(
            model_name="testdevice",
            name="device",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="accounts.device",
                verbose_name="qurilma",
            ),
        ),
        migrations.AddField(
            model_name="testdevice",
            name="registered_by",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="ro‘yxatga olgan",
            ),
        ),
        migrations.AddField(
            model_name="campaigntest",
            name="test_device",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="tests",
                to="notifications.testdevice",
                verbose_name="test qurilmasi",
            ),
        ),
        migrations.AddConstraint(
            model_name="testdevice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("is_active", True)),
                fields=("device",),
                name="testdevice_active_device_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="testdevice",
            constraint=models.CheckConstraint(
                condition=models.Q(("label", ""), _negated=True),
                name="testdevice_label_required",
            ),
        ),
        migrations.AddIndex(
            model_name="campaigntest",
            index=models.Index(fields=["campaign", "revision"], name="campaigntest_campaign_rev_idx"),
        ),
        migrations.AddConstraint(
            model_name="campaigntest",
            constraint=models.CheckConstraint(
                condition=models.Q(("confirmed_at__isnull", True), ("fcm_ok", True), _connector="OR"),
                name="campaigntest_confirmed_delivered",
            ),
        ),
    ]
