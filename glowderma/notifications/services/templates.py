"""The status push templates."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from glowderma.core import audit, timeutils
from glowderma.notifications.models import (
    StatusTemplate,
)


def template_texts(template: StatusTemplate) -> dict[str, str]:
    return {"uz": template.text_uz, "ru": template.text_ru, "en": template.text_en}


@transaction.atomic
def update_template(
    status: str, texts_: dict[str, str], *, actor: Any, request: Any = None
) -> StatusTemplate:
    """``PUT notification-templates/{status}/`` (validated by the serializer); audit the diff."""
    template = StatusTemplate.objects.select_for_update().get(status=status)
    before = template_texts(template)
    template.text_uz, template.text_ru, template.text_en = texts_["uz"], texts_["ru"], texts_["en"]
    template.updated_at = timeutils.now()
    template.updated_by = actor
    template.save()
    after = template_texts(template)
    audit.record(
        action="notification_template.updated",
        entity_type="notification_template",
        entity_id=status,
        actor=actor,
        request=request,
        before={key: value for key, value in before.items() if after[key] != value},
        after={key: value for key, value in after.items() if before[key] != value},
    )
    return template


__all__ = [
    "template_texts",
    "update_template",
]
