"""What the ops console's sign-in steps tell the person: the step messages (lazy, in the console's language),
the lockout notice and the recovery-codes warning."""

from __future__ import annotations

import math

from django.utils.translation import gettext, ngettext
from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import Throttled

WRONG_CODE = _("Kod noto‘g‘ri yoki allaqachon ishlatilgan. Ilovadagi yangi kodni kiriting.")
WRONG_RECOVERY = _("Zaxira kod noto‘g‘ri yoki allaqachon ishlatilgan.")
SIGNIN_EXPIRED = _("Kirish muddati tugadi. Email va parolni qaytadan kiriting.")
LOGGED_OUT = _("Siz konsoldan chiqdingiz.")
ENROL_CLOSED = _(
    "Ikki bosqichli himoyani sozlash muddati tugagan yoki ochilmagan. Menejerdan 2FA ni qayta o‘rnatishni "
    "so‘rang, so‘ng qaytadan kiring."
)
ENROL_SAME_MANAGER = _(
    "Parolingiz va 2FA bir menejer tomonidan tiklangan: xavfsizlik uchun 2FA ni boshqa menejer (yoki server "
    "buyrug‘i) qayta o‘rnatishi kerak."
)
CODES_SHOWN_ONCE = _(
    "Zaxira kodlar faqat bir marta ko‘rsatiladi va ular endi ko‘rsatilmaydi. Kodlarni saqlamagan bo‘lsangiz, "
    "menejerdan 2FA ni qayta o‘rnatishni so‘rang."
)
CSRF_FAILED = _(
    "Sahifa eskirgan yoki sessiya yangilangan, shuning uchun so‘rov qabul qilinmadi. Sahifani qaytadan "
    "oching va yana urinib ko‘ring."
)


def few_recovery_codes(left: int) -> str:
    return ngettext(
        "Zaxira kodlardan %(left)s tasi qoldi. Menejerdan 2FA ni qayta o‘rnatishni so‘rang (yangi kodlar "
        "beriladi).",
        "Zaxira kodlardan %(left)s tasi qoldi. Menejerdan 2FA ni qayta o‘rnatishni so‘rang (yangi kodlar "
        "beriladi).",
        left,
    ) % {"left": left}


def lockout_message(exc: Throttled) -> tuple[str, int]:
    """The lockout notice in the console's language, and the wait in seconds."""
    wait = int(math.ceil(float(getattr(exc, "wait", None) or 0)))
    notice = ngettext(
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        "Juda ko‘p urinish. %(wait)s soniyadan so‘ng qayta urinib ko‘ring.",
        wait,
    ) % {"wait": wait}
    return notice, wait


def console_password_change_required() -> str:
    """The console login refusal of an account with a pending forced password change (its language)."""
    return gettext("Avval admin panelda o‘z parolingizni o‘rnating, so‘ng konsolga qayta kiring.")


__all__ = [
    "WRONG_CODE",
    "WRONG_RECOVERY",
    "SIGNIN_EXPIRED",
    "LOGGED_OUT",
    "ENROL_CLOSED",
    "ENROL_SAME_MANAGER",
    "CODES_SHOWN_ONCE",
    "CSRF_FAILED",
    "console_password_change_required",
    "few_recovery_codes",
    "lockout_message",
]
