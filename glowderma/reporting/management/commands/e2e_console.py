"""``manage.py e2e_console <action> [--data JSON]``: data setup and database checks for the ops console's
browser suite (``e2e_admin/``, Playwright; ``e2e_admin/README.md``).

The suite drives a real server through a real browser; whatever it cannot see on a page (the audit row an
action wrote, a revoked session, a lockout in Redis) or must arrange beforehand (a staff member with a known
TOTP secret, a dead outbox message, an aged session) goes through this command, one JSON object per call on
the last line of stdout (``E2E_JSON:{…}``).

Refused unless the e2e settings are active: ``E2E_CONSOLE_ENABLED`` (only ``config.settings.e2e`` sets it)
together with ``DEV_FEATURES_ALLOWED`` (the production settings force both off), and never under a settings
module named ``production``. ``reset-db`` touches only databases named ``glowderma_e2e*``.
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, models
from django.urls import reverse
from django_otp import DEVICE_ID_SESSION_KEY
from django_otp.plugins.otp_static.models import StaticToken
from django_otp.plugins.otp_totp.models import TOTPDevice

from glowderma.accounts.api.guard import email_hash
from glowderma.accounts.console import state, twofactor
from glowderma.accounts.models import AuthSession, Customer, StaffUser
from glowderma.core import integration_modes, timeutils
from glowderma.core.admin import ReadOnlyAdmin, column_field
from glowderma.core.models import AuditEntry, OutboxMessage
from glowderma.integrations.models import Job, WebhookInbox
from glowderma.payments import selectors as payment_selectors

MARKER = "E2E_JSON:"
DB_PREFIX = "glowderma_e2e"
E2E_PASSWORD = "E2e-Console-Pass-2026!"  # noqa: S105 - the e2e database's staff only
# Columns that hold a credential or a credential's hash: never rendered by the console, whether the admin
# excludes them or not (the browser suite asserts their values appear on no page).
SECRET_FIELDS = {
    "password",
    "refresh_hash",
    "previous_refresh_hash",
    "fcm_token",
    "key_hash",
    "code_hmac",
    "verification_token_hash",
    "provider_refresh_token",
    "request_hash",
    "response_body",
}
SECRET_FIELDS_BY_MODEL = {"otp_totp.totpdevice": {"key"}, "otp_static.statictoken": {"token"}}


def refusal() -> str:
    """Why the command may not run here ("" when it may)."""
    module = str(getattr(settings, "SETTINGS_MODULE", "") or "")
    if module.rsplit(".", 1)[-1] == "production":
        return "e2e_console is refused under the production settings"
    if getattr(settings, "E2E_CONSOLE_ENABLED", False) is not True:
        return "e2e_console needs the e2e settings (E2E_CONSOLE_ENABLED, config.settings.e2e)"
    if not integration_modes.dev_features_allowed():
        return "e2e_console is refused without DEV_FEATURES_ALLOWED"
    return ""


# --- helpers ------------------------------------------------------------------------------------------------


def _model(label: str) -> type[models.Model]:
    for model in admin.site._registry:
        if model._meta.label_lower == label.lower():
            return model
    raise CommandError(f"not a registered model: {label}")


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _audit_row(entry: AuditEntry) -> dict[str, Any]:
    return {
        "id": entry.pk,
        "action": entry.action,
        "actor_type": entry.actor_type,
        "actor_label": entry.actor_label,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "before": entry.before,
        "after": entry.after,
        "reason": entry.reason,
    }


def _secret_fields(model: type[models.Model], model_admin: Any) -> list[str]:
    names = {field.name for field in model._meta.concrete_fields}
    declared = {name for name in (model_admin.exclude or ()) if isinstance(name, str)}
    wanted = (SECRET_FIELDS | declared | SECRET_FIELDS_BY_MODEL.get(model._meta.label_lower, set())) & names
    json_fields = {f.name for f in model._meta.concrete_fields if isinstance(f, models.JSONField)}
    return sorted(wanted - (json_fields - SECRET_FIELDS))


def _secret_values(model: type[models.Model], fields: list[str], rows: list[Any]) -> list[str]:
    """The secret columns' values of ``rows`` as they could appear in HTML (long enough not to match by
    chance; a TOTP seed also in base32, the form an authenticator takes)."""
    values: set[str] = set()
    for row in rows:
        for name in fields:
            value = getattr(row, name, None)
            if value is None:
                continue
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            if len(text) >= 8:
                values.add(text)
            if model is TOTPDevice and name == "key" and value:
                values.add(base64.b32encode(bytes.fromhex(str(value))).decode().rstrip("="))
    return sorted(values)


def _filters(model_admin: Any) -> list[dict[str, Any]]:
    entries = []
    for index, entry in enumerate(model_admin.list_filter or ()):
        field = column_field(entry)
        kind = entry[1].__name__ if isinstance(entry, tuple) else "default"
        entries.append({"index": index, "field": field, "kind": kind})
    return entries


# --- actions ------------------------------------------------------------------------------------------------


def registry(data: dict[str, Any]) -> Any:
    """Every model of ``admin.site`` and what its admin declares (no database access)."""
    rows = []
    for model, model_admin in sorted(admin.site._registry.items(), key=lambda kv: kv[0]._meta.label_lower):
        opts = model._meta
        prefix = f"admin:{opts.app_label}_{opts.model_name}"
        rows.append(
            {
                "label": opts.label_lower,
                "app_label": opts.app_label,
                "model_name": opts.model_name,
                "verbose_name": str(opts.verbose_name),
                "verbose_name_plural": str(opts.verbose_name_plural),
                "changelist": reverse(f"{prefix}_changelist"),
                "add": reverse(f"{prefix}_add"),
                "read_only": isinstance(model_admin, ReadOnlyAdmin),
                "search_fields": list(model_admin.search_fields or ()),
                "filters": _filters(model_admin),
                "list_per_page": model_admin.list_per_page,
                "date_hierarchy": model_admin.date_hierarchy,
            }
        )
    return rows


def _search_q(model_admin: Any, word: str) -> models.Q:
    """What the admin's search does for one word: OR over every search field with its lookup."""
    condition = models.Q()
    for raw in model_admin.search_fields or ():
        path = raw.lstrip("^=@")
        lookup = "iexact" if raw.startswith("=") else "istartswith" if raw.startswith("^") else "icontains"
        condition |= models.Q(**{f"{path}__{lookup}": word})
    return condition


def _search_term(model: type[models.Model], model_admin: Any, queryset: models.QuerySet[Any]) -> Any:
    """A word taken from one search field that finds at most a page of rows (counted over every search
    field, as the admin searches), and a row it finds."""
    for raw in model_admin.search_fields or ():
        path = raw.lstrip("^=@")
        for row in queryset.exclude(**{f"{path}__isnull": True}).values("pk", path)[:20]:
            value = str(row[path]).strip()
            if not value:
                continue
            word = value if raw.startswith("=") else max(value.split(), key=len)
            try:
                hits = queryset.filter(_search_q(model_admin, word)).distinct().count()
            except Exception:  # noqa: S112 - a word some field cannot be compared with: try another
                continue
            if 0 < hits <= model_admin.list_per_page:
                return {"field": raw, "term": word, "pk": _plain(row["pk"]), "hits": hits}
    return None


def _change_url(model: type[models.Model], pk: Any) -> str:
    return reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_change", args=[pk])


def facts(data: dict[str, Any]) -> Any:
    """Per model: row count, the first row of the changelist, a search term that finds a row, the secret
    columns, a row that has them filled, and their values (that row's and the first page's)."""
    wanted = {label.lower() for label in data.get("models", [])}
    result = {}
    for model, model_admin in admin.site._registry.items():
        label = model._meta.label_lower
        if wanted and label not in wanted:
            continue
        ordering = list(model_admin.ordering or model._meta.ordering or ["-pk"])
        queryset = model._default_manager.all()
        first_page = list(queryset.order_by(*ordering)[: model_admin.list_per_page])
        first = first_page[0] if first_page else None
        fields = _secret_fields(model, model_admin)
        filled = models.Q()
        for name in fields:
            condition = ~models.Q(**{f"{name}__isnull": True})
            if isinstance(model._meta.get_field(name), (models.CharField, models.TextField)):
                condition &= ~models.Q(**{name: ""})
            filled |= condition
        secret_row = queryset.filter(filled).order_by(*ordering).first() if fields else None
        rows = first_page + ([secret_row] if secret_row is not None else [])
        result[label] = {
            "count": queryset.count(),
            "first_pk": _plain(first.pk) if first is not None else None,
            "change_url": _change_url(model, first.pk) if first is not None else None,
            "search": _search_term(model, model_admin, queryset),
            "secret_fields": fields,
            "secret_change_url": _change_url(model, secret_row.pk) if secret_row is not None else None,
            "secret_values": _secret_values(model, fields, rows),
        }
    return result


def _new_device(user: StaffUser) -> TOTPDevice:
    key = secrets.token_hex(20)
    return TOTPDevice.objects.create(
        user=user, name="Ops konsol", key=key, confirmed=True, step=30, tolerance=1, digits=6
    )


def staff(data: dict[str, Any]) -> Any:
    """A new staff member (``password`` E2E_PASSWORD unless given); ``totp`` adds a confirmed device on a
    random key the suite computes codes from."""
    email = str(data.get("email") or f"e2e-{secrets.token_hex(5)}@e2e.uz").lower()
    user = StaffUser.objects.create(
        email=email,
        full_name=str(data.get("full_name", "E2E Xodim")),
        is_active=bool(data.get("active", True)),
        is_staff=bool(data.get("staff", True)),
        is_manager=bool(data.get("manager", False)),
        is_superuser=bool(data.get("superuser", False)),
    )
    user.set_password(str(data.get("password") or E2E_PASSWORD))
    user.save(update_fields=["password"])
    out: dict[str, Any] = {
        "id": user.pk,
        "email": email,
        "password": str(data.get("password") or E2E_PASSWORD),
    }
    if data.get("totp", True):
        device = _new_device(user)
        out["key_hex"] = device.key
        out["key_base32"] = base64.b32encode(device.bin_key).decode().rstrip("=")
        if data.get("recovery"):
            out["recovery_codes"] = twofactor.issue_recovery_codes(user)
    return out


def session(data: dict[str, Any]) -> Any:
    """A console session of ``email`` as the sign-in leaves it behind (verified by the user's TOTP device),
    or with ``verified: false`` a password-only one (logged in, no second factor)."""
    user = StaffUser.objects.get(email=str(data["email"]).lower())
    store = SessionStore()
    now = timeutils.now().timestamp()
    store[SESSION_KEY] = str(user.pk)
    store[BACKEND_SESSION_KEY] = state.BACKEND
    store[HASH_SESSION_KEY] = user.get_session_auth_hash()
    if data.get("verified", True):
        device = TOTPDevice.objects.filter(user=user, confirmed=True).first() or _new_device(user)
        store[DEVICE_ID_SESSION_KEY] = device.persistent_id
    store[state.STARTED] = now
    store[state.SEEN] = now
    store.create()
    return {"session_key": store.session_key, "cookie": settings.SESSION_COOKIE_NAME}


def _store(key: str) -> SessionStore:
    store = SessionStore(session_key=key)
    if not store.exists(key):
        raise CommandError("no such session")
    return store


def age_session(data: dict[str, Any]) -> Any:
    """Move a console session's login and last-request stamps into the past."""
    store = _store(str(data["session_key"]))
    store.load()
    for name, setting in ((state.SEEN, "idle_seconds"), (state.STARTED, "absolute_seconds")):
        if setting in data:
            store[name] = float(store[name]) - float(data[setting])
    store.save()
    return {"started": store.get(state.STARTED), "seen": store.get(state.SEEN)}


def age_pending(data: dict[str, Any]) -> Any:
    """Move a pending sign-in (password passed, code not yet) into the past."""
    store = _store(str(data["session_key"]))
    pending = dict(store[state.PENDING])
    pending["at"] = float(pending["at"]) - float(data.get("seconds", state.PENDING_SECONDS + 1))
    store[state.PENDING] = pending
    store.save()
    return {"at": pending["at"]}


def session_state(data: dict[str, Any]) -> Any:
    key = str(data["session_key"])
    row = Session.objects.filter(session_key=key, expire_date__gt=timeutils.now()).first()
    if row is None:
        return {"exists": False}
    decoded = row.get_decoded()
    return {
        "exists": True,
        "user_id": decoded.get(SESSION_KEY),
        "verified": DEVICE_ID_SESSION_KEY in decoded,
        "pending": state.PENDING in decoded,
    }


def user_sessions(data: dict[str, Any]) -> Any:
    """How many unexpired sessions belong to (or are signing in as) ``email``."""
    user = StaffUser.objects.get(email=str(data["email"]).lower())
    count = 0
    for row in Session.objects.filter(expire_date__gt=timeutils.now()).iterator():
        decoded = row.get_decoded()
        signin = decoded.get(state.PENDING)
        if str(decoded.get(SESSION_KEY)) == str(user.pk) or (
            isinstance(signin, dict) and signin.get("uid") == user.pk
        ):
            count += 1
    return {"count": count}


def audit_rows(data: dict[str, Any]) -> Any:
    rows = AuditEntry.objects.filter(pk__gt=int(data.get("after_id", 0))).order_by("pk")
    if "action" in data:
        rows = rows.filter(action__startswith=str(data["action"]))
    if "entity_id" in data:
        rows = rows.filter(entity_id=str(data["entity_id"]))
    return [_audit_row(entry) for entry in rows[:200]]


def audit_max(data: dict[str, Any]) -> Any:
    last = AuditEntry.objects.order_by("-pk").values_list("pk", flat=True).first()
    return {"id": last or 0}


def lockout(data: dict[str, Any]) -> Any:
    key = f"auth:lock:{data['scope']}:{email_hash(str(data['email']))}"
    expiry = cache.get(key)
    return {"locked": expiry is not None, "until": expiry}


def clear_cache(data: dict[str, Any]) -> Any:
    """Drop the console's cached badges and dashboards (and, with ``all``, every key of the e2e cache)."""
    client = cache._cache.get_client(write=True)  # type: ignore[attr-defined]
    prefix = cache.make_key("")
    patterns = [f"{prefix}*"] if data.get("all") else [f"{prefix}ops:nav:*", f"{prefix}ops:dash:*"]
    removed = 0
    for pattern in patterns:
        for key in client.scan_iter(match=pattern, count=500):
            removed += int(client.delete(key))
    return {"removed": removed}


def cache_delete(data: dict[str, Any]) -> Any:
    """Forget cache keys (an action's cooldown, e.g. ``ops:bts_poll``)."""
    keys = [str(key) for key in data["keys"]]
    cache.delete_many(keys)
    return {"deleted": keys}


def panel_session(data: dict[str, Any]) -> Any:
    """An open session of the staff member in the Next.js panel (the staff API's refresh session)."""
    from glowderma.accounts import services

    user = StaffUser.objects.get(email=str(data["email"]).lower())
    row, _refresh = services.start_staff_session(user, device_label="e2e panel")
    return {"id": str(row.pk)}


def dead_outbox(data: dict[str, Any]) -> Any:
    topic = str(data.get("topic", "push.order_status"))
    message = OutboxMessage.objects.create(
        topic=topic,
        payload=data.get("payload", {"order_id": 999999}),
        dedupe_key=f"e2e:{secrets.token_hex(8)}",
        status="dead",
        attempts=10,
        last_error="e2e: dead on purpose",
    )
    return {"id": message.pk, "topic": topic}


def outbox_row(data: dict[str, Any]) -> Any:
    message = OutboxMessage.objects.filter(pk=int(data["id"])).first()
    if message is None:
        return None
    return {"id": message.pk, "status": message.status, "attempts": message.attempts, "topic": message.topic}


def outbox_done(data: dict[str, Any]) -> Any:
    """A delivered outbox message (nothing to requeue)."""
    message = OutboxMessage.objects.create(
        topic="push.order_status",
        payload={"order_id": 999998},
        dedupe_key=f"e2e:{secrets.token_hex(8)}",
        status="done",
        attempts=1,
    )
    return {"id": message.pk}


def customer_sessions(data: dict[str, Any]) -> Any:
    """A new customer with ``count`` open sessions."""
    customer = Customer.objects.create(
        phone=f"+99899{secrets.randbelow(10**7):07d}", name=f"E2E Mijoz {secrets.token_hex(3)}"
    )
    ids = []
    for index in range(int(data.get("count", 2))):
        row = AuthSession.objects.create(
            audience="customer",
            customer=customer,
            device_label=f"e2e-{index}",
            refresh_hash=secrets.token_hex(32),
            absolute_expires_at=timeutils.now() + timedelta(days=30),
        )
        ids.append(str(row.pk))
    return {"customer_id": customer.pk, "session_ids": ids}


def auth_sessions(data: dict[str, Any]) -> Any:
    rows = AuthSession.objects.filter(pk__in=list(data["ids"]))
    return [
        {"id": str(row.pk), "revoked_at": _plain(row.revoked_at), "revoke_reason": row.revoke_reason}
        for row in rows
    ]


def two_factor(data: dict[str, Any]) -> Any:
    user = StaffUser.objects.get(email=str(data["email"]).lower())
    return {
        "confirmed": TOTPDevice.objects.filter(user=user, confirmed=True).count(),
        "unconfirmed": TOTPDevice.objects.filter(user=user, confirmed=False).count(),
        "recovery_codes": StaticToken.objects.filter(device__user=user).count(),
    }


def jobs(data: dict[str, Any]) -> Any:
    rows = Job.objects.filter(pk__gt=int(data.get("after_id", 0))).order_by("pk")
    return [{"id": row.pk, "kind": row.kind, "state": row.state} for row in rows]


def job_max(data: dict[str, Any]) -> Any:
    return {"id": Job.objects.order_by("-pk").values_list("pk", flat=True).first() or 0}


def badge_counts(data: dict[str, Any]) -> Any:
    """The sidebar badges' numbers: dead outbox messages and failed callbacks counted here, the refunds by the
    payments selector (its "superseded failure" rule is the definition of the number)."""
    since = timeutils.now() - timedelta(hours=24)
    return {
        "dead_outbox": OutboxMessage.objects.filter(status="dead").count(),
        "failed_webhooks": WebhookInbox.objects.filter(
            received_at__gte=since, result__outcome__in=["rejected", "error"]
        ).count(),
        "refunds_needing_action": payment_selectors.refunds_needing_action(),
    }


def row(data: dict[str, Any]) -> Any:
    model = _model(str(data["model"]))
    filters = dict(data.get("filter", {}))
    obj = model._default_manager.filter(**filters).order_by("-pk").first()
    if obj is None:
        return None
    return {field.attname: _plain(getattr(obj, field.attname)) for field in model._meta.concrete_fields}


def count(data: dict[str, Any]) -> Any:
    model = _model(str(data["model"]))
    return {"count": model._default_manager.filter(**dict(data.get("filter", {}))).count()}


def professional(data: dict[str, Any]) -> Any:
    """An active professional with upcoming appointments (``upcoming``), or a new one without any rows that
    point to it (``fresh``)."""
    from glowderma.professionals import bookings, services
    from glowderma.professionals.models import Location, Professional

    if data.get("fresh"):
        row = Professional(
            location=Location.objects.order_by("pk").first(),
            name=f"E2E Mutaxassis {secrets.token_hex(3)}",
            role="cosmetologist",
        )
        services.save_row(row, actor=None)
        return {"id": row.pk, "name": row.name}
    active = list(Professional.objects.filter(is_active=True).values_list("pk", flat=True))
    counts = bookings.upcoming_counts(active, timeutils.now())
    for pk, upcoming in sorted(counts.items()):
        if upcoming:
            found = Professional.objects.get(pk=pk)
            return {"id": pk, "name": found.name, "upcoming": upcoming}
    raise CommandError("no professional with upcoming appointments in the seeded data")


def reset_db(data: dict[str, Any]) -> Any:
    """Drop and create the e2e databases (only ``glowderma_e2e*`` names); ``template`` copies one."""
    names = [str(name) for name in data["names"]]
    for name in names:
        if not name.startswith(DB_PREFIX) or not name.replace("_", "").isalnum():
            raise CommandError(f"refusing to touch database {name!r}")
    template = data.get("template")
    if template is not None and not str(template).startswith(DB_PREFIX):
        raise CommandError(f"refusing template {template!r}")
    with connection._nodb_cursor() as cursor:
        for name in names:
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            suffix = f' TEMPLATE "{template}"' if template else ""
            cursor.execute(f'CREATE DATABASE "{name}"{suffix}')
    return {"databases": names}


def seed_coverage(data: dict[str, Any]) -> Any:
    """At least one row in every registered model the demo seeds leave empty (so every change page and
    every secret column is exercised), plus a staff member with recovery codes."""
    from glowderma.reporting import e2e_seed

    return e2e_seed.fill_empty_models()


ACTIONS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "registry": registry,
    "facts": facts,
    "staff": staff,
    "session": session,
    "age-session": age_session,
    "age-pending": age_pending,
    "session-state": session_state,
    "user-sessions": user_sessions,
    "audit": audit_rows,
    "audit-max": audit_max,
    "lockout": lockout,
    "clear-cache": clear_cache,
    "cache-delete": cache_delete,
    "panel-session": panel_session,
    "dead-outbox": dead_outbox,
    "outbox": outbox_row,
    "outbox-done": outbox_done,
    "customer-sessions": customer_sessions,
    "auth-sessions": auth_sessions,
    "two-factor": two_factor,
    "jobs": jobs,
    "job-max": job_max,
    "badge-counts": badge_counts,
    "row": row,
    "count": count,
    "professional": professional,
    "reset-db": reset_db,
    "seed-coverage": seed_coverage,
}


class Command(BaseCommand):
    help = "Data setup and database checks for the ops console's Playwright suite (e2e settings only)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("action", choices=sorted(ACTIONS))
        parser.add_argument("--data", default="{}", help="JSON object with the action's arguments")

    def handle(self, *args: Any, **options: Any) -> None:
        reason = refusal()
        if reason:
            raise CommandError(reason)
        try:
            data = json.loads(options["data"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"--data is not JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise CommandError("--data must be a JSON object")
        result = ACTIONS[options["action"]](data)
        self.stdout.write(MARKER + json.dumps(result, ensure_ascii=False, default=str))
