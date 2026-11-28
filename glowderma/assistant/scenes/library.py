"""The scene templates staff manage in the admin panel (contract §2.20.9): create an AI template, edit a
template's names, scene text and reference picture, hide it.

An AI template is a reference picture of the scene alone and/or a description of it:

- The description is typed in any language. The analyst turns it into English (``prompt``, what the models
  read) and refuses one that asks for people, text or brands. The staff's own words stay in
  ``prompt_original``, shown in the editor.
- An uploaded reference is screened and described by the analyst. A picture with a product, a person or
  lettering would put them in every scene, so it is refused. The English description is what Bria's
  text-only mode follows; Gemini also sees the picture.
- Without an upload, a reference can be made from the text («Rasmini yaratish», a billed job:
  ``services.start_preview``).

The analyst's calls need the AI key, which only the worker reads. An edit that needs them (a new or changed
text, an uploaded reference) is saved by the worker (:mod:`.template_jobs`): the request answers with the
job. Any other edit (names, visibility, a removed reference, a cleared text) is saved at once. Both paths
write through :mod:`.template_edits`.

Studio templates (composed on our server) only change names and visibility here; their colours stay in the
ops console. Every save is audited (``assistant.scene_template_created`` / ``…_updated``).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant.models import SceneJob, SceneTemplate
from glowderma.assistant.scenes import prompts, template_edits, template_jobs
from glowderma.assistant.scenes import services as scenes
from glowderma.core import enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.images import ImageError, ProcessedImage, process_image
from glowderma.core.uzsource import N_

logger = logging.getLogger(__name__)

NAME_MAX = 60
TEXT_MAX = prompts.CUSTOM_MAX_CHARS

MESSAGES = {
    "name": N_("Shablon nomini yozing (60 belgigacha)."),
    "scene": N_("Sahnani tasvirlang yoki namuna rasm yuklang."),
    "text": N_("Sahnani tasvirlang (ko‘pi bilan 400 belgi)."),
    "image": N_("Rasmni o‘qib bo‘lmadi: JPG, PNG yoki WebP, 5 MB gacha."),
    "studio": N_("Studiya shablonining ranglari ops konsolida o‘zgartiriladi."),
}


@dataclass(slots=True)
class TemplateInput:
    """What staff sent (``None``: unchanged)."""

    name: dict[str, str] | None = None
    text: str | None = None  # the scene in staff's words ("" clears it)
    reference: Any = None  # an uploaded file
    remove_reference: bool = False
    enabled: bool | None = None


@dataclass(slots=True)
class TemplateSave:
    """A save: the template when it was written at once, else the job that checks and writes it."""

    template: SceneTemplate | None = None
    job: SceneJob | None = None


def _invalid(field: str, key: str) -> ValidationError:
    return ValidationError({field: [field_error("invalid", MESSAGES[key])]})


def _clean_name(name: dict[str, str]) -> dict[str, str]:
    clean = {language: " ".join(str(name.get(language) or "").split()) for language in ("uz", "ru", "en")}
    if not clean["uz"] or any(len(value) > NAME_MAX for value in clean.values()):
        raise _invalid("name.uz" if not clean["uz"] else "name", "name")
    return clean


def _clean_text(text: str | None) -> str | None:
    typed = " ".join(text.split()) if text is not None else None
    if typed is not None and len(typed) > TEXT_MAX:
        raise _invalid("text", "text")
    return typed


def _picture(upload: Any) -> ProcessedImage | None:
    """An uploaded reference, decoded and normalised here (no AI): a broken file is refused at once."""
    if upload is None:
        return None
    try:
        return process_image(upload)
    except ImageError:
        raise _invalid("reference", "image") from None


def _ai_on() -> None:
    row = scenes.get_settings()
    if not row.enabled:
        raise scenes.ScenesDisabled()
    if not row.ai_enabled:
        raise scenes.AiScenesDisabled()


def create_template(data: TemplateInput, *, actor: Any) -> TemplateSave:
    """A new AI template: always checked by the analyst first (it has a text or a reference), so a job."""
    name = _clean_name(data.name or {})
    typed = _clean_text(data.text) or ""
    if not typed and data.reference is None:
        raise _invalid("text", "scene")
    picture = _picture(data.reference)
    _ai_on()
    edit = template_edits.Edit(name=name, text=typed or None, enabled=data.enabled)
    key = f"staff-{secrets.token_hex(4)}"
    return TemplateSave(
        job=template_jobs.queue(template=None, key=key, edit=edit, reference=picture, actor=actor)
    )


def update_template(
    template_id: int, data: TemplateInput, *, actor: Any, request: Any = None
) -> TemplateSave:
    current = SceneTemplate.objects.filter(pk=template_id).first()
    if current is None:
        raise NotFound
    studio = current.kind == enums.SceneTemplateKind.STUDIO
    if studio and (data.text is not None or data.reference is not None or data.remove_reference):
        raise _invalid("text", "studio")
    name = _clean_name(data.name) if data.name is not None else None
    typed = _clean_text(data.text)
    if typed == (current.prompt_original or current.prompt):
        typed = None  # unchanged
    picture = _picture(data.reference)
    edit = template_edits.Edit(
        name=name,
        text=typed,
        remove_reference=data.remove_reference and picture is None,
        enabled=data.enabled,
    )
    if typed or picture is not None:  # the analyst's turn: on the worker
        _ai_on()
        return TemplateSave(
            job=template_jobs.queue(
                template=current, key=current.key, edit=edit, reference=picture, actor=actor
            )
        )
    checked = template_edits.Checked(english="" if typed == "" else None)  # a cleared text needs no AI
    return TemplateSave(
        template=template_edits.update(template_id, edit, checked, actor=actor, request=request)
    )
