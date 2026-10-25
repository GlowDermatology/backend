"""Fixed uz/ru/en texts of the appointment pushes (design 03 §3.0 / N-2: no admin templates for them in v1).

``{vaqt}`` is the appointment start as ``DD.MM.YYYY HH:MM`` (Tashkent). A language without a text falls back
to Uzbek, exactly like the order-status templates.
"""

from __future__ import annotations

from datetime import datetime

from glowderma.core import timeutils

TIME_FORMAT = "%d.%m.%Y %H:%M"

# action → language → (title, body)
APPOINTMENT_TEXTS: dict[str, dict[str, tuple[str, str]]] = {
    "confirmed": {
        "uz": ("Qabul tasdiqlandi", "Sizning {vaqt} dagi qabulingiz tasdiqlandi. Sizni kutamiz!"),
        "ru": ("Приём подтверждён", "Ваш приём {vaqt} подтверждён. Ждём вас!"),
        "en": ("Appointment confirmed", "Your appointment on {vaqt} is confirmed. See you there!"),
    },
    "rejected": {
        "uz": (
            "Qabul rad etildi",
            "Afsuski, {vaqt} dagi qabulingizni tasdiqlay olmadik. Boshqa vaqtni tanlang.",
        ),
        "ru": ("Приём отклонён", "К сожалению, приём {vaqt} не подтверждён. Выберите другое время."),
        "en": ("Appointment declined", "Unfortunately we could not confirm your appointment on {vaqt}."),
    },
    "rescheduled": {
        "uz": ("Qabul vaqti o‘zgardi", "Qabulingiz yangi vaqtga ko‘chirildi: {vaqt}."),
        "ru": ("Время приёма изменено", "Ваш приём перенесён на {vaqt}."),
        "en": ("Appointment moved", "Your appointment has been moved to {vaqt}."),
    },
    "cancelled": {
        "uz": (
            "Qabul bekor qilindi",
            "{vaqt} dagi qabulingiz bekor qilindi. Savollar bo‘lsa, biz bilan bog‘laning.",
        ),
        "ru": ("Приём отменён", "Ваш приём {vaqt} отменён. Если есть вопросы, свяжитесь с нами."),
        "en": (
            "Appointment cancelled",
            "Your appointment on {vaqt} has been cancelled. Contact us with any questions.",
        ),
    },
}


def appointment_text(action: str, language: str, start: datetime) -> tuple[str, str, str]:
    """``(title, body, language used)`` for an appointment push; unknown actions are a programming error."""
    by_language = APPOINTMENT_TEXTS[action]
    used = language if language in by_language else "uz"
    title, body = by_language[used]
    when = start.astimezone(timeutils.TASHKENT).strftime(TIME_FORMAT)
    return title, body.replace("{vaqt}", when), used
