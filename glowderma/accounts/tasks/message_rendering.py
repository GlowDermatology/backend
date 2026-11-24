"""The texts accounts sends: the one-time-code e-mail and the staff password-link e-mail (pure rendering, in
the recipient's language). The one-time-code SMS has no text of ours: DevSMS's universal OTP template is
chosen by purpose in ``otp_delivery``."""

from __future__ import annotations

from django.conf import settings
from django.template.loader import render_to_string

from glowderma.core import enums, i18n


def email_parts(code: str, lang: str, purpose: str) -> tuple[str, str, str]:
    """``(subject, text, html)`` of an e-mail code in the request language (uz/ru/en catalog texts,
    ``accounts/email/otp.txt`` and ``.html``)."""
    lang = i18n.normalize_language(lang)
    intro_key = f"email.otp.intro.{purpose}"
    if intro_key not in i18n.CATALOG:
        intro_key = "email.otp.intro.verify_email"
    minutes = max(1, int(settings.CUSTOMER_OTP_TTL_SECONDS) // 60)
    context = {
        "lang": lang,
        "subject": i18n.t("email.otp.subject", lang),
        "intro": i18n.t(intro_key, lang),
        "code": code,
        "expiry": i18n.t("email.otp.expiry", lang, m=minutes),
        "ignore": i18n.t("email.otp.ignore", lang),
        "footer": i18n.t("email.otp.footer", lang),
    }
    text = render_to_string("accounts/email/otp.txt", context).strip() + "\n"
    html = render_to_string("accounts/email/otp.html", context)
    return str(context["subject"]), text, html


def staff_link_parts(*, link: str, name: str, lang: str, purpose: str) -> tuple[str, str, str]:
    """``(subject, text, html)`` of a staff password link in ``lang`` (uz/ru/en catalog texts
    ``email.staff_link.*``, templates ``accounts/email/staff_link.txt`` and ``.html``)."""
    lang = i18n.normalize_language(lang)
    if purpose not in enums.StaffPasswordPurpose.values:
        purpose = enums.StaffPasswordPurpose.RESET
    context = {
        "lang": lang,
        "subject": i18n.t(f"email.staff_link.{purpose}.subject", lang),
        "greeting": i18n.t("email.staff_link.greeting_named" if name else "email.staff_link.greeting", lang),
        "name": name,
        "intro": i18n.t(f"email.staff_link.{purpose}.intro", lang),
        "button": i18n.t("email.staff_link.button", lang),
        "link": link,
        "expiry": i18n.t("email.staff_link.expiry", lang, n=int(settings.STAFF_INVITE_TTL_HOURS)),
        "ignore": i18n.t("email.staff_link.ignore", lang),
        "footer": i18n.t("email.otp.footer", lang),
    }
    text = render_to_string("accounts/email/staff_link.txt", context).strip() + "\n"
    html = render_to_string("accounts/email/staff_link.html", context)
    return str(context["subject"]), text, html


__all__ = [
    "email_parts",
    "staff_link_parts",
]
