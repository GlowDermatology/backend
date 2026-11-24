"""The admin API writes of a specialist (FND-1): the profile and its publication, the specialist's own profile
edit, the invite and the account role."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy
from rest_framework.exceptions import ValidationError as ApiValidationError

from glowderma.accounts import services as account_services
from glowderma.accounts.models import StaffUser
from glowderma.core import audit, enums, timeutils
from glowderma.core.api.exceptions import Conflict, field_error
from glowderma.core.uzsource import N_
from glowderma.professionals import bookings, selectors
from glowderma.professionals.models import (
    PROFESSIONAL_SLUG_CONSTRAINT,
    Location,
    Professional,
)
from glowderma.professionals.services import common


def _save_specialist(professional: Professional) -> None:
    """Save with both unique races (the staff link, the B-W1 slug) reported as field errors, not a 500."""

    def save() -> None:
        common.save_unique(
            professional.save, PROFESSIONAL_SLUG_CONSTRAINT, "slug", common.API_MESSAGES["slug_taken"]
        )

    common.save_unique(
        save, common.STAFF_USER_CONSTRAINT, "staff_user_id", common.API_MESSAGES["staff_taken"]
    )


def checked_clinics(professional: Professional, data: dict[str, Any], current_ids: set[int]) -> set[int]:
    ids = set(data.get("location_ids", current_ids))
    if professional.location_id not in ids:
        raise ApiValidationError(
            {"location_ids": [field_error("invalid", common.API_MESSAGES["clinic_primary_required"])]}
        )
    check_clinic_assignments(professional, ids, current_ids)
    return ids


def check_clinic_assignments(
    professional: Professional, ids: set[int], current_ids: set[int], *, form: bool = False
) -> None:
    # Match the booking lock order: the specialist is already locked, then clinics in PK order.
    clinics = dict(
        Location.objects.select_for_update().filter(pk__in=ids).order_by("pk").values_list("pk", "is_active")
    )
    if ids != set(clinics) or any(
        not is_active and (professional.is_active or pk not in current_ids)
        for pk, is_active in clinics.items()
    ):
        if form:
            raise ValidationError({"location": gettext_lazy(common.API_MESSAGES["clinic_not_found"])})
        raise ApiValidationError(
            {"location_ids": [field_error("does_not_exist", common.API_MESSAGES["clinic_not_found"])]}
        )
    removed = current_ids - ids
    if removed and (
        professional.working_hours.filter(location_id__in=removed).exists()
        or professional.hours_overrides.filter(
            location_id__in=removed, date__gte=timeutils.local_date(timeutils.now())
        ).exists()
    ):
        if form:
            raise ValidationError({"location": gettext_lazy(common.API_MESSAGES["clinic_scheduled"])})
        raise Conflict(common.API_MESSAGES["clinic_scheduled"])


def _specialist_state(professional: Professional, location_ids: set[int]) -> dict[str, Any]:
    return {
        "name": professional.name,
        "role": professional.role,
        **common.i18n_state(professional, "qualifications"),
        **common.i18n_state(professional, "bio"),
        "experience_years": professional.experience_years,
        "slot_step_min": professional.slot_step_min,
        "location_id": professional.location_id,
        "location_ids": sorted(location_ids),
        "staff_user_id": professional.staff_user_id,
        "position": professional.position,
        "is_active": professional.is_active,
        "slug": professional.slug,
    }


SPECIALIST_SCALARS = ("name", "role", "experience_years", "slot_step_min", "position", "is_active")


# What a specialist may edit on their own profile (``specialist/profile/``).
# Clinics, the site order and the owner account stay with staff.
OWN_PROFILE_KEYS = frozenset(
    {"name", "role", "qualifications", "bio", "experience_years", "slot_step_min", "slug", "is_active"}
)


PUBLISH_BLOCKER_MESSAGES = {
    "account": N_("hisob Mutaxassis rolida emas"),
    "name": N_("ommaviy ism"),
    "discipline": N_("yo‘nalish"),
    "bio": N_("bio (o‘zbekcha)"),
    "photo": N_("surat"),
    "clinic": N_("faol asosiy filial"),
    "service": N_("faol xizmat"),
    "availability": N_("ish vaqti"),
}


PUBLISH_BLOCKED = N_("Profilni e’lon qilib bo‘lmaydi. Yetishmayapti: %(missing)s.")


def _apply_specialist(professional: Professional, data: dict[str, Any]) -> None:
    for key in SPECIALIST_SCALARS:
        if key in data:
            setattr(professional, key, data[key])
    if "location_id" in data:
        professional.location_id = data["location_id"]
    if data.get("slug"):
        professional.slug = data["slug"]  # B-W1: staff edit the website URL segment
    common.set_i18n(professional, "qualifications", data.get("qualifications"))
    common.set_i18n(professional, "bio", data.get("bio"))


def _refuse_unpublishable(professional: Professional) -> None:
    missing = selectors.publish_blockers(professional)
    if missing:
        raise Conflict(
            PUBLISH_BLOCKED % {"missing": ", ".join(str(PUBLISH_BLOCKER_MESSAGES[key]) for key in missing)}
        )


def _create_draft_profile(user: StaffUser, *, actor: Any, request: Any) -> Professional:
    """The private draft profile owned by ``user`` for its lifetime: named after the account, no clinic,
    service or availability yet, not published (staff publish it once it is complete)."""
    professional = Professional(
        staff_user=user, name=user.full_name, position=common.next_position(Professional), is_active=False
    )
    _save_specialist(professional)
    audit.record(
        action="professional.created",
        entity_type="professional",
        entity_id=str(professional.pk),
        actor=actor,
        request=request,
        after=_specialist_state(professional, set()),
    )
    return professional


def update_specialist(
    professional_id: int, data: dict[str, Any], *, force: bool = False, actor: Any, request: Any = None
) -> Professional:
    """``PATCH specialists/{id}/`` (staff) and ``PATCH specialist/profile/`` (the owner, no clinics).

    ``is_active`` is publication: publishing needs the owner account in the Specialist role and a complete
    profile (:func:`selectors.publish_blockers`, else 409 naming what is missing). Unpublishing a
    profile with ``requested`` / ``confirmed`` appointments ahead is refused (409 naming the count) unless
    ``force``; the appointments are kept either way. Audit ``professional.deactivated`` /
    ``professional.activated`` when ``is_active`` flips (with the other changed fields), else
    ``professional.updated``; nothing changed → no write, no audit."""
    with transaction.atomic():
        professional = common.locked_professional(professional_id)
        current_ids = set(selectors.assigned_location_ids(professional))
        before = _specialist_state(professional, current_ids)
        _apply_specialist(professional, data)
        location_ids = current_ids
        if "location_id" in data or "location_ids" in data:
            location_ids = checked_clinics(professional, data, current_ids)
        after = _specialist_state(professional, location_ids)
        if after == before:
            return professional
        extra: dict[str, Any] = {}
        action = "professional.updated"
        if before["is_active"] != after["is_active"]:
            action = "professional.activated" if after["is_active"] else "professional.deactivated"
        if action == "professional.deactivated":
            upcoming = bookings.upcoming_counts([professional.pk], timeutils.now())[professional.pk]
            if upcoming and not force:
                raise Conflict(common.API_MESSAGES["has_upcoming"].format(n=upcoming))
            extra = {"upcoming_appointments": upcoming, "forced": bool(upcoming)}
        _save_specialist(professional)
        if before["location_id"] != professional.location_id or location_ids != current_ids:
            professional.additional_locations.set(location_ids - {professional.location_id})
        if action == "professional.activated":
            # Checked after the write (the clinics are part of it); the 409 rolls the whole change back.
            _refuse_unpublishable(professional)
        diff_before, diff_after = common.diff(before, after)
        audit.record(
            action=action,
            entity_type="professional",
            entity_id=str(professional.pk),
            actor=actor,
            request=request,
            before=diff_before,
            after={**diff_after, **extra},
        )
    return professional


def update_own_profile(
    professional_id: int, data: dict[str, Any], *, force: bool = False, actor: Any, request: Any = None
) -> Professional:
    """A specialist edits their own profile (:data:`OWN_PROFILE_KEYS`), publication included, under the
    same rules as staff (:func:`update_specialist`); any other key is a programming error."""
    unknown = set(data) - OWN_PROFILE_KEYS
    if unknown:
        raise ValueError(f"Not editable by the specialist: {sorted(unknown)}")
    return update_specialist(professional_id, data, force=force, actor=actor, request=request)


@transaction.atomic
def invite_specialist(
    data: dict[str, Any], *, actor: StaffUser, request: Any = None
) -> tuple[StaffUser, str | None, Professional]:
    """Staff → Add employee → Specialist: create the Specialist account and its draft profile together.

    An email already in use is the usual ``unique`` field error, so a duplicate invite never creates a second
    account or profile; resending the invitation goes through the account's own password routes."""
    user, password = account_services.create_staff(
        **data, role=enums.StaffRole.SPECIALIST, actor=actor, request=request
    )
    professional = _create_draft_profile(user, actor=actor, request=request)
    return user, password, professional


@transaction.atomic
def set_specialist_account_role(
    staff_user_id: int, role: str, *, actor: StaffUser, request: Any = None
) -> tuple[StaffUser, Professional | None, int]:
    """Staff → Edit: the confirmed ``staff`` ↔ ``specialist`` role change of one account.

    → ``specialist``: an eligible Staff or Manager account becomes a Specialist; its draft profile is created
    if it never had one, else the profile it already owns is reused as it is (unpublished since it left the
    role: staff review it before republishing). → ``staff``: specialist access ends and the profile is
    unpublished (no new bookings); the profile and its appointments stay attached to the account and the
    count of outstanding visits is returned for staff to handle. The accounts service keeps the protections
    (own account, inactive, superuser, operations-console access, last active manager) and revokes the
    account's sessions on a change. Asking for the role the account already has changes nothing, so a retry
    never creates a second profile. Returns ``(account, profile, upcoming appointments)``."""
    profile = Professional.objects.select_for_update().filter(staff_user_id=staff_user_id).first()
    to_specialist = role == enums.StaffRole.SPECIALIST
    user = account_services.change_specialist_role(
        staff_user_id,
        enums.StaffRole.SPECIALIST if to_specialist else enums.StaffRole.ADMIN,
        actor=actor,
        request=request,
    )
    if to_specialist:
        if profile is None:
            profile = _create_draft_profile(user, actor=actor, request=request)
        return user, profile, 0
    if profile is None:
        return user, None, 0
    upcoming = bookings.upcoming_counts([profile.pk], timeutils.now())[profile.pk]
    if profile.is_active:
        profile.is_active = False
        profile.save(update_fields=["is_active", "updated_at"])
        audit.record(
            action="professional.deactivated",
            entity_type="professional",
            entity_id=str(profile.pk),
            actor=actor,
            request=request,
            before={"is_active": True},
            after={"is_active": False, "upcoming_appointments": upcoming, "reason": "role_changed"},
        )
    return user, profile, upcoming


__all__ = [
    "OWN_PROFILE_KEYS",
    "PUBLISH_BLOCKED",
    "PUBLISH_BLOCKER_MESSAGES",
    "SPECIALIST_SCALARS",
    "check_clinic_assignments",
    "checked_clinics",
    "invite_specialist",
    "set_specialist_account_role",
    "update_own_profile",
    "update_specialist",
]
