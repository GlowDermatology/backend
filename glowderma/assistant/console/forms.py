"""Forms of the AI assistant's console pages. None of them saves itself: the views pass ``changes()`` to
:mod:`glowderma.assistant.console.writes` (settings, provider, prompts) or the key to
:mod:`glowderma.assistant.credentials`.

:class:`RotateKeyForm` is write-only: a ``PasswordInput(render_value=False)`` with
``autocomplete="new-password"``, so neither the page, a re-rendered invalid form nor the browser's autofill
ever holds the key; its errors never quote the value."""

from __future__ import annotations

import re
from typing import Any

from django import forms
from django.core.validators import MaxValueValidator, MinValueValidator
from django.utils.translation import gettext_lazy as _
from unfold.widgets import (
    UnfoldAdminDecimalFieldWidget,
    UnfoldAdminIntegerFieldWidget,
    UnfoldAdminPasswordWidget,
    UnfoldAdminSelectWidget,
    UnfoldAdminTextareaWidget,
    UnfoldAdminTextInputWidget,
    UnfoldBooleanSwitchWidget,
)

from glowderma.assistant.console import writes
from glowderma.assistant.credentials import MAX_KEY_LENGTH
from glowderma.assistant.models import AssistantSettings, ProviderConfig

MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,99}$")
MODEL_ID_INVALID = _(
    "Model nomi faqat kichik lotin harflari, raqamlar, nuqta va chiziqchadan iborat bo‘ladi."
)
KEY_INVALID = _("Kalitda bo‘sh joy bo‘lmasligi kerak.")
INSTRUCTIONS_MAX = 8000


def unfold_widget(field: forms.Field) -> Any:
    """The console's (Unfold's) widget for a plain form field, keeping its attributes."""
    attrs = dict(field.widget.attrs)
    if isinstance(field, forms.BooleanField):
        return UnfoldBooleanSwitchWidget(attrs=attrs)
    if isinstance(field, forms.ChoiceField):
        widget = UnfoldAdminSelectWidget(attrs=attrs)
        widget.choices = field.widget.choices
        return widget
    if isinstance(field, forms.DecimalField):
        return UnfoldAdminDecimalFieldWidget(attrs=attrs)
    if isinstance(field, forms.IntegerField):
        return UnfoldAdminIntegerFieldWidget(attrs=attrs)
    return UnfoldAdminTextInputWidget(attrs=attrs)


class ChangesMixin:
    """Unfold widgets on every field, and ``changes()``: the cleaned values of the fields the form changed."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        form: Any = self
        for field in form.fields.values():
            field.widget = unfold_widget(field)

    def changes(self) -> dict[str, Any]:
        form: Any = self
        return {name: form.cleaned_data[name] for name in form.changed_data}


class SettingsForm(ChangesMixin, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = AssistantSettings
        fields = writes.SETTINGS_FIELDS
        help_texts = {
            "enabled": _("O‘chirilganda admin panelda yangi so‘rov yuborilmaydi."),
            "daily_budget_usd": _("Bugungi xarajat shu chegaraga yetganda yangi so‘rovlar to‘xtatiladi."),
            "monthly_budget_usd": _("Oy boshidan beri xarajat chegarasi."),
            "max_images": _("Rasm bilan ishlash rejimida modelga yuboriladigan rasmlar soni (8 tagacha)."),
        }


class ProviderForm(ChangesMixin, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = ProviderConfig
        fields = writes.PROVIDER_FIELDS
        help_texts = {
            "model_id": _("Masalan: gemini-2.5-flash-lite. Keyingi so‘rovdan boshlab ishlatiladi."),
            "price_input_per_mtok_usd": _("Narx hisoblash uchun: provayder narxlar sahifasidan oling."),
            "timeout_seconds": _(
                "Bitta urinish uchun, 5–20 soniya. Butun so‘rov 90 soniyada to‘xtatiladi: kutish vaqti "
                "qayta urinishlar bilan birga shu chegaraga sig‘ishi kerak."
            ),
            "max_retries": _("Ulanish uzilsa yoki provayder band bo‘lsa qo‘shimcha urinishlar soni (0–2)."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        form: Any = self
        for name, low, high in (("timeout_seconds", 5, 20), ("max_retries", 0, 2)):
            field = form.fields[name]
            field.validators += [MinValueValidator(low), MaxValueValidator(high)]
            field.widget.attrs.update(min=low, max=high)

    def clean_model_id(self) -> str:
        value = str(self.cleaned_data["model_id"]).strip()
        if not MODEL_ID.match(value):
            raise forms.ValidationError(MODEL_ID_INVALID)
        return value


class RotateKeyForm(forms.Form):
    api_key = forms.CharField(
        label=_("Yangi API kalit"),
        max_length=MAX_KEY_LENGTH,
        strip=True,
        widget=UnfoldAdminPasswordWidget(
            render_value=False,
            attrs={"autocomplete": "new-password", "spellcheck": "false", "autocapitalize": "off"},
        ),
        help_text=_(
            "Kalit faqat Vault’ga yoziladi va hech qayerda ko‘rsatilmaydi. Ishchi jarayon uni tekshiradi; "
            "tekshiruvdan o‘tsa faol bo‘ladi, aks holda eski kalit ishlashda davom etadi."
        ),
    )

    def clean_api_key(self) -> str:
        value = str(self.cleaned_data["api_key"])
        if any(char.isspace() for char in value):
            raise forms.ValidationError(KEY_INVALID)
        return value


class InstructionsForm(forms.Form):
    instructions = forms.CharField(
        label=_("Ko‘rsatmalar"),
        max_length=INSTRUCTIONS_MAX,
        widget=UnfoldAdminTextareaWidget(attrs={"rows": 18, "spellcheck": "false"}),
        help_text=_(
            "Uslub, ohang, atamalar, SEO va uzunlik bo‘yicha ko‘rsatmalar. Majburiy qoidalar "
            "(o‘ylab topilgan "
            "tarkib, SPF, sertifikat va tibbiy da’volar taqiqi, JSON sxemasi) kodda va ularni bekor qilib "
            "bo‘lmaydi."
        ),
    )


class PromptTestForm(forms.Form):
    product = forms.IntegerField(
        label=_("Namuna mahsulot (ID)"),
        min_value=1,
        widget=UnfoldAdminIntegerFieldWidget(attrs={"list": "gd-ai-products"}),
    )

    def clean_product(self) -> int:
        from glowderma.catalog import selectors

        value = int(self.cleaned_data["product"])
        if selectors.get_product(value) is None:
            raise forms.ValidationError(_("Bunday mahsulot topilmadi."))
        return value


class ConfirmForm(forms.Form):
    confirm = forms.BooleanField(required=True, widget=forms.HiddenInput)
