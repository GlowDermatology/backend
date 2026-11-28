"""Writing a template edit staff made (contract §2.20.9), once whatever needed the AI is known.

:mod:`.library` checks an edit and either applies it here at once (names, visibility, a removed reference, a
cleared text: nothing for the AI) or hands it to :mod:`.template_jobs`, whose worker asks the analyst first
(the translation of a typed scene, the screening and description of an uploaded reference) and then applies
it here. Either way one function writes the row and its audit entry, so a save means the same thing on both
paths. No AI call is made here: the API process holds no AI key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant.models import SceneTemplate
from glowderma.assistant.scenes import services as scenes
from glowderma.core import audit, enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.uzsource import N_

SCENE_NEEDED = N_("Sahnani tasvirlang yoki namuna rasm yuklang.")


@dataclass(slots=True)
class Checked:
    """What the analyst made of an edit: the typed scene in English, the uploaded reference and its English
    description (``None``: not part of the edit)."""

    english: str | None = None
    reference: ContentFile[bytes] | None = None
    reference_description: str = ""


@dataclass(slots=True)
class Edit:
    """A checked edit (``None``: unchanged). ``text`` is the staff's own words, already whitespace-folded."""

    name: dict[str, str] | None = None
    text: str | None = None
    remove_reference: bool = False
    enabled: bool | None = None

    def as_inputs(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "text": self.text,
            "remove_reference": self.remove_reference,
            "enabled": self.enabled,
        }

    @classmethod
    def of_inputs(cls, inputs: dict[str, Any]) -> Edit:
        name = inputs.get("name")
        text = inputs.get("text")
        enabled = inputs.get("enabled")
        return cls(
            name=dict(name) if isinstance(name, dict) else None,
            text=str(text) if text is not None else None,
            remove_reference=bool(inputs.get("remove_reference")),
            enabled=bool(enabled) if enabled is not None else None,
        )


def snapshot(row: SceneTemplate) -> dict[str, Any]:
    """What the audit log keeps of a template (the scene text by length only)."""
    return {
        "name": {"uz": row.name_uz, "ru": row.name_ru, "en": row.name_en},
        "enabled": row.enabled,
        "prompt": {"length": len(row.prompt)},
        "reference": row.reference_source or None,
    }


def create(key: str, edit: Edit, checked: Checked, *, actor: Any, request: Any = None) -> SceneTemplate:
    """A new AI template, last in the list."""
    name = edit.name or {}
    with transaction.atomic():
        last = SceneTemplate.objects.order_by("-position").values_list("position", flat=True).first() or 0
        row = SceneTemplate(
            key=key,
            kind=enums.SceneTemplateKind.AI_SCENE,
            name_uz=name.get("uz", ""),
            name_ru=name.get("ru", ""),
            name_en=name.get("en", ""),
            enabled=True if edit.enabled is None else edit.enabled,
            position=last + 1,
            prompt=checked.english or "",
            prompt_original=edit.text or "",
            created_by=actor,
        )
        if checked.reference is not None:  # on the row before its first save (a picture alone is a scene too)
            row.reference.save(checked.reference.name or "reference.webp", checked.reference, save=False)
            row.reference_source = enums.SceneReferenceSource.UPLOAD
            row.reference_description = checked.reference_description
        row.save()
        audit.record(
            action="assistant.scene_template_created",
            entity_type="assistant_scene_template",
            entity_id=row.key,
            actor=actor,
            request=request,
            after=snapshot(row),
        )
    return row


def update(
    template_id: int, edit: Edit, checked: Checked, *, actor: Any, request: Any = None
) -> SceneTemplate:
    """Applies ``edit`` to the template as it is now (a concurrent edit of other fields is kept)."""
    with transaction.atomic():
        row = SceneTemplate.objects.select_for_update().filter(pk=template_id).first()
        if row is None:
            raise NotFound
        studio = row.kind == enums.SceneTemplateKind.STUDIO
        before = snapshot(row)
        if edit.name is not None:
            row.name_uz, row.name_ru, row.name_en = edit.name["uz"], edit.name["ru"], edit.name["en"]
        if edit.enabled is not None:
            row.enabled = edit.enabled
        retexted = edit.text is not None and checked.english is not None
        if retexted:
            row.prompt, row.prompt_original = checked.english or "", edit.text or ""
        keeps_reference = bool(row.reference) and not edit.remove_reference
        if not studio and not row.prompt and checked.reference is None and not keeps_reference:
            raise ValidationError({"text": [field_error("invalid", SCENE_NEEDED)]})
        row.save()
        if checked.reference is not None:
            scenes.store_reference(
                row,
                checked.reference,
                source=enums.SceneReferenceSource.UPLOAD,
                description=checked.reference_description,
            )
        elif edit.remove_reference and row.reference:
            scenes.store_reference(row, None)
        elif retexted:
            scenes.template_changed(row)  # a reference made from the old words is cleared
        after = snapshot(row)
        if after != before:
            audit.record(
                action="assistant.scene_template_updated",
                entity_type="assistant_scene_template",
                entity_id=row.key,
                actor=actor,
                request=request,
                before=before,
                after=after,
            )
    return row
