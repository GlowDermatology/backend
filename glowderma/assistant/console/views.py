"""The AI assistant's ops console pages (``UNFOLD["SITE_VIEWS"]``, ``config/settings/ops_admin.py``).

Superusers only (:func:`~glowderma.assistant.console.access.is_superuser`; anyone else signed in gets 403),
behind the console's 2FA, network gate, CSRF and ``never_cache`` (``admin_site.admin_view``). Every POST
answers with a redirect (PRG) and a message in the console's language, or re-renders the bound form.

Pages: settings and budgets (``ai/``), providers and their parameters (``ai/providers/<provider>/``), the API
key's rotation (``…/key/``, write-only) and rollback (``…/rollback/``), prompt templates and versions
(``ai/prompts/…``) and the generation jobs with usage totals (``ai/jobs/…``). No Django ``LogEntry``
is written
by any of them; the domain writes are audited (``assistant.*``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.conf import settings as django_settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from rest_framework.exceptions import APIException
from unfold.views import UnfoldSiteViewMixin

from glowderma.assistant import credentials
from glowderma.assistant.console import forms, reports, writes
from glowderma.assistant.console.access import is_superuser
from glowderma.assistant.models import (
    SETTINGS_PK,
    AssistantSettings,
    GenerationJob,
    PromptTemplate,
    PromptVersion,
    ProviderConfig,
    SceneJob,
    SceneSettings,
    SceneTemplate,
)
from glowderma.core import enums, timeutils
from glowderma.integrations.secret import Secret
from glowderma.integrations.vault.ports import VaultError

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

TEMPLATES = "assistant/console"
SAVED: StrOrPromise = _("Saqlandi.")
NOTHING_CHANGED: StrOrPromise = _("O‘zgarish yo‘q: saqlanadigan narsa topilmadi.")
UNKNOWN_ACTION: StrOrPromise = _("So‘rov tushunarsiz. Sahifani yangilang va qayta urinib ko‘ring.")

# The console texts of the credential / prompt refusal codes (credentials.RotationRefused, LLM / Vault codes).
CODE_MESSAGES: dict[str, StrOrPromise] = {
    "empty_key": _("Kalit bo‘sh."),
    "key_invalid": _("Kalit noto‘g‘ri ko‘rinishda."),
    "test_pending": _("Oldingi kalit hali tekshirilmoqda. Bir necha daqiqadan keyin qayta urinib ko‘ring."),
    "no_previous": _("Qaytariladigan oldingi kalit yo‘q."),
    "ai_not_configured": _("Faol kalit yo‘q yoki provayder kalitni qabul qilmadi."),
    "provider_unavailable": _("Provayder vaqtincha javob bermadi."),
    "ai_refused": _("Provayder so‘rovni rad etdi."),
    "ai_bad_output": _("Provayder noto‘g‘ri javob qaytardi."),
    "vault_unavailable": _(
        "Vault vaqtincha ishlamayapti: hech narsa o‘zgarmadi. Keyinroq qayta urinib ko‘ring."
    ),
    "vault_not_found": _("Kalit versiyasi Vault’da topilmadi."),
    "vault_not_configured": _("Vault sozlanmagan: kalitni saqlab bo‘lmaydi."),
    "not_a_draft": _("Faqat qoralama versiyani tahrirlash va sinash mumkin."),
    "not_tested": _("Faqat sinovdan o‘tgan (yoki avval faol bo‘lgan) versiyani faollashtirish mumkin."),
    "test_unavailable": _("Sinov ishga tushmadi."),
}


def code_message(code: str) -> str:
    text = CODE_MESSAGES.get(code)
    if text is None:
        return gettext("Amal bajarilmadi (kod: %(code)s).") % {"code": code}
    return str(text)


def _actor(user: Any) -> str:
    if user is None:
        return "-"
    return str(getattr(user, "full_name", "") or getattr(user, "email", "") or user)


def nav(request: HttpRequest, active: str) -> list[dict[str, Any]]:
    items = [
        ("settings", gettext("Sozlamalar"), "tune", reverse("admin:assistant_settings")),
        ("providers", gettext("Provayderlar"), "hub", reverse("admin:assistant_providers")),
        ("prompts", gettext("Promptlar"), "edit_note", reverse("admin:assistant_prompts")),
        ("jobs", gettext("So‘rovlar va xarajat"), "receipt_long", reverse("admin:assistant_jobs")),
        ("scenes", gettext("Mahsulot sahnalari"), "photo_filter", reverse("admin:assistant_scenes")),
    ]
    return [
        {"title": title, "icon": icon, "link": link, "active": key == active}
        for key, title, icon, link in items
    ]


class ConsolePage(UnfoldSiteViewMixin, View):  # type: ignore[misc]  # Unfold is untyped
    """A superuser-only console page."""

    title: StrOrPromise = _("AI yordamchi")
    section = "settings"
    http_method_names = ["get", "post"]

    def has_permission(self) -> bool:
        return is_superuser(self.request)

    def page(self, request: HttpRequest, page_template: str, status: int = 200, **extra: Any) -> HttpResponse:
        context = {**self.get_context_data(), "ai_nav": nav(request, self.section), **extra}
        return render(request, f"{TEMPLATES}/{page_template}", context, status=status)


# --- settings and budgets -----------------------------------------------------------------------------------


def _settings_row() -> AssistantSettings:
    return AssistantSettings.objects.select_related("updated_by").get(pk=SETTINGS_PK)


class SettingsView(ConsolePage):
    title = _("AI yordamchi: sozlamalar")
    section = "settings"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render(request, forms.SettingsForm(instance=_settings_row()))

    def post(self, request: HttpRequest) -> HttpResponse:
        row = _settings_row()
        form = forms.SettingsForm(request.POST, instance=row)
        if not form.is_valid():
            return self._render(request, form, status=400)
        changes = form.changes()
        if not changes:
            messages.info(request, NOTHING_CHANGED)
        else:
            # form.is_valid() applied the values to its instance: the service re-reads the row itself
            writes.update_settings(changes, actor=request.user, request=request)
            messages.success(request, SAVED)
        return HttpResponseRedirect(reverse("admin:assistant_settings"))

    def _render(self, request: HttpRequest, form: forms.SettingsForm, status: int = 200) -> HttpResponse:
        row = _settings_row()
        return self.page(
            request,
            "settings.html",
            status=status,
            form=form,
            row=row,
            updated_by=_actor(row.updated_by),
            usage=reports.usage(),
            modes={"ai": django_settings.AI_MODE, "vault": django_settings.VAULT_MODE},
        )


# --- providers and the API key ------------------------------------------------------------------------------


def _provider(provider: str) -> ProviderConfig:
    row = ProviderConfig.objects.select_related("rotated_by", "updated_by").filter(provider=provider).first()
    if row is None:
        raise Http404
    return row


def credential_panel(row: ProviderConfig) -> dict[str, Any]:
    """What the console shows of the key: metadata only (never a value)."""
    return {
        "configured": row.configured,
        "active_version": row.active_version,
        "previous_version": row.previous_version,
        "pending_version": row.pending_version,
        "rotated_at": row.rotated_at,
        "rotated_by": _actor(row.rotated_by) if row.rotated_by_id else "-",
        "test_status": row.test_status,
        "test_status_label": gettext(enums.CredentialTestStatus(row.test_status).label),
        "tested_at": row.tested_at,
        "test_error": code_message(row.test_error_code) if row.test_error_code else "",
        "vault_path": credentials.vault_path(row),
    }


class ProvidersView(ConsolePage):
    title = _("AI yordamchi: provayderlar")
    section = "providers"

    def get(self, request: HttpRequest) -> HttpResponse:
        rows = [
            {"row": row, "credential": credential_panel(row)}
            for row in ProviderConfig.objects.select_related("rotated_by", "updated_by").order_by("provider")
        ]
        active = AssistantSettings.objects.get(pk=SETTINGS_PK).active_provider
        return self.page(request, "providers.html", rows=rows, active_provider=active)


class ProviderView(ConsolePage):
    title = _("AI provayder")
    section = "providers"

    def get(self, request: HttpRequest, provider: str) -> HttpResponse:
        row = _provider(provider)
        return self._render(request, row, forms.ProviderForm(instance=row))

    def post(self, request: HttpRequest, provider: str) -> HttpResponse:
        row = _provider(provider)
        back = HttpResponseRedirect(reverse("admin:assistant_provider", args=[row.provider]))
        action = request.POST.get("action", "")
        if action == "test_key":
            try:
                credentials.start_active_test(row.provider, actor=request.user, request=request)
            except credentials.RotationRefused as exc:
                messages.warning(request, code_message(exc.code))
            else:
                messages.success(
                    request, gettext("Faol kalit tekshiruvga yuborildi. Natija shu sahifada ko‘rinadi.")
                )
            return back
        if action != "params":
            messages.error(request, UNKNOWN_ACTION)
            return back
        form = forms.ProviderForm(request.POST, instance=row)
        if not form.is_valid():
            return self._render(request, _provider(provider), form, status=400)
        changes = form.changes()
        if not changes:
            messages.info(request, NOTHING_CHANGED)
        else:
            writes.update_provider(row.provider, changes, actor=request.user, request=request)
            messages.success(request, SAVED)
        return back

    def _render(
        self, request: HttpRequest, row: ProviderConfig, form: Any, status: int = 200
    ) -> HttpResponse:
        return self.page(
            request,
            "provider.html",
            status=status,
            row=row,
            form=form,
            credential=credential_panel(row),
            updated_by=_actor(row.updated_by),
            provider_title=row.get_provider_display(),
        )


@method_decorator(sensitive_post_parameters("api_key"), name="dispatch")
class RotateKeyView(ConsolePage):
    """«Kalitni almashtirish»: write-only; the key goes to Vault as an inactive version, a worker tests it."""

    title = _("API kalitni almashtirish")
    section = "providers"

    def get(self, request: HttpRequest, provider: str) -> HttpResponse:
        return self._render(request, _provider(provider), forms.RotateKeyForm())

    @method_decorator(sensitive_variables("api_key", "secret", "form"))
    def post(self, request: HttpRequest, provider: str) -> HttpResponse:
        row = _provider(provider)
        form = forms.RotateKeyForm(request.POST)
        if not form.is_valid():
            return self._render(request, row, form, status=400)  # render_value=False: no value
        secret = Secret(form.cleaned_data["api_key"])
        try:
            pending = credentials.start_rotation(row.provider, secret, actor=request.user, request=request)
        except credentials.RotationRefused as exc:
            return self._render(request, row, forms.RotateKeyForm(), status=409, error=code_message(exc.code))
        except VaultError as exc:
            return self._render(request, row, forms.RotateKeyForm(), status=503, error=code_message(exc.code))
        messages.success(
            request,
            gettext(
                "Yangi kalit Vault’ga %(version)s-versiya sifatida yozildi va tekshirilmoqda. "
                "Tekshiruvdan o‘tsa "
                "faol bo‘ladi; o‘tmasa eski kalit ishlashda davom etadi."
            )
            % {"version": pending.version},
        )
        return HttpResponseRedirect(reverse("admin:assistant_provider", args=[row.provider]))

    def _render(
        self, request: HttpRequest, row: ProviderConfig, form: Any, status: int = 200, error: str = ""
    ) -> HttpResponse:
        return self.page(
            request,
            "rotate_key.html",
            status=status,
            row=row,
            form=form,
            error=error,
            credential=credential_panel(row),
            provider_title=row.get_provider_display(),
        )


class RollbackKeyView(ConsolePage):
    """«Oldingi kalitga qaytish»: a confirm page; the POST swaps active and previous and tests the result."""

    title = _("Oldingi kalitga qaytish")
    section = "providers"

    def get(self, request: HttpRequest, provider: str) -> HttpResponse:
        row = _provider(provider)
        return self.page(
            request,
            "rollback_key.html",
            row=row,
            credential=credential_panel(row),
            provider_title=row.get_provider_display(),
        )

    def post(self, request: HttpRequest, provider: str) -> HttpResponse:
        row = _provider(provider)
        back = HttpResponseRedirect(reverse("admin:assistant_provider", args=[row.provider]))
        if not forms.ConfirmForm(request.POST).is_valid():
            messages.error(request, UNKNOWN_ACTION)
            return back
        try:
            updated = credentials.rollback(row.provider, actor=request.user, request=request)
        except credentials.RotationRefused as exc:
            messages.warning(request, code_message(exc.code))
            return back
        messages.success(
            request,
            gettext("%(version)s-versiya yana faol. U qayta tekshirilmoqda.")
            % {"version": updated.active_version},
        )
        return back


# --- prompt templates and versions --------------------------------------------------------------------------


def _template(purpose: str) -> PromptTemplate:
    row = PromptTemplate.objects.filter(purpose=purpose).first()
    if row is None:
        raise Http404
    return row


def _purpose_label(template: PromptTemplate) -> str:
    return gettext(enums.PromptPurpose(template.purpose).label)


def _status_label(status: str) -> str:
    return gettext(enums.PromptVersionStatus(status).label)


class PromptsView(ConsolePage):
    title = _("AI yordamchi: promptlar")
    section = "prompts"

    def get(self, request: HttpRequest) -> HttpResponse:
        rows = []
        for template in PromptTemplate.objects.order_by("id"):
            versions = list(template.versions.all())
            active = next((v for v in versions if v.status == enums.PromptVersionStatus.ACTIVE), None)
            draft = next((v for v in versions if v.status in writes.EDITABLE_STATUSES), None)
            rows.append(
                {
                    "template": template,
                    "label": _purpose_label(template),
                    "active": active,
                    "draft": draft,
                    "count": len(versions),
                }
            )
        return self.page(request, "prompts.html", rows=rows)


class PromptTemplateView(ConsolePage):
    """A template's versions (newest first) and «Yangi qoralama»."""

    title = _("Prompt shabloni")
    section = "prompts"

    def get(self, request: HttpRequest, purpose: str) -> HttpResponse:
        template = _template(purpose)
        versions: list[dict[str, Any]] = []
        for row in template.versions.select_related("created_by", "activated_by").order_by("-number"):
            version = writes.refresh_test_status(row)
            versions.append(
                {
                    "version": version,
                    "status_label": _status_label(version.status),
                    "created_by": _actor(row.created_by),
                }
            )
        return self.page(
            request,
            "prompt_template.html",
            template=template,
            label=_purpose_label(template),
            versions=versions,
        )

    def post(self, request: HttpRequest, purpose: str) -> HttpResponse:
        template = _template(purpose)
        if request.POST.get("action") != "new_draft":
            messages.error(request, UNKNOWN_ACTION)
            return HttpResponseRedirect(reverse("admin:assistant_prompt", args=[template.purpose]))
        draft = writes.new_draft(template.purpose, actor=request.user, request=request)
        return HttpResponseRedirect(
            reverse("admin:assistant_prompt_version", args=[template.purpose, draft.number])
        )


class PromptVersionView(ConsolePage):
    """One version: its text (a textarea for a draft), its last test, «Sinash» and «Faollashtirish»."""

    title = _("Prompt versiyasi")
    section = "prompts"

    def _version(self, purpose: str, number: int) -> PromptVersion:
        template = _template(purpose)
        version = (
            PromptVersion.objects.select_related("template", "created_by", "activated_by")
            .filter(template=template, number=number)
            .first()
        )
        if version is None:
            raise Http404
        return writes.refresh_test_status(version)

    def get(self, request: HttpRequest, purpose: str, number: int) -> HttpResponse:
        version = self._version(purpose, number)
        return self._render(request, version)

    def post(self, request: HttpRequest, purpose: str, number: int) -> HttpResponse:
        version = self._version(purpose, number)
        here = HttpResponseRedirect(reverse("admin:assistant_prompt_version", args=[purpose, number]))
        action = request.POST.get("action", "")
        try:
            if action == "save":
                form = forms.InstructionsForm(request.POST)
                if not form.is_valid():
                    return self._render(request, version, edit_form=form, status=400)
                before = version.instructions
                writes.edit_draft(
                    version.pk, form.cleaned_data["instructions"], actor=request.user, request=request
                )
                messages.success(
                    request, SAVED if before != form.cleaned_data["instructions"] else NOTHING_CHANGED
                )
                return here
            if action == "test":
                test_form = forms.PromptTestForm(request.POST)
                if not test_form.is_valid():
                    return self._render(request, version, test_form=test_form, status=400)
                job = writes.start_test(
                    version.pk, test_form.cleaned_data["product"], actor=request.user, request=request
                )
                messages.success(
                    request,
                    gettext("Sinov boshlandi (so‘rov #%(job)s). Natijani ko‘rish uchun sahifani yangilang.")
                    % {"job": job.pk},
                )
                return here
            if action == "activate":
                if not forms.ConfirmForm(request.POST).is_valid():
                    messages.error(request, UNKNOWN_ACTION)
                    return here
                activated = writes.activate(version.pk, actor=request.user, request=request)
                messages.success(
                    request,
                    gettext("%(number)s-versiya faollashtirildi: keyingi so‘rovlar undan foydalanadi.")
                    % {"number": activated.number},
                )
                return here
        except writes.PromptRefused as exc:
            messages.warning(request, code_message(exc.code))
            return here
        except (ValidationError, APIException) as exc:  # the generation service refused the test
            messages.warning(request, _refusal_text(exc))
            return here
        messages.error(request, UNKNOWN_ACTION)
        return here

    def _render(
        self,
        request: HttpRequest,
        version: PromptVersion,
        *,
        edit_form: forms.InstructionsForm | None = None,
        test_form: forms.PromptTestForm | None = None,
        status: int = 200,
    ) -> HttpResponse:
        editable = version.status in writes.EDITABLE_STATUSES
        last_test = version.last_test or {}
        job_id = last_test.get("job_id")
        test_job = GenerationJob.objects.filter(pk=int(job_id)).first() if isinstance(job_id, int) else None
        return self.page(
            request,
            "prompt_version.html",
            status=status,
            version=version,
            template=version.template,
            label=_purpose_label(version.template),
            status_label=_status_label(version.status),
            editable=editable,
            activatable=version.status in writes.ACTIVATABLE_STATUSES,
            rollback=version.status == enums.PromptVersionStatus.RETIRED,
            edit_form=edit_form or forms.InstructionsForm(initial={"instructions": version.instructions}),
            test_form=test_form or forms.PromptTestForm(initial={"product": last_test.get("product_id")}),
            test_job=test_job,
            test_job_state=gettext(enums.JobState(test_job.state).label) if test_job is not None else "",
            test_suggestions=json.dumps(test_job.suggestions, ensure_ascii=False, indent=2)
            if test_job is not None and test_job.suggestions
            else "",
            sample_products=_sample_products(),
            created_by=_actor(version.created_by),
            activated_by=_actor(version.activated_by),
        )


def _refusal_text(exc: ValidationError | APIException) -> str:
    """The service's Uzbek message (an ``N_()`` literal), in the console's language."""
    if isinstance(exc, APIException):
        detail: Any = exc.detail
        if isinstance(detail, dict):
            detail = next(iter(detail.values()), "")
        if isinstance(detail, list):
            detail = detail[0] if detail else ""
        return gettext(str(detail))
    return gettext(str(exc.messages[0])) if exc.messages else str(code_message("test_unavailable"))


def _sample_products() -> list[dict[str, Any]]:
    from glowderma.catalog import selectors

    return [
        {"id": row.pk, "name": row.name_uz}
        for row in selectors.product_queryset().order_by("-updated_at")[:30]
    ]


# --- generation jobs and usage ------------------------------------------------------------------------------

JOB_FILTERS = ("state", "mode", "model", "user", "date_from", "date_to")


class JobsView(ConsolePage):
    """Every generation (newest first), filtered by state, mode, model, staff member and day; usage totals."""

    title = _("AI so‘rovlari va xarajat")
    section = "jobs"
    per_page = 50

    def get(self, request: HttpRequest) -> HttpResponse:
        params = {name: request.GET.get(name, "").strip() for name in JOB_FILTERS}
        jobs = GenerationJob.objects.select_related("created_by", "product").order_by("-created_at", "-id")
        if params["state"] in enums.JobState.values:
            jobs = jobs.filter(state=params["state"])
        if params["mode"] in enums.AssistantMode.values:
            jobs = jobs.filter(mode=params["mode"])
        if params["model"]:
            jobs = jobs.filter(model_id=params["model"])
        if params["user"].isdigit():
            jobs = jobs.filter(created_by_id=int(params["user"]))
        bounds = timeutils.date_range_bounds(_date(params["date_from"]), _date(params["date_to"]))
        if bounds:
            jobs = jobs.filter(**{f"created_at__{op}": value for op, value in bounds.items()})
        page = Paginator(jobs, self.per_page).get_page(request.GET.get("p"))
        query = urlencode({name: value for name, value in params.items() if value})
        usage = reports.usage()
        return self.page(
            request,
            "jobs.html",
            page=page,
            params=params,
            query=query,
            states=[(value, gettext(label)) for value, label in enums.JobState.choices],
            modes=[(value, gettext(label)) for value, label in enums.AssistantMode.choices],
            models=sorted(set(GenerationJob.objects.exclude(model_id="").values_list("model_id", flat=True))),
            users=_job_users(),
            usage=usage,
            periods=[
                {"title": gettext("Bugun"), "totals": usage["day"]},
                {"title": gettext("Shu oy"), "totals": usage["month"]},
            ],
        )


def _date(value: str) -> Any:
    from datetime import date

    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _job_users() -> list[tuple[int, str]]:
    from django.contrib.auth import get_user_model

    ids = GenerationJob.objects.values_list("created_by_id", flat=True).distinct()
    return [(user.pk, _actor(user)) for user in get_user_model().objects.filter(pk__in=ids).order_by("pk")]


class JobView(ConsolePage):
    title = _("AI so‘rovi")
    section = "jobs"

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        job = (
            GenerationJob.objects.select_related("created_by", "product", "applied_by").filter(pk=pk).first()
        )
        if job is None:
            raise Http404
        versions = PromptVersion.objects.select_related("template").filter(
            pk__in=[value for value in (job.prompt_versions or {}).values() if isinstance(value, int)]
        )
        return self.page(
            request,
            "job.html",
            job=job,
            created_by=_actor(job.created_by),
            applied_by=_actor(job.applied_by),
            state_label=gettext(enums.JobState(job.state).label),
            mode_label=gettext(enums.AssistantMode(job.mode).label),
            outcome_label=gettext(enums.AssistantJobOutcome(job.outcome).label),
            prompt_versions=versions,
            documents=[
                (gettext("Takliflar"), json.dumps(job.suggestions, ensure_ascii=False, indent=2)),
                (gettext("Ogohlantirishlar"), json.dumps(job.warnings, ensure_ascii=False, indent=2)),
                (
                    gettext("Boshlang‘ich qiymatlar"),
                    json.dumps(job.input_snapshot, ensure_ascii=False, indent=2),
                ),
                (gettext("Maydonlar"), json.dumps(job.fields, ensure_ascii=False)),
            ],
        )


# --- product scenes -----------------------------------------------------------------------------------------


def _scene_row() -> SceneSettings:
    return SceneSettings.objects.select_related("updated_by").get(pk=SETTINGS_PK)


class ScenesView(ConsolePage):
    """Product scenes: switches, cut-out source, models, limits, prices, style; the templates; spend."""

    title = _("AI yordamchi: mahsulot sahnalari")
    section = "scenes"

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render(request, forms.SceneSettingsForm(instance=_scene_row()))

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.POST.get("action") == "missing_previews":
            return self._missing_previews(request)
        form = forms.SceneSettingsForm(request.POST, instance=_scene_row())
        if not form.is_valid():
            return self._render(request, form, status=400)
        changes = form.changes()
        if not changes:
            messages.info(request, NOTHING_CHANGED)
        else:
            writes.update_scene_settings(changes, actor=request.user, request=request)
            messages.success(request, SAVED)
        return HttpResponseRedirect(reverse("admin:assistant_scenes"))

    def _missing_previews(self, request: HttpRequest) -> HttpResponse:
        from glowderma.assistant.scenes import services as scene_services

        try:
            drawn, queued = scene_services.start_missing_previews(actor=request.user)
        except APIException as exc:  # the scene budget is spent
            messages.error(request, str(exc.detail))
        else:
            messages.success(
                request,
                gettext(
                    "Studiya namunalari chizildi: %(drawn)s. AI namunalari navbatga qo‘yildi: %(queued)s."
                )
                % {"drawn": drawn, "queued": queued},
            )
        return HttpResponseRedirect(reverse("admin:assistant_scenes"))

    def _render(self, request: HttpRequest, form: forms.SceneSettingsForm, status: int = 200) -> HttpResponse:
        from glowderma.assistant.scenes import services as scene_services

        row = _scene_row()
        recent = SceneJob.objects.select_related("created_by").order_by("-created_at")[:20]
        return self.page(
            request,
            "scenes.html",
            status=status,
            form=form,
            row=row,
            updated_by=_actor(row.updated_by),
            templates=SceneTemplate.objects.order_by("position", "id"),
            spent_today=scene_services.spent_today(),
            spent_month=scene_services.spent_this_month(),
            recent=recent,
        )


class SceneTemplatePreviewView(ConsolePage):
    """A template's sample picture, served by the console itself: the console's Content-Security-Policy
    allows images from its own origin only (``img-src 'self'``), and media live on the site's domain."""

    http_method_names = ["get"]

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        row = SceneTemplate.objects.filter(pk=pk).first()
        if row is None or not row.preview:
            raise Http404
        response = FileResponse(row.preview.open("rb"), content_type="image/webp")
        # The name is content-hashed (a new picture is a new name): cacheable, privately (staff only).
        response["Cache-Control"] = "private, max-age=86400"
        return response


class SceneTemplateView(ConsolePage):
    title = _("AI yordamchi: sahna shabloni")
    section = "scenes"

    def _template(self, pk: int) -> SceneTemplate:
        row = SceneTemplate.objects.filter(pk=pk).first()
        if row is None:
            raise Http404
        return row

    def get(self, request: HttpRequest, pk: int) -> HttpResponse:
        row = self._template(pk)
        return self._render(request, row, forms.SceneTemplateForm(instance=row))

    def post(self, request: HttpRequest, pk: int) -> HttpResponse:
        row = self._template(pk)
        if request.POST.get("action") == "preview":
            return self._preview(request, row)
        form = forms.SceneTemplateForm(request.POST, instance=row)
        if not form.is_valid():
            return self._render(request, self._template(pk), form, status=400)
        changes = form.changes()
        if not changes:
            messages.info(request, NOTHING_CHANGED)
        else:
            writes.update_scene_template(pk, changes, actor=request.user, request=request)
            messages.success(request, SAVED)
        return HttpResponseRedirect(reverse("admin:assistant_scene_template", args=[pk]))

    def _preview(self, request: HttpRequest, row: SceneTemplate) -> HttpResponse:
        """«Namuna yaratish»: a studio preview is drawn at once; an AI one is a billed job on the worker."""
        from glowderma.assistant.scenes import services as scene_services

        try:
            job = scene_services.start_preview(row.pk, actor=request.user)
        except APIException as exc:  # the scene budget is spent
            messages.error(request, str(exc.detail))
        else:
            messages.success(
                request,
                gettext("Namuna chizildi.")
                if job is None
                else gettext("Namuna tayyorlanmoqda (taxminan 30 soniya). Keyin sahifani yangilang."),
            )
        return HttpResponseRedirect(reverse("admin:assistant_scene_template", args=[row.pk]))

    def _render(
        self, request: HttpRequest, row: SceneTemplate, form: forms.SceneTemplateForm, status: int = 200
    ) -> HttpResponse:
        from glowderma.assistant.scenes import services as scene_services

        preview_job = (
            SceneJob.objects.filter(kind=enums.SceneJobKind.PREVIEW, template=row)
            .order_by("-created_at")
            .first()
        )
        return self.page(
            request,
            "scene_template.html",
            status=status,
            form=form,
            row=row,
            preview_job=preview_job,
            preview_error=scene_services.message_of(preview_job.error_code)
            if preview_job is not None and preview_job.error_code
            else "",
        )
