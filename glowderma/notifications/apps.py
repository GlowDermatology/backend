from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db import connections
from django.db.models.signals import post_migrate

# The four push templates (design 03 §3.1); ``0002_seed_status_templates`` inserts them with these texts.
# ``delivered`` has no English text by design (the mock shows ``en: false`` for that row).
STATUS_TEMPLATES: tuple[tuple[str, str, str, str], ...] = (
    (
        "processing",
        "To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda.",
        "Оплата принята. Заказ №{raqam} готовится.",
        "Payment received. Order №{raqam} is being prepared.",
    ),
    (
        "handed_to_courier",
        "Buyurtma №{raqam} kuryerga topshirildi. Holatni ilovada kuzating.",
        "Заказ №{raqam} передан курьеру. Следите за статусом в приложении.",
        "Order №{raqam} has been handed to the courier. Track it in the app.",
    ),
    (
        "delivered",
        "Buyurtma №{raqam} yetkazildi. Xaridingiz uchun rahmat.",
        "Заказ №{raqam} доставлен. Спасибо за покупку.",
        "",
    ),
    (
        "cancelled",
        "Buyurtma №{raqam} bekor qilindi. To‘lov qaytariladi.",
        "Заказ №{raqam} отменён. Оплата будет возвращена.",
        "Order №{raqam} has been cancelled. The payment will be refunded.",
    ),
)


def ensure_status_templates(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Keep one ``StatusTemplate`` row per ``TemplateStatus`` (design 03 §3.1: ``GET notification-templates/
    {status}/`` never 404s for a valid status and never writes).

    ``0002_seed_status_templates`` inserts them; this ``post_migrate`` receiver re-inserts them after a
    ``flush`` (transactional tests, ``--reuse-db`` reruns), the way ``contenttypes`` restores its rows. An
    existing row is left as the admin edited it.
    """
    # ``migrate`` passes the post-migration state apps, ``flush`` passes nothing (the global registry)
    try:
        StatusTemplate = apps.get_model("notifications", "StatusTemplate")
    except LookupError:  # migrated to zero: no table to fill
        return
    # ``flush`` after ``migrate catalog zero`` (a dependency) still knows the model but has no table
    if StatusTemplate._meta.db_table not in connections[using].introspection.table_names():
        return
    for status, text_uz, text_ru, text_en in STATUS_TEMPLATES:
        StatusTemplate.objects.using(using).get_or_create(
            status=status, defaults={"text_uz": text_uz, "text_ru": text_ru, "text_en": text_en}
        )


class NotificationsConfig(AppConfig):
    name = "glowderma.notifications"
    verbose_name = "Xabarnomalar"

    def ready(self) -> None:
        post_migrate.connect(
            ensure_status_templates, sender=self, dispatch_uid="notifications.ensure_status_templates"
        )
        # The outbox handlers (push.order_status, push.appointment, campaign.send_batch) and the dead-letter
        # hook register at import time; every process that dispatches the outbox needs them.
        from glowderma.notifications import tasks  # noqa: F401
