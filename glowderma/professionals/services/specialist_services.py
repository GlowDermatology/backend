"""A specialist's own services (FND-7): create, update and delete — or deactivate, once booked."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models.deletion import ProtectedError
from rest_framework.exceptions import NotFound

from glowderma.core import audit, enums
from glowderma.core.api.exceptions import Conflict
from glowderma.professionals.models import (
    SERVICE_NAME_CONSTRAINT,
    Professional,
    Service,
)
from glowderma.professionals.services import common


def _service_state(service: Service) -> dict[str, Any]:
    return {
        "professional_id": service.professional_id,
        **common.i18n_state(service, "name"),
        **common.i18n_state(service, "short_name"),
        **common.i18n_state(service, "description"),
        "category": service.category,
        "price": service.price,
        "duration_min": service.duration_min,
        "position": service.position,
        "is_active": service.is_active,
    }


SERVICE_SCALARS = ("category", "price", "duration_min", "position", "is_active")


def _apply_service(service: Service, data: dict[str, Any]) -> None:
    for key in SERVICE_SCALARS:
        if key in data:
            setattr(service, key, data[key])
    for prefix in ("name", "short_name", "description"):
        common.set_i18n(service, prefix, data.get(prefix))


def _save_service(service: Service) -> None:
    """Save with the per-specialist name race (``service_professional_name_unique``) as 400 ``unique``."""
    common.save_unique(
        service.save, SERVICE_NAME_CONSTRAINT, "name", common.API_MESSAGES["service_name_taken"]
    )


def _owned_service(professional_id: int, service_id: int) -> Service:
    """Lock the specialist, then its service (the booking order: specialist → service); another
    specialist's service is 404."""
    professional = common.locked_professional(professional_id)
    service = (
        Service.objects.select_for_update().filter(pk=service_id, professional_id=professional.pk).first()
    )
    if service is None:
        raise NotFound
    return service


def default_service_category(professional: Professional) -> str:
    """A new service's discipline when none is given: the specialist's own (dermatology for a draft)."""
    if professional.role == enums.SpecialistRole.COSMETOLOGIST:
        return str(enums.ServiceCategory.COSMETOLOGY)
    return str(enums.ServiceCategory.DERMATOLOGY)


def create_specialist_service(
    professional_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> Service:
    """``POST specialists/{id}/services/`` (staff) and ``specialist/services/`` (the owner): a new service of
    that specialist, appended after its others unless a ``position`` is given; the category defaults from
    the specialist's role. A repeated name of the same specialist is 400 ``unique``; more than
    :data:`MAX_SERVICES_PER_SPECIALIST` is 409. Audit ``service.created``."""
    with transaction.atomic():
        professional = common.locked_professional(professional_id)
        owned = Service.objects.filter(professional=professional)
        if owned.count() >= common.MAX_SERVICES_PER_SPECIALIST:
            raise Conflict(common.API_MESSAGES["service_limit"] % {"n": common.MAX_SERVICES_PER_SPECIALIST})
        last = owned.order_by("-position").values_list("position", flat=True).first()
        service = Service(
            professional=professional,
            position=0 if last is None else int(last) + 1,
            category=default_service_category(professional),
        )
        _apply_service(service, data)
        _save_service(service)
        audit.record(
            action="service.created",
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            after=_service_state(service),
        )
    return service


def update_specialist_service(
    professional_id: int, service_id: int, data: dict[str, Any], *, actor: Any, request: Any = None
) -> Service:
    """``PATCH …/services/{id}/``: ``is_active: false`` takes a service out of the customer catalogue and the
    booking dialogs at once; booked appointments keep their price and duration snapshots. Audit
    ``service.deactivated`` / ``service.activated`` when ``is_active`` flips, else ``service.updated``;
    nothing changed → no write, no audit."""
    with transaction.atomic():
        service = _owned_service(professional_id, service_id)
        before = _service_state(service)
        _apply_service(service, data)
        after = _service_state(service)
        if after == before:
            return service
        action = "service.updated"
        if before["is_active"] != after["is_active"]:
            action = "service.activated" if after["is_active"] else "service.deactivated"
        _save_service(service)
        diff_before, diff_after = common.diff(before, after)
        audit.record(
            action=action,
            entity_type="service",
            entity_id=str(service.pk),
            actor=actor,
            request=request,
            before={"professional_id": service.professional_id, **diff_before},
            after={"professional_id": service.professional_id, **diff_after},
        )
    return service


def delete_specialist_service(
    professional_id: int, service_id: int, *, actor: Any, request: Any = None
) -> None:
    """``DELETE …/services/{id}/``: only a service that was never booked (``Appointment.service`` is
    ``PROTECT``); one with appointments, past or cancelled included, is 409 (deactivate it instead).
    Audit ``service.deleted``."""
    with transaction.atomic():
        service = _owned_service(professional_id, service_id)
        state = _service_state(service)
        pk = service.pk
        try:
            with transaction.atomic():
                service.delete()
        except ProtectedError as exc:
            raise Conflict(common.API_MESSAGES["service_booked"]) from exc
        audit.record(
            action="service.deleted",
            entity_type="service",
            entity_id=str(pk),
            actor=actor,
            request=request,
            before=state,
        )


__all__ = [
    "SERVICE_SCALARS",
    "create_specialist_service",
    "default_service_category",
    "delete_specialist_service",
    "update_specialist_service",
]
