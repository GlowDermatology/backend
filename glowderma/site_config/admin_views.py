"""The ops console's settings page, ``/django-admin/settings/`` (``admin:site_config_settings``).

The store settings, the contacts and the legal texts are edited here only — behind the console's second
factor (``core.admin_site.OpsAdminSite.has_permission``), its network gate and its session limits — and no
longer through the admin API: ``settings/`` and ``settings/terms/publish/`` left it with the Next.js admin
page they served.

The page keeps that page's shape: three cards, each saved on its own (:mod:`glowderma.site_config.forms`),
and «E’lon qilish», which publishes the saved terms and privacy drafts as a new ``terms_version`` after a
confirmation.
Every write goes through :mod:`glowderma.site_config.services` (row lock, audit ``settings.updated`` /
``settings.terms_published``, public-settings cache dropped on commit). A save answers with a redirect back to
the page (POST/redirect/GET) and a message in the console's language; a refused form is rendered again with
the input kept.

Mounted by ``UNFOLD["SITE_VIEWS"]`` (``config/settings/ops_admin.py``), so ``admin_site.admin_view`` wraps it
(staff, 2FA, ``csrf_protect``, ``never_cache``); :meth:`SettingsConsoleView.has_permission` checks the grant
again (``core.admin.is_active_staff``, as every console page).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.views import View
from unfold.views import UnfoldSiteViewMixin

from glowderma.core import integration_modes
from glowderma.core.admin import is_active_staff
from glowderma.core.api.exceptions import Conflict
from glowderma.site_config import services
from glowderma.site_config.forms import (
    LANGUAGE_TABS,
    SECTION_FORMS,
    ContactsForm,
    LegalTextsForm,
    PublishForm,
    SectionForm,
    StoreForm,
)
from glowderma.site_config.models import SiteSettings

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

TEMPLATE = "site_config/console/settings.html"
URL_NAME = "admin:site_config_settings"
TITLE: StrOrPromise = _("Sozlamalar")

NOTHING_CHANGED: StrOrPromise = _("O‘zgarish yo‘q: saqlanadigan narsa topilmadi.")
UNKNOWN_SECTION: StrOrPromise = _("So‘rov tushunarsiz. Sahifani yangilang va qayta urinib ko‘ring.")
LEGAL_UNPUBLISHED_NOTE: StrOrPromise = _("Mijozlar yangi shartlarni e’lon qilganingizdan keyin ko‘radi.")
PUBLISHED_NOTE: StrOrPromise = _("Mijozlar ilovada yangi versiyani qabul qiladi.")
# The publication's confirmation, word for word as on the Next.js page (and in the e2e suites).
PUBLISH_TITLE: StrOrPromise = _("Shartlarning yangi versiyasini e’lon qilasizmi?")
PUBLISH_WARNING: StrOrPromise = _("Barcha mijozlardan shartlarni qayta qabul qilish so‘raladi.")
PUBLISH_BODY: StrOrPromise = _(
    "Saqlangan foydalanish shartlari va maxfiylik siyosati (barcha tillarda) yangi versiya sifatida e’lon "
    "qilinadi. Mijozlar yangi versiyani qabul qilmaguncha buyurtma bera olmaydi va qabulga yozila olmaydi. "
    "Bu amalni qaytarib bo‘lmaydi."
)


def _row() -> SiteSettings:
    # Reads never create the singleton (migration + post_migrate keep it): a missing row is a deploy error.
    return SiteSettings.objects.select_related("updated_by").get(pk=1)


def _actor_name(row: SiteSettings) -> str:
    user = row.updated_by
    if user is None:
        return gettext("Tizim")
    return str(getattr(user, "full_name", "") or getattr(user, "email", "") or user)


class SettingsConsoleView(UnfoldSiteViewMixin, View):  # type: ignore[misc]  # Unfold is untyped
    """GET renders the three cards; POST ``section`` = ``store`` | ``contacts`` | ``legal`` saves that card,
    ``section=publish`` publishes the terms."""

    title = TITLE
    http_method_names = ["get", "post"]

    def has_permission(self) -> bool:
        return is_active_staff(self.request)

    # --- GET ------------------------------------------------------------------------------------------------

    def get(self, request: HttpRequest) -> HttpResponse:
        return self._render(request, _row())

    # --- POST -----------------------------------------------------------------------------------------------

    def post(self, request: HttpRequest) -> HttpResponse:
        section = request.POST.get("section", "")
        if section == "publish":
            return self._publish(request)
        form_class = SECTION_FORMS.get(section)
        if form_class is None:
            messages.error(request, UNKNOWN_SECTION)
            return HttpResponseRedirect(reverse(URL_NAME))
        row = _row()
        form = form_class(row, data=request.POST)
        if not form.is_valid():
            return self._render(request, row, bound=form)
        changes = form.changes()
        if not changes:
            messages.info(request, NOTHING_CHANGED)
            return self._redirect(section)
        saved = services.update_settings(changes, actor=request.user, request=request)
        message = str(form.saved_message)
        if section == LegalTextsForm.section and services.unpublished_changes(saved):
            message = f"{message}. {LEGAL_UNPUBLISHED_NOTE}"
        messages.success(request, message)
        return self._redirect(section)

    def _publish(self, request: HttpRequest) -> HttpResponse:
        form = PublishForm(request.POST)
        if not form.is_valid():
            messages.error(request, UNKNOWN_SECTION)
            return self._redirect(LegalTextsForm.section)
        try:
            row = services.publish_terms(
                actor=request.user,
                request=request,
                expected_fingerprint=form.cleaned_data["fingerprint"],
            )
        except Conflict as exc:
            # Shown inside the reopened confirmation (the Next.js dialog showed it there too). The service's
            # message is plain Uzbek (``services.PUBLISH_STALE`` / ``TERMS_INCOMPLETE``): translated here.
            return self._render(request, _row(), publish_error=gettext(str(exc.detail)))
        published = gettext("Shartlar e’lon qilindi: %(version)s.") % {"version": row.terms_version}
        messages.success(request, f"{published} {PUBLISHED_NOTE}")
        return self._redirect(LegalTextsForm.section)

    # --- rendering ------------------------------------------------------------------------------------------

    def _redirect(self, section: str) -> HttpResponse:
        return HttpResponseRedirect(f"{reverse(URL_NAME)}#{section}")

    def _render(
        self,
        request: HttpRequest,
        row: SiteSettings,
        *,
        bound: SectionForm | None = None,
        publish_error: str | None = None,
    ) -> HttpResponse:
        forms: dict[str, SectionForm] = {
            section: (bound if bound is not None and bound.section == section else form_class(row))
            for section, form_class in SECTION_FORMS.items()
        }
        legal = forms[LegalTextsForm.section]
        assert isinstance(legal, LegalTextsForm)  # noqa: S101 - SECTION_FORMS maps "legal" to it
        document, language = legal.first_error_tab()
        mode = str(settings.INTEGRATIONS_MODE)
        context = {
            **self.get_context_data(),
            "row": row,
            "updated_by": _actor_name(row),
            "store": forms[StoreForm.section],
            "contacts": forms[ContactsForm.section],
            "legal": legal,
            "bound_section": bound.section if bound is not None else "",
            "focus_field": bound.first_error_field() if bound is not None else None,
            "legal_document": document,
            "legal_language": language,
            "language_tabs": LANGUAGE_TABS,
            "unpublished": services.unpublished_changes(row),
            "publish_form": PublishForm(initial={"fingerprint": services.publish_fingerprint(row)}),
            "publish_error": publish_error,
            "publish_title": PUBLISH_TITLE,
            "publish_warning": PUBLISH_WARNING,
            "publish_body": PUBLISH_BODY,
            "integrations_live": mode == "live",
            "integrations": integration_modes.provider_modes(),
        }
        return render(request, TEMPLATE, context)
