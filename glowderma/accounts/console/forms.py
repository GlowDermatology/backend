"""Forms of the console's second-factor steps (Unfold widgets, so they match the login page)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from unfold.forms import AuthenticationForm as UnfoldAuthenticationForm
from unfold.widgets import UnfoldAdminTextInputWidget

from glowderma.accounts.console import notices
from glowderma.accounts.security.address_budget import IpFailureBudget, client_bucket
from glowderma.accounts.security.login_lockout import LoginGuard, email_hash

if TYPE_CHECKING:
    _TextInput = forms.TextInput
else:
    _TextInput = UnfoldAdminTextInputWidget


class TotpCodeForm(forms.Form):
    code = forms.CharField(
        label=_("Autentifikator ilovasidagi 6 xonali kod"),
        min_length=6,
        max_length=6,
        strip=True,
        widget=_TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]{6}",
                "autofocus": "autofocus",
                "placeholder": "123456",
            }
        ),
        error_messages={
            "required": _("Kodni kiriting."),
            "min_length": _("Kod 6 ta raqamdan iborat."),
            "max_length": _("Kod 6 ta raqamdan iborat."),
        },
    )


class RecoveryCodeForm(forms.Form):
    code = forms.CharField(
        label=_("Zaxira kod"),
        max_length=32,
        strip=True,
        widget=_TextInput(
            attrs={
                "autocomplete": "off",
                "autocapitalize": "off",
                "spellcheck": "false",
                "autofocus": "autofocus",
                "placeholder": "xxxxx-xxxxx",
            }
        ),
        error_messages={"required": _("Zaxira kodni kiriting.")},
    )


# Unfold's admin login form: a ``django.contrib.admin.forms.AdminAuthenticationForm`` with styled widgets.
if TYPE_CHECKING:
    _AdminAuthenticationForm = AdminAuthenticationForm
else:
    _AdminAuthenticationForm = UnfoldAuthenticationForm


PASSWORD_CHANGE_REQUIRED_CODE = "password_change_required"  # noqa: S105 - an error code


class GuardedAdminAuthenticationForm(_AdminAuthenticationForm):
    def clean(self) -> dict[str, Any]:
        username = str(self.cleaned_data.get("username", "")).strip().lower()
        self.cleaned_data["username"] = username
        budget = IpFailureBudget("password")
        bucket = client_bucket(self.request) if self.request is not None else ""
        if bucket:
            budget.reserve(bucket)
        guard = LoginGuard()
        hashed = email_hash(username)
        guard.check("django_admin_login", hashed)
        try:
            result = super().clean()
        except ValidationError:
            guard.record_failure("django_admin_login", hashed)
            raise
        guard.reset("django_admin_login", hashed)
        if bucket:
            budget.refund(bucket)
        user = self.get_user()
        if user is not None and getattr(user, "must_change_password", False):
            # The right password, but someone else chose it (feat/admin-ux): the person sets their own in the
            # admin panel first. Not a failed guess (the lockout and the address budget stay as they are).
            raise ValidationError(
                notices.console_password_change_required(), code=PASSWORD_CHANGE_REQUIRED_CODE
            )
        return result
