from typing import Any

from glowderma.core.models import AuditEntry
from glowderma.core.pii import mask_email, mask_phone


def actor_identity(actor: Any) -> tuple[str, str]:
    """``(actor_type, actor_label)`` of an audit actor.

    Staff users (they have ``get_username``) → ``staff`` and the full name or email; customers (an
    ``accounts.Customer``: a ``phone``) → ``customer`` and the masked phone, never the number itself (a
    phoneless customer, hybrid identity: the masked e-mail, else ``***``); POS
    devices (a ``loyalty.PosDevice``: a ``key_prefix``) → ``integration`` and the device name; no actor →
    ``system``. Duck-typed because ``core`` imports no module above it.
    """
    if actor is None:
        return "system", ""
    if getattr(actor, "full_name", ""):
        return "staff", str(actor.full_name)
    if hasattr(actor, "get_username"):
        return "staff", str(actor.get_username())
    if hasattr(actor, "phone"):
        if actor.phone:
            return "customer", mask_phone(str(actor.phone))
        email = getattr(actor, "email", None)
        return "customer", mask_email(str(email)) if email else "***"
    if hasattr(actor, "key_prefix"):
        return "integration", str(getattr(actor, "name", ""))
    raise TypeError(f"Unsupported audit actor {type(actor).__name__}")


def record(
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    actor: Any = None,
    actor_type: str | None = None,
    request: Any = None,
    before: Any = None,
    after: Any = None,
    reason: str = "",
) -> AuditEntry:
    kind, label = actor_identity(actor)
    meta = request.META if request is not None else {}
    return AuditEntry.objects.create(
        actor_type=actor_type or kind,
        actor_id=actor.pk if actor is not None else None,
        actor_label=label,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
        reason=reason,
        ip=getattr(request, "client_ip", None) or meta.get("REMOTE_ADDR"),
        user_agent=meta.get("HTTP_USER_AGENT", ""),
        request_id=getattr(request, "request_id", ""),
    )
