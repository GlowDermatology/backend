"""The scene templates staff manage in the admin panel (contract §2.20.9): create an AI template, edit a
template's names, scene text and reference picture, hide it.

An AI template is a reference picture of the scene alone and/or a description of it:

- The description is typed in any language. The analyst turns it into English (``prompt``, what the models
  read) and refuses one that asks for people, text or brands. The staff's own words stay in
  ``prompt_original``, shown in the editor.
- An uploaded reference is screened and described by the analyst in the same request. A picture with a
  product, a person or lettering would put them in every scene, so it is refused. The English description is
  what Bria's text-only mode follows; Gemini also sees the picture.
- Without an upload, a reference can be made from the text («Rasmini yaratish», a billed job:
  ``services.start_preview``).

Studio templates (composed on our server) only change names and visibility here; their colours stay in the
ops console. Every save is audited (``assistant.scene_template_created`` / ``…_updated``).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant.models import SceneTemplate
from glowderma.assistant.scenes import pipeline, prompts
from glowderma.assistant.scenes import services as scenes
from glowderma.core import audit, enums
from glowderma.core.api.exceptions import field_error
from glowderma.core.images import ImageError, process_image
from glowderma.core.uzsource import N_
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.vault.ports import VaultError

logger = logging.getLogger(__name__)

NAME_MAX = 60
TEXT_MAX = prompts.CUSTOM_MAX_CHARS

MESSAGES = {
    "name": N_("Shablon nomini yozing (60 belgigacha)."),
    "scene": N_("Sahnani tasvirlang yoki namuna rasm yuklang."),
    "text": N_("Sahnani tasvirlang (ko‘pi bilan 400 belgi)."),
    "refused": N_(
        "Bu tavsif bo‘yicha sahna yaratib bo‘lmaydi: unda odamlar, qo‘llar, yozuv yoki brend bo‘lmasligi "
        "kerak. Faqat fon va atrofni tasvirlang."
    ),
    "has_product": N_(
        "Namuna rasmda mahsulot bor: u har bir sahnaga tushib qoladi. Faqat sahnaning o‘zi (sirt, fon, "
        "bezaklar) bor rasm yuklang."
    ),
    "has_people": N_("Namuna rasmda odam yoki qo‘l bor. Faqat sahnaning o‘zi bor rasm yuklang."),
    "has_text": N_("Namuna rasmda yozuv yoki logotip bor. Yozuvsiz rasm yuklang."),
    "ai_unavailable": N_("AI hozir javob bermadi: birozdan so‘ng qayta saqlab ko‘ring."),
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


def _invalid(field: str, key: str) -> ValidationError:
    return ValidationError({field: [field_error("invalid", MESSAGES[key])]})


def _clean_name(name: dict[str, str]) -> dict[str, str]:
    clean = {language: " ".join(str(name.get(language) or "").split()) for language in ("uz", "ru", "en")}
    if not clean["uz"] or any(len(value) > NAME_MAX for value in clean.values()):
        raise _invalid("name.uz" if not clean["uz"] else "name", "name")
    return clean


def _ai_tools() -> tuple[pipeline.Providers, pipeline.Config]:
    row = scenes.get_settings()
    if not row.enabled:
        raise scenes.ScenesDisabled()
    if not row.ai_enabled:
        raise scenes.AiScenesDisabled()
    try:
        return scenes.providers(row), scenes.config_of(row)
    except LLMError, VaultError:
        raise _invalid("reference", "ai_unavailable") from None


def _translate(text: str) -> str:
    """The scene in English (one analyst call); a refused one is a field error."""
    used, config = _ai_tools()
    try:
        return pipeline.custom_scene(text, used, config, pipeline.Usage())
    except pipeline.SceneError:
        raise _invalid("text", "refused") from None
    except LLMError, VaultError:
        raise _invalid("text", "ai_unavailable") from None


def _reference(upload: Any, key: str) -> tuple[ContentFile[bytes], str]:
    """An uploaded reference, screened and described (one analyst call): its file and English description."""
    try:
        processed = process_image(upload)
    except ImageError:
        raise _invalid("reference", "image") from None
    picture = pipeline.load_photo(processed.content)
    used, config = _ai_tools()
    try:
        verdict = pipeline.describe_reference(picture, used, config, pipeline.Usage())
    except LLMError, VaultError:
        raise _invalid("reference", "ai_unavailable") from None
    for flag in ("has_product", "has_people", "has_text"):
        if verdict[flag]:
            raise _invalid("reference", flag)
    digest = hashlib.sha256(processed.content).hexdigest()[:12]
    content = ContentFile(processed.content, name=f"{key}-{digest}.{processed.extension}")
    return content, str(verdict["scene"])


def _snapshot(row: SceneTemplate) -> dict[str, Any]:
    return {
        "name": {"uz": row.name_uz, "ru": row.name_ru, "en": row.name_en},
        "enabled": row.enabled,
        "prompt": {"length": len(row.prompt)},
        "reference": row.reference_source or None,
    }


def create_template(data: TemplateInput, *, actor: Any, request: Any = None) -> SceneTemplate:
    """A new AI template, last in the list."""
    name = _clean_name(data.name or {})
    typed = " ".join((data.text or "").split())
    if len(typed) > TEXT_MAX:
        raise _invalid("text", "text")
    if not typed and data.reference is None:
        raise _invalid("text", "scene")
    key = f"staff-{secrets.token_hex(4)}"
    english = _translate(typed) if typed else ""
    reference = _reference(data.reference, key) if data.reference is not None else None
    with transaction.atomic():
        last = SceneTemplate.objects.order_by("-position").values_list("position", flat=True).first() or 0
        row = SceneTemplate(
            key=key,
            kind=enums.SceneTemplateKind.AI_SCENE,
            name_uz=name["uz"],
            name_ru=name["ru"],
            name_en=name["en"],
            enabled=True if data.enabled is None else data.enabled,
            position=last + 1,
            prompt=english,
            prompt_original=typed,
            created_by=actor,
        )
        if reference is not None:  # on the row before its first save (a picture alone is a scene too)
            row.reference.save(reference[0].name or "reference.webp", reference[0], save=False)
            row.reference_source = enums.SceneReferenceSource.UPLOAD
            row.reference_description = reference[1]
        row.save()
        audit.record(
            action="assistant.scene_template_created",
            entity_type="assistant_scene_template",
            entity_id=row.key,
            actor=actor,
            request=request,
            after=_snapshot(row),
        )
    return row


def update_template(
    template_id: int, data: TemplateInput, *, actor: Any, request: Any = None
) -> SceneTemplate:
    current = SceneTemplate.objects.filter(pk=template_id).first()
    if current is None:
        raise NotFound
    studio = current.kind == enums.SceneTemplateKind.STUDIO
    if studio and (data.text is not None or data.reference is not None or data.remove_reference):
        raise _invalid("text", "studio")
    name = _clean_name(data.name) if data.name is not None else None
    typed = " ".join(data.text.split()) if data.text is not None else None
    if typed is not None and len(typed) > TEXT_MAX:
        raise _invalid("text", "text")
    # the slow calls (translation, screening) before the row is locked
    text_changed = typed is not None and typed != (current.prompt_original or current.prompt)
    english = (_translate(typed) if typed else "") if text_changed and typed is not None else None
    reference = _reference(data.reference, current.key) if data.reference is not None else None
    with transaction.atomic():
        row = SceneTemplate.objects.select_for_update().get(pk=template_id)
        before = _snapshot(row)
        if name is not None:
            row.name_uz, row.name_ru, row.name_en = name["uz"], name["ru"], name["en"]
        if data.enabled is not None:
            row.enabled = data.enabled
        if english is not None and typed is not None:
            row.prompt, row.prompt_original = english, typed
        keeps_reference = bool(row.reference) and not data.remove_reference
        if not studio and not row.prompt and reference is None and not keeps_reference:
            raise _invalid("text", "scene")
        row.save()
        if reference is not None:
            scenes.store_reference(
                row, reference[0], source=enums.SceneReferenceSource.UPLOAD, description=reference[1]
            )
        elif data.remove_reference and row.reference:
            scenes.store_reference(row, None)
        elif english is not None:
            scenes.template_changed(row)  # a reference made from the old words is cleared
        after = _snapshot(row)
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
