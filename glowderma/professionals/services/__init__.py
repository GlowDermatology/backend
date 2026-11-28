"""Writes of `professionals` (design 01 §5, 03 §8): every row of the six editable models — and the date
override — is created, changed and deleted here, validated against the model invariants and audited as
``<snake_model>.created|updated|deleted`` (contract §3.2 "Django admin (editable models)").

The Django admin (:mod:`glowderma.professionals.admin`) routes ``save_model`` / ``delete_model`` / inline
formsets through :func:`save_row` and :func:`delete_row` (``editable_rows``, with the named helpers other code
— seeders, M2 — uses). The admin REST API (FND-1, ``api/admin/``) has its own operations, one subject per
module: ``specialists``, ``specialist_photos``, ``schedules``, ``specialist_services``, ``locations`` and
``home`` (the landing's specialists); they raise the contract's DRF errors and audit one row per operation
(``professional.*``, ``service.*``, ``schedule.*``, ``location.*``, contract "Mutaxassislar va xizmatlar";
``home_section.saved``). ``common`` holds their shared rules.
"""

from glowderma.professionals.services.common import (
    API_MESSAGES,
    BLOCK_MAX_DAYS,
    DURATION_STEP_MIN,
    LOCATION_LOCK,
    MAX_PRICE,
    MAX_SERVICES_PER_SPECIALIST,
    OVERRIDE_CONSTRAINT,
    OVERRIDE_HORIZON_DAYS,
    PHOTO_KIND,
    STAFF_USER_CONSTRAINT,
)
from glowderma.professionals.services.editable_rows import (
    EDITABLE_MODELS,
    ENTITY_NAMES,
    IMAGE_MESSAGES,
    PROTECTED_MESSAGE,
    SPECIALIST_LINK_ERROR,
    add_blocked_period,
    add_override,
    admin_upcoming_message,
    deactivate_professional,
    delete_row,
    entity_name,
    remove_blocked_period,
    save_row,
    set_working_hours,
    snapshot,
    store_photo,
    update_professional,
)
from glowderma.professionals.services.home import (
    HOME_SPECIALIST_HIDDEN,
    set_home_specialists,
)
from glowderma.professionals.services.locations import (
    create_location,
    update_location,
)
from glowderma.professionals.services.schedules import (
    create_block,
    create_override,
    delete_block,
    delete_override,
    set_working_week,
)
from glowderma.professionals.services.specialist_photos import (
    delete_specialist_photo,
    set_specialist_photo,
)
from glowderma.professionals.services.specialist_services import (
    SERVICE_SCALARS,
    create_specialist_service,
    default_service_category,
    delete_specialist_service,
    update_specialist_service,
)
from glowderma.professionals.services.specialists import (
    OWN_PROFILE_KEYS,
    PUBLISH_BLOCKED,
    PUBLISH_BLOCKER_MESSAGES,
    SPECIALIST_SCALARS,
    invite_specialist,
    set_specialist_account_role,
    update_own_profile,
    update_specialist,
)

__all__ = [
    "API_MESSAGES",
    "BLOCK_MAX_DAYS",
    "DURATION_STEP_MIN",
    "EDITABLE_MODELS",
    "ENTITY_NAMES",
    "HOME_SPECIALIST_HIDDEN",
    "IMAGE_MESSAGES",
    "LOCATION_LOCK",
    "MAX_PRICE",
    "MAX_SERVICES_PER_SPECIALIST",
    "OVERRIDE_CONSTRAINT",
    "OVERRIDE_HORIZON_DAYS",
    "OWN_PROFILE_KEYS",
    "PHOTO_KIND",
    "PROTECTED_MESSAGE",
    "PUBLISH_BLOCKED",
    "PUBLISH_BLOCKER_MESSAGES",
    "SERVICE_SCALARS",
    "SPECIALIST_LINK_ERROR",
    "SPECIALIST_SCALARS",
    "STAFF_USER_CONSTRAINT",
    "add_blocked_period",
    "add_override",
    "admin_upcoming_message",
    "create_block",
    "create_location",
    "create_override",
    "create_specialist_service",
    "deactivate_professional",
    "default_service_category",
    "delete_block",
    "delete_override",
    "delete_row",
    "delete_specialist_photo",
    "delete_specialist_service",
    "entity_name",
    "invite_specialist",
    "remove_blocked_period",
    "save_row",
    "set_home_specialists",
    "set_specialist_account_role",
    "set_specialist_photo",
    "set_working_hours",
    "set_working_week",
    "snapshot",
    "store_photo",
    "update_location",
    "update_own_profile",
    "update_professional",
    "update_specialist",
    "update_specialist_service",
]
