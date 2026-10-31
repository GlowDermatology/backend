"""Safe audited actions of the ops console (plan §4): the few one-click fixes next to the read-only records.

Every action is declared with :func:`ops_action` on a ``ReadOnlyAdmin`` that mixes in :class:`OpsActionsMixin`
and is listed in Unfold's ``actions_list`` (changelist button) or ``actions_detail`` (change page button);
Unfold builds the URL (``<object_id>/<url_path>/`` or ``<url_path>/``) behind ``admin_site.admin_view`` (an
active staff session, ``csrf_protect``). The decorator then guarantees, for every action alike:

* **confirmation** — a ``GET`` never acts: it renders the confirm step (Unfold's modal through ``hx-get``
  with ``hx-select="#dialog"``, or the same page in the console layout without JavaScript); only a ``POST``
  of the confirm form (CSRF token + the hidden ``confirm`` field) runs the action; other methods answer 405;
* **permissions** — active staff, or managers only (``StaffUser.is_manager`` / superuser) for
  ``manager_only`` actions, checked on every request (Unfold's ``allowed_permissions`` only hides buttons);
* **availability** — ``available(model_admin, request, obj)`` returns why the action makes no sense for that
  row ("" when it does): the change-page button is hidden (:meth:`OpsActionsMixin.get_actions_detail`) and a
  direct request is refused with that reason;
* **audit** — the action returns an :class:`ActionResult`; when it changed something, the ``ops.*`` row is
  written with the staff member as the actor, in the same transaction as the effect (the services keep
  writing their own domain rows, e.g. ``outbox.requeued``);
* **feedback** — an Uzbek success / warning / error message, then a redirect (PRG) to the change page or the
  changelist (``next`` when it is a safe same-host URL).

Actions call the owning module's services or enqueue its existing Celery tasks; a module below the one that
owns a task enqueues it by its registered name (:func:`enqueue_task`, like an outbox string topic), never by
import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.contrib import messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import models, transaction
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from unfold.enums import ActionVariant

from glowderma.core import audit

if TYPE_CHECKING:
    from django.contrib.admin import ModelAdmin

    _MixinBase = ModelAdmin[models.Model]
else:
    _MixinBase = object

logger = logging.getLogger(__name__)

CONFIRM_TEMPLATE = "admin/ops/confirm_action.html"
FAILED_MESSAGE = "Amal bajarilmadi: kutilmagan xato. Tafsilotlar server jurnalida."
MANAGER_ONLY_NOTE = "Faqat menejerlar uchun."

__all__ = [
    "ActionResult",
    "ActionVariant",
    "ConfirmForm",
    "OpsActionsMixin",
    "done",
    "enqueue_task",
    "failed",
    "is_manager",
    "ops_action",
    "refused",
]


def is_active_staff(request: HttpRequest) -> bool:
    """``core.admin.is_active_staff``, imported late: ``core.admin`` declares its actions with this module."""
    from glowderma.core.admin import is_active_staff as predicate

    return predicate(request)


def is_manager(request: HttpRequest) -> bool:
    """An active staff member with the manager flag (``StaffUser.is_manager``) or a superuser."""
    user = request.user
    return is_active_staff(request) and bool(
        getattr(user, "is_superuser", False) or getattr(user, "is_manager", False)
    )


@dataclass(frozen=True, slots=True)
class AuditFacts:
    action: str  # ``ops.<what>``
    entity_type: str
    entity_id: str
    before: Any = None
    after: Any = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an action did: the message staff see and, when something changed, the ``ops.*`` audit row."""

    level: int
    message: str
    audit: AuditFacts | None = None


def done(
    message: str,
    *,
    audit_action: str,
    entity_type: str,
    entity_id: Any,
    before: Any = None,
    after: Any = None,
) -> ActionResult:
    """The action changed something (or queued work): success message + the ``ops.*`` audit row."""
    return ActionResult(
        messages.SUCCESS, message, AuditFacts(audit_action, entity_type, str(entity_id), before, after)
    )


def refused(message: str) -> ActionResult:
    """Nothing was done (already done, a guard said no): a warning, no audit row."""
    return ActionResult(messages.WARNING, message)


def failed(message: str) -> ActionResult:
    """The action could not run (a provider is fake in production, a service refused): an error, no audit."""
    return ActionResult(messages.ERROR, message)


def enqueue_task(name: str, *args: Any, **kwargs: Any) -> Any:
    """``delay()`` the Celery task registered as ``name`` (``commerce.poll_bts``): the console action of a
    lower module starts an upper module's task without importing it. A broker error propagates (the action
    reports it and its audit row is rolled back)."""
    from celery import current_app  # type: ignore[import-untyped]

    if name not in current_app.tasks:
        # A web process imports a module's tasks.py only when something uses it; ``autodiscover_tasks``
        # registers the rest when the loader imports its default modules (what a worker does at start).
        current_app.loader.import_default_modules()
    return current_app.tasks[name].delay(*args, **kwargs)


class ConfirmForm(forms.Form):
    """The confirm step: nothing but the CSRF token, the hidden ``confirm`` flag and the return URL.

    Subclasses add the fields an action needs (a topic to requeue); ``request`` and ``obj`` (``None`` for a
    changelist action) are available to build choices."""

    confirm = forms.BooleanField(required=True, initial=True, widget=forms.HiddenInput)
    next = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args: Any, request: HttpRequest, obj: models.Model | None, **kwargs: Any) -> None:
        self.request = request
        self.obj = obj
        super().__init__(*args, **kwargs)


Availability = Callable[[Any, HttpRequest, Any], str]
Notes = Callable[[Any, HttpRequest, Any], list[str]]
ActionFunc = Callable[..., ActionResult]


def _safe_next(request: HttpRequest, candidate: str) -> str:
    if candidate and url_has_allowed_host_and_scheme(
        candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return candidate
    return ""


def _default_back(model_admin: Any, obj: models.Model | None) -> str:
    opts = model_admin.model._meta
    site = model_admin.admin_site.name
    if obj is not None:
        return reverse(f"{site}:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
    return reverse(f"{site}:{opts.app_label}_{opts.model_name}_changelist")


def ops_action(
    *,
    description: str,
    title: str,
    body: str,
    submit: str,
    icon: str,
    variant: ActionVariant = ActionVariant.PRIMARY,
    manager_only: bool = False,
    form_class: type[ConfirmForm] = ConfirmForm,
    url_path: str | None = None,
    available: Availability | None = None,
    notes: Notes | None = None,
) -> Callable[[ActionFunc], Callable[..., HttpResponse]]:
    """Declare a console action (module doc). The decorated method is called as
    ``method(self, request, obj, form)`` (``obj`` is ``None`` on a changelist action) inside a transaction
    and returns an :class:`ActionResult`.

    ``description`` is the button, ``title`` / ``body`` / ``submit`` the confirm step; ``notes(model_admin,
    request, obj)`` adds lines to it (what the row is about, a topic's warning)."""

    def decorate(func: ActionFunc) -> Callable[..., HttpResponse]:
        @wraps(func)
        def view(
            model_admin: Any, request: HttpRequest, object_id: str | None = None, **_kwargs: Any
        ) -> HttpResponse:
            if not is_active_staff(request) or (manager_only and not is_manager(request)):
                raise PermissionDenied
            if request.method not in ("GET", "POST"):
                return HttpResponseNotAllowed(["GET", "POST"])
            obj = None
            if object_id is not None:
                obj = model_admin.get_object(request, unquote(object_id))
                if obj is None:
                    raise Http404
            refusal = available(model_admin, request, obj) if available is not None else ""
            requested_next = request.POST.get("next", "") if request.method == "POST" else ""
            back = _safe_next(request, requested_next or request.GET.get("next", "")) or _default_back(
                model_admin, obj
            )
            if request.method == "POST":
                if refusal:
                    messages.warning(request, refusal)
                    return HttpResponseRedirect(back)
                form = form_class(request.POST, request=request, obj=obj)
                if form.is_valid():
                    messages.add_message(request, *_run(func, model_admin, request, obj, form))
                    return HttpResponseRedirect(back)
            else:
                form = form_class(initial={"confirm": True, "next": back}, request=request, obj=obj)
            object_title = _object_title(model_admin, obj)
            context = {
                **model_admin.admin_site.each_context(request),
                "opts": model_admin.model._meta,
                "title": title,
                "subtitle": object_title,
                # Unfold's header breadcrumb renders ``original``: the readable title, not the English str()
                "original": _titled(obj, object_title),
                "object_title": object_title,
                "action_title": title,
                "action_body": body,
                "action_notes": [
                    *(notes(model_admin, request, obj) if notes is not None else []),
                    *([MANAGER_ONLY_NOTE] if manager_only else []),
                ],
                "action_submit": submit,
                "action_variant": variant.value,
                "action_url": request.path,
                "refusal": refusal,
                "form": form,
                "back_url": back,
                "in_modal": request.headers.get("HX-Request") == "true",
            }
            return TemplateResponse(request, CONFIRM_TEMPLATE, context)

        # The attributes Unfold reads from an ``@action`` (unfold.decorators.action) plus ours.
        vars(view).update(
            {
                "allowed_permissions": ("ops_manager",) if manager_only else ("ops",),
                "short_description": description,
                "url_path": url_path or func.__name__.replace("_", "-"),
                "icon": icon,
                "dialog": {"title": title, "description": body},
                "variant": variant,
                "extra_options": {},
                "attrs": {},
                "original_function_name": func.__name__,
                "ops_available": available,
                "ops_manager_only": manager_only,
            }
        )
        return view

    return decorate


def _object_title(model_admin: Any, obj: models.Model | None) -> str | None:
    if obj is None:
        return None
    titled = getattr(model_admin, "object_title", None)
    return str(titled(obj)) if callable(titled) else str(obj)


def _titled(obj: models.Model | None, title: str | None) -> Any:
    from glowderma.core.admin import TitledObject

    return None if obj is None else TitledObject(obj, title or str(obj))


def _run(
    func: ActionFunc, model_admin: Any, request: HttpRequest, obj: models.Model | None, form: ConfirmForm
) -> tuple[int, str]:
    try:
        with transaction.atomic():
            result = func(model_admin, request, obj, form)
            if result.audit is not None:
                audit.record(
                    action=result.audit.action,
                    entity_type=result.audit.entity_type,
                    entity_id=result.audit.entity_id,
                    actor=request.user,
                    actor_type="staff",
                    request=request,
                    before=result.audit.before,
                    after=result.audit.after,
                    reason="ops console",
                )
    except Exception:
        logger.exception("ops.action_failed action=%s", func.__name__)
        return messages.ERROR, FAILED_MESSAGE
    return result.level, result.message


class OpsActionsMixin(_MixinBase):
    """Mix into a ``ReadOnlyAdmin`` that declares :func:`ops_action` methods (put it first in the bases)."""

    def has_ops_permission(self, request: HttpRequest, object_id: Any = None) -> bool:
        return is_active_staff(request)

    def has_ops_manager_permission(self, request: HttpRequest, object_id: Any = None) -> bool:
        return is_manager(request)

    def get_actions_detail(self, request: HttpRequest, object_id: int | str) -> list[Any]:
        # Unfold filters by ``allowed_permissions``; an action that makes no sense for this record (a
        # requeue of a delivered message, a revoke of an ended session) is hidden as well.
        actions = cast(list[Any], super().get_actions_detail(request, object_id))  # type: ignore[misc]
        if not actions:
            return actions
        obj = self.get_object(request, unquote(str(object_id)))
        if obj is None:
            return []
        return [
            action
            for action in actions
            if getattr(action.method, "ops_available", None) is None
            or not action.method.ops_available(self, request, obj)
        ]
