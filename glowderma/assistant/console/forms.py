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
from glowderma.assistant.models import AssistantSettings, ProviderConfig, SceneSettings, SceneTemplate
from glowderma.core.compose import hex_colour

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


class SceneSettingsForm(ChangesMixin, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = SceneSettings
        fields = writes.SCENE_SETTINGS_FIELDS
        widgets = {"style": forms.Textarea(attrs={"rows": 5})}
        help_texts = {
            "enabled": _("Studiya sahnalari va ekranlarga moslash admin panelda ko‘rinadi."),
            "ai_enabled": _("AI sahnalari: fonni quyida tanlangan provayder chizadi."),
            "scene_provider": _(
                "AI sahna fonini kim chizadi. Gemini (Nano Banana): Google loyihasida to‘lov yoqilgan "
                "bo‘lishi kerak; qo‘l yopgan kichik joyni to‘ldira oladi. Bria (Fibo 2): Provayderlar "
                "sahifasidagi Bria kaliti bilan ishlaydi, faqat fonni chizadi (qo‘l yopgan rasmlar rad "
                "etiladi). Har bir provayder o‘z kalitidan foydalanadi."
            ),
            "segmenter": _(
                "Mahsulotni fondan qanday ajratish: Gemini yashil fon (rasm modeli kerak) yoki Bria "
                "(Provayderlar sahifasida Bria kaliti kerak)."
            ),
            "analyst_model": _("Mahsulot chegarasi, qo‘l va kesilganlikni aniqlaydigan arzon model."),
            "max_occluded_pct": _(
                "Qo‘l mahsulotning shuncha foizini yopsa ham AI to‘ldiradi (belgilab ko‘rsatiladi); "
                "ko‘prog‘ida rasm rad etiladi. Studiya shablonlari hech qanday yopilgan joyni to‘ldirmaydi."
            ),
            "style": _(
                "Barcha AI sahnalariga qo‘shiladigan uslub. Mahsulotni o‘zgartirmaslik qoidalari kodda."
            ),
            "price_bria_call": _("Bria fonni olib tashlash narxi, bitta so‘rov (promptli: $0.025)."),
            "price_bria_scene_call": _("Bria sahna (fon yaratish) narxi, bitta rasm ($0.032)."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        form: Any = self
        form.fields["style"].widget = UnfoldAdminTextareaWidget(attrs={"rows": 5})
        for name, low, high in (("max_attempts", 1, 3), ("candidate_ttl_hours", 1, 720)):
            field = form.fields[name]
            field.validators += [MinValueValidator(low), MaxValueValidator(high)]

    def _model(self, name: str) -> str:
        value = str(self.cleaned_data[name]).strip()
        if not MODEL_ID.match(value):
            raise forms.ValidationError(MODEL_ID_INVALID)
        return value

    def clean_analyst_model(self) -> str:
        return self._model("analyst_model")

    def clean_image_model(self) -> str:
        return self._model("image_model")


class SceneTemplateForm(ChangesMixin, forms.ModelForm):  # type: ignore[type-arg]
    class Meta:
        model = SceneTemplate
        fields = writes.SCENE_TEMPLATE_FIELDS
        help_texts = {
            "params": _(
                'Studiya: {"fill": "solid" | "linear" | "radial", "colours": ["#rrggbb", …], '
                '"floor": "#rrggbb", "floor_at": 0.8, "shadow_opacity": 0.35, "shadow_colour": "#rrggbb"}.'
            ),
            "prompt": _("AI sahnasi: faqat fon va atrofdagi narsalar tavsifi (mahsulot haqida yozmang)."),
        }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        form: Any = self
        form.fields["prompt"].widget = UnfoldAdminTextareaWidget(attrs={"rows": 5})
        form.fields["params"].widget = UnfoldAdminTextareaWidget(attrs={"rows": 5, "spellcheck": "false"})

    def clean_params(self) -> dict[str, Any]:
        value = self.cleaned_data["params"]
        if not isinstance(value, dict):
            raise forms.ValidationError(_("JSON obyekt bo‘lishi kerak."))
        try:
            for colour in [*list(value.get("colours") or []), value.get("floor"), value.get("shadow_colour")]:
                if colour:
                    hex_colour(str(colour))
        except ValueError:
            raise forms.ValidationError(_("Ranglar #rrggbb ko‘rinishida bo‘lsin.")) from None
        if value.get("fill") not in (None, "solid", "linear", "radial"):
            raise forms.ValidationError(_("fill: solid, linear yoki radial."))
        return value

    def clean(self) -> dict[str, Any]:
        data: dict[str, Any] = super().clean() or {}
        form: Any = self
        if form.instance.kind == "ai_scene" and not str(data.get("prompt") or "").strip():
            form.add_error("prompt", _("AI sahnasi uchun tavsif majburiy."))
        return data


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
