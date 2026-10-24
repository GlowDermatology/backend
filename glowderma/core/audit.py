from typing import Any

from glowderma.core.models import AuditEntry


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
    label = (getattr(actor, "full_name", "") or actor.get_username()) if actor is not None else ""
    meta = request.META if request is not None else {}
    return AuditEntry.objects.create(
        actor_type=actor_type or ("staff" if actor is not None else "system"),
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
