"""Who may read which audit entries in the admin panel (admin contract §2.13, design "audit tiers").

One rule table decides the *scope* of an entry from its ``action`` (the part before the first dot is the
*prefix*), its ``actor_type`` and its ``entity_type``; the first rule that matches wins:

1. :data:`SYSTEM_PREFIXES` and :data:`SYSTEM_ACTIONS` → ``system`` (superusers only): the ops console,
   console access, integrations, the outbox, reports, POS devices and the hand-run Billz / ledger fixes.
2. :data:`STAFF_ADMIN_PREFIXES` (``staff.*``) → ``staff_admin`` (managers and superusers).
3. ``auth.*`` written by a staff actor or about a staff account or a staff session, and
   :data:`SECURITY_PREFIXES` (``export.*``) → ``staff_security``: ordinary staff see only their **own**
   entries (:func:`own_q`), managers and superusers see all of them.
4. :data:`BUSINESS_PREFIXES`, and ``auth.*`` written by a customer → ``business`` (every staff member).
5. Anything else → ``system`` (fail closed): a new action nobody classified stays hidden until it is added
   here (``core/tests/test_audit_scopes.py`` scans every ``audit.record(action=…)`` for that).

The admin API (``reporting.selectors.audit_log`` and its export) applies :func:`visible_q`; a filter on a
hidden action or object therefore answers an empty list, never 403. Ordinary staff never see network
details: the ``ip`` column is blanked and the ``ip`` / ``last_ip`` / ``user_agent`` / ``location`` keys of
the data document are masked (:func:`mask_network`). The ops console (``/django-admin/``) reads the table
without this filter. ``core`` imports nothing above it, so the viewer is duck-typed: ``pk``,
``is_manager``, ``is_superuser``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from django.db.models import Q

from glowderma.core import enums

SYSTEM: Final = "system"
STAFF_ADMIN: Final = "staff_admin"
STAFF_SECURITY: Final = "staff_security"
BUSINESS: Final = "business"

SYSTEM_PREFIXES: Final = frozenset(
    {"console", "ops", "ops_access", "integrations", "outbox", "report", "pos"}
)
# Hand-run fixes and the nightly reconciliation, filed under a business prefix but meant for operators.
SYSTEM_ACTIONS: Final = frozenset(
    {
        "billz.sale_recorded_by_hand",
        "billz.payment_attempt_forgotten",
        "loyalty.reconciliation_completed",
        "loyalty.reconciliation_mismatch",
    }
)
STAFF_ADMIN_PREFIXES: Final = frozenset({"staff"})
AUTH_PREFIX: Final = "auth"
SECURITY_PREFIXES: Final = frozenset({"export"})
# ``auth.*`` entries about these objects are staff sign-in security, whoever wrote them.
SECURITY_ENTITY_TYPES: Final = frozenset({"staff", "auth_session"})
BUSINESS_PREFIXES: Final = frozenset(
    {
        "appointment",
        "banner",
        "billz",
        "blocked_period",
        "cashback",
        "category",
        "customer",
        "delivery",
        "location",
        "loyalty",
        "notification",
        "notification_template",
        "order",
        "payment",
        "product",
        "professional",
        # the Django-admin edits of a specialist's service link (``professional_service.*``, contract §3.2)
        "professional_service",
        "refund",
        "schedule",
        "segment",
        "service",
        "settings",
        "test_device",
        "working_hours",
        "working_hours_override",
    }
)
# Keys of an entry's data document that carry network details (masked for ordinary staff, at any depth).
NETWORK_KEYS: Final = frozenset({"ip", "last_ip", "user_agent", "location"})


def prefix(action: str) -> str:
    """``"auth.login_failed"`` → ``"auth"``; an action without a dot is its own prefix."""
    return action.partition(".")[0]


def scope_of(action: str, actor_type: str = "", entity_type: str = "") -> str:
    """The scope of one entry by the rule table (first match wins, unknown → ``system``)."""
    head = prefix(action)
    if head in SYSTEM_PREFIXES or action in SYSTEM_ACTIONS:
        return SYSTEM
    if head in STAFF_ADMIN_PREFIXES:
        return STAFF_ADMIN
    if head == AUTH_PREFIX and (actor_type == "staff" or entity_type in SECURITY_ENTITY_TYPES):
        return STAFF_SECURITY
    if head in SECURITY_PREFIXES:
        return STAFF_SECURITY
    if head in BUSINESS_PREFIXES or (head == AUTH_PREFIX and actor_type == "customer"):
        return BUSINESS
    return SYSTEM


def is_classified(action: str) -> bool:
    """Whether rules 1–4 name ``action`` at all (``auth.*`` counts: its scope depends on the actor). The
    scan test fails on an audited action for which this is false (it would fall into rule 5)."""
    head = prefix(action)
    return (
        head in SYSTEM_PREFIXES
        or action in SYSTEM_ACTIONS
        or head in STAFF_ADMIN_PREFIXES
        or head == AUTH_PREFIX
        or head in SECURITY_PREFIXES
        or head in BUSINESS_PREFIXES
    )


def known_prefixes() -> frozenset[str]:
    return SYSTEM_PREFIXES | STAFF_ADMIN_PREFIXES | SECURITY_PREFIXES | BUSINESS_PREFIXES | {AUTH_PREFIX}


# --- viewers -------------------------------------------------------------------------------------------


def viewer_tier(viewer: Any) -> str:
    """``superuser`` · ``manager`` (``is_manager``) · ``staff`` (everyone else) — :class:`AuditViewerTier`."""
    if getattr(viewer, "is_superuser", False):
        return enums.AuditViewerTier.SUPERUSER
    if getattr(viewer, "is_manager", False):
        return enums.AuditViewerTier.MANAGER
    return enums.AuditViewerTier.STAFF


def ip_visible(viewer: Any) -> bool:
    """Managers and superusers see IP addresses and user agents; ordinary staff do not."""
    return viewer_tier(viewer) != enums.AuditViewerTier.STAFF


def _prefix_q(prefixes: frozenset[str] | set[str]) -> Q:
    # one anchored regular expression instead of one ``LIKE`` per prefix; prefixes are [a-z_] literals
    alternatives = "|".join(re.escape(name) for name in sorted(prefixes))
    return Q(action__regex=rf"^({alternatives})\.")


def _system_q() -> Q:
    return _prefix_q(SYSTEM_PREFIXES) | Q(action__in=sorted(SYSTEM_ACTIONS))


def _staff_admin_q() -> Q:
    return _prefix_q(STAFF_ADMIN_PREFIXES)


def _security_q() -> Q:
    auth = _prefix_q({AUTH_PREFIX}) & (
        Q(actor_type="staff") | Q(entity_type__in=sorted(SECURITY_ENTITY_TYPES))
    )
    return auth | _prefix_q(SECURITY_PREFIXES)


def _business_q() -> Q:
    customer_auth = (
        _prefix_q({AUTH_PREFIX})
        & Q(actor_type="customer")
        & ~Q(entity_type__in=sorted(SECURITY_ENTITY_TYPES))
    )
    return (_prefix_q(BUSINESS_PREFIXES) & ~Q(action__in=sorted(SYSTEM_ACTIONS))) | customer_auth


def own_q(viewer: Any) -> Q:
    """The viewer's own entries: written by them, or about their own staff account."""
    return Q(actor_type="staff", actor_id=viewer.pk) | Q(entity_type="staff", entity_id=str(viewer.pk))


def visible_q(viewer: Any) -> Q | None:
    """The entries ``viewer`` may read (``None`` = every entry, superusers)."""
    tier = viewer_tier(viewer)
    if tier == enums.AuditViewerTier.SUPERUSER:
        return None
    if tier == enums.AuditViewerTier.MANAGER:
        return _staff_admin_q() | _security_q() | _business_q()
    return (_security_q() & own_q(viewer)) | _business_q()


# --- masking -------------------------------------------------------------------------------------------


def mask_network(document: Any) -> Any:
    """A copy of ``document`` with the value of every :data:`NETWORK_KEYS` key set to ``None`` (any depth)."""
    if isinstance(document, Mapping):
        return {key: None if key in NETWORK_KEYS else mask_network(value) for key, value in document.items()}
    if isinstance(document, list):
        return [mask_network(item) for item in document]
    return document


# --- facets (``GET audit-log/facets/``) -----------------------------------------------------------------


def facets(viewer: Any) -> dict[str, Any]:
    """What the panel offers this viewer: ``tier``, ``ip_visible``, the prefixes it sees entries of
    (``visible_prefixes``), those limited to its own entries (``own_only_prefixes``, a subset) and the
    actions hidden although their prefix is visible (``hidden_actions``)."""
    tier = viewer_tier(viewer)
    business = BUSINESS_PREFIXES | {AUTH_PREFIX}
    security = SECURITY_PREFIXES | {AUTH_PREFIX}
    own_only: frozenset[str] = frozenset()
    hidden: frozenset[str] = SYSTEM_ACTIONS
    if tier == enums.AuditViewerTier.SUPERUSER:
        visible = known_prefixes()
        hidden = frozenset()
    elif tier == enums.AuditViewerTier.MANAGER:
        visible = business | security | STAFF_ADMIN_PREFIXES
    else:
        visible = business | security
        own_only = security
    return {
        "tier": tier,
        "ip_visible": ip_visible(viewer),
        "visible_prefixes": sorted(visible),
        "own_only_prefixes": sorted(own_only),
        "hidden_actions": sorted(hidden),
    }
