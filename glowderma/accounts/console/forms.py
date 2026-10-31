"""Forms of the console's second-factor steps (Unfold widgets, so they match the login page)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django import forms
from unfold.widgets import UnfoldAdminTextInputWidget

if TYPE_CHECKING:
    _TextInput = forms.TextInput
else:
    _TextInput = UnfoldAdminTextInputWidget


class TotpCodeForm(forms.Form):
    code = forms.CharField(
        label="Autentifikator ilovasidagi 6 xonali kod",
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
            "required": "Kodni kiriting.",
            "min_length": "Kod 6 ta raqamdan iborat.",
            "max_length": "Kod 6 ta raqamdan iborat.",
        },
    )


class RecoveryCodeForm(forms.Form):
    code = forms.CharField(
        label="Zaxira kod",
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
        error_messages={"required": "Zaxira kodni kiriting."},
    )
