"""Response bodies of the customer ``auth/`` and ``me/`` views that are not a selector's dictionary: the code
issue, the next steps and a session row."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from django.conf import settings

from glowderma.accounts import selectors as account_selectors
from glowderma.accounts import services as account_services
from glowderma.core import enums
from glowderma.core.pii import mask_email, mask_phone
from glowderma.customers import services


def code_issue_body(issue: account_services.CodeIssue) -> dict[str, Any]:
    return {
        "phone_mask": mask_phone(issue.phone) if issue.phone else None,
        "email_mask": mask_email(issue.email) if issue.email else None,
        "purpose": issue.purpose,
        "expires_in": issue.expires_in,
        "resend_in": issue.resend_in,
        "attempts": issue.attempts,
        "length": issue.length,
    }


def device_step_body(step: services.DeviceStep) -> dict[str, Any]:
    issue = step.issue
    return {
        "next_step": enums.NextStep.VERIFY_DEVICE.value,
        # the code went where the customer signed in with (hybrid identity): one mask, the other null
        "phone_mask": mask_phone(issue.phone) if issue.phone else None,
        "otp": {"expires_in": issue.expires_in, "resend_in": issue.resend_in, "length": issue.length},
        "social_ticket": None,
        "provider": None,
        "email_mask": mask_email(issue.email) if issue.email else None,
        # The step lasts as long as the app may keep the password and the verification token (C§2.0,
        # §2.1.4: 10 min), not the code's own TTL (that is otp.expires_in).
        "expires_in": int(settings.CUSTOMER_VERIFICATION_TTL_SECONDS),
        # additive: every place the code can go (the account's verified phone and e-mail), masked
        "channels": [
            {"channel": channel, "mask": mask_email(value) if channel == "email" else mask_phone(value)}
            for channel, value in step.channels
        ],
    }


def phone_step_body(step: services.PhoneStep) -> dict[str, Any]:
    return {
        "next_step": enums.NextStep.VERIFY_PHONE.value,
        "phone_mask": None,
        "otp": None,
        "social_ticket": step.ticket,
        "provider": step.provider,
        "email_mask": mask_email(step.email) if step.email else None,
        "expires_in": step.expires_in,
        "channels": [],
    }


def session_payload(session: Any, *, language: str, current_sid: str | None) -> dict[str, Any]:
    view = account_selectors.session_view(session, language=language, current_sid=current_sid)
    row = asdict(view)
    row["platform"] = row["platform"] or None
    return row


__all__ = [
    "code_issue_body",
    "device_step_body",
    "phone_step_body",
    "session_payload",
]
