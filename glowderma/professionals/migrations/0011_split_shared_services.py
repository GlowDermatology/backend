"""Per-specialist services, step 2 of 3 (data): split every shared service into one row per specialist.

A service's owners are the specialists linked to it (``ProfessionalService``, in link order, with the
link's price and duration; active only when both the service and the link were) plus any specialist that
has appointments on it without a link (inactive, with the price and duration of its latest appointment).
The first owner keeps the original row; every other owner gets a copy with the same texts, category and
position, and that owner's appointments are repointed to the copy. A service nobody owns is deleted.
Finally a name repeated within one specialist is made unique (``"<name> #<id>"``).

The rows cannot simply be recreated: ``Appointment.service`` is ``PROTECT`` and ``AppointmentEvent`` is
append-only, so booked services must survive with their ids (the copies only take over appointments).
The reverse creates one link per service from its owner, price and duration; copies stay separate rows.
"""

from typing import Any

from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps

COPIED_FIELDS = (
    "name_uz",
    "name_ru",
    "name_en",
    "short_name_uz",
    "short_name_ru",
    "short_name_en",
    "description_uz",
    "description_ru",
    "description_en",
    "category",
    "position",
)
NAME_MAX_LENGTH = 120


def split_shared_services(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    database = schema_editor.connection.alias
    service_model = apps.get_model("professionals", "Service")
    link_model: Any = apps.get_model("professionals", "ProfessionalService")
    appointment_model = apps.get_model("appointments", "Appointment")
    services = service_model.objects.using(database)
    appointments = appointment_model.objects.using(database)
    for service in list(services.order_by("id")):
        owners: list[dict[str, Any]] = [
            {
                "professional_id": link.professional_id,
                "price": link.price,
                "duration_min": link.duration_min,
                "is_active": service.is_active and link.is_active,
            }
            for link in link_model.objects.using(database).filter(service_id=service.pk).order_by("id")
        ]
        linked = {owner["professional_id"] for owner in owners}
        booked = (
            appointments.filter(service_id=service.pk)
            .exclude(professional_id__in=linked)
            .values_list("professional_id", flat=True)
            .distinct()
        )
        for professional_id in sorted(set(booked)):
            latest = (
                appointments.filter(service_id=service.pk, professional_id=professional_id)
                .order_by("-start", "-id")
                .first()
            )
            assert latest is not None  # noqa: S101 - the professional id came from these appointments
            owners.append(
                {
                    "professional_id": professional_id,
                    "price": latest.price,
                    "duration_min": latest.duration_min,
                    "is_active": False,
                }
            )
        if not owners:
            service.delete()  # no appointment uses it (its specialist would own it)
            continue
        first, *others = owners
        for name, value in first.items():
            setattr(service, name, value)
        service.save(update_fields=list(first))
        for owner in others:
            copy = services.create(**{field: getattr(service, field) for field in COPIED_FIELDS}, **owner)
            appointments.filter(service_id=service.pk, professional_id=owner["professional_id"]).update(
                service_id=copy.pk
            )
    seen: set[tuple[int, str]] = set()
    for service in services.order_by("professional_id", "id"):
        key = (service.professional_id, service.name_uz)
        if key in seen:
            suffix = f" #{service.pk}"
            service.name_uz = f"{service.name_uz[: NAME_MAX_LENGTH - len(suffix)]}{suffix}"
            service.save(update_fields=["name_uz"])
        seen.add((service.professional_id, service.name_uz))


def link_owned_services(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    database = schema_editor.connection.alias
    service_model = apps.get_model("professionals", "Service")
    link_model: Any = apps.get_model("professionals", "ProfessionalService")
    for service in service_model.objects.using(database).filter(professional__isnull=False).order_by("id"):
        link_model.objects.using(database).create(
            professional_id=service.professional_id,
            service_id=service.pk,
            price=service.price or 0,
            duration_min=service.duration_min,
            is_active=service.is_active,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("professionals", "0010_service_owner"),
        ("appointments", "0006_appointment_clinic"),
    ]

    operations = [migrations.RunPython(split_shared_services, link_owned_services)]
