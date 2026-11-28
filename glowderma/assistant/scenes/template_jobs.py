"""A template edit that needs the AI, saved by the worker (contract §2.20.9).

The analyst translates a typed scene to English and screens and describes an uploaded reference. Those calls
need the Gemini key, which only the worker can read (its Vault proxy reads ``<env>/ai/*``; the API's proxy
can only write keys). So the API stores the edit and the uploaded picture on a scene job of kind
``template`` and answers 202 with it; the worker asks the analyst, then saves the template through
:mod:`.template_edits`, and the job records which template it saved (``saved_template_id``). The editor
polls the job like any scene job (``scenes/jobs/{id}/``).

A refused text or picture fails the job with its own code (its message tells staff what to change) and
nothing is saved. Only one save of a template waits at a time. These jobs make a few text calls, so they do
not wait for a running scene (:func:`.services.claim`).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from django.core.files.base import ContentFile
from django.db import transaction
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant import topics
from glowderma.assistant.models import ACTIVE_SCENE_STATES, SceneJob, SceneTemplate
from glowderma.assistant.scenes import pipeline, template_edits
from glowderma.assistant.scenes import services as scenes
from glowderma.core import enums, outbox, timeutils
from glowderma.core.api.exceptions import Conflict
from glowderma.core.images import ProcessedImage
from glowderma.core.uzsource import N_
from glowderma.integrations.llm.ports import LLMError
from glowderma.integrations.vault.ports import VaultError

logger = logging.getLogger(__name__)

# a reference that would put something into every scene: the flag the analyst set → the job's error code
REFERENCE_REFUSALS = (
    ("has_product", "template_reference_product"),
    ("has_people", "template_reference_people"),
    ("has_text", "template_reference_text"),
)


class TemplateSaving(Conflict):
    default_code = "scene_template_saving"
    default_detail = N_("Shablon hali saqlanmoqda: AI tekshiruvi tugashini kuting.")


def queue(
    *,
    template: SceneTemplate | None,
    key: str,
    edit: template_edits.Edit,
    reference: ProcessedImage | None,
    actor: Any,
) -> SceneJob:
    """The edit waits for the worker; ``template`` is ``None`` for a new one (``key`` names it)."""
    with transaction.atomic():
        if template is not None:
            SceneTemplate.objects.select_for_update().filter(pk=template.pk).first()
            if SceneJob.objects.filter(
                kind=enums.SceneJobKind.TEMPLATE, template=template, state__in=ACTIVE_SCENE_STATES
            ).exists():
                raise TemplateSaving()
        name = edit.name or (
            {"uz": template.name_uz, "ru": template.name_ru, "en": template.name_en} if template else {}
        )
        job = SceneJob(
            kind=enums.SceneJobKind.TEMPLATE,
            template=template,
            template_snapshot={"key": key, "name": name},
            inputs=edit.as_inputs(),
            source_sha256="",
            created_by=actor,
        )
        if reference is not None:
            digest = hashlib.sha256(reference.content).hexdigest()[:12]
            job.candidate.save(
                f"{key}-{digest}.{reference.extension}", ContentFile(reference.content), save=False
            )
        job.save()
        outbox.enqueue(topics.SCENE_QUEUED, {"job_id": job.pk}, dedupe_key=f"{topics.SCENE_QUEUED}:{job.pk}")
    logger.info("scene template save %s queued", job.pk)
    return job


def _picture(job: SceneJob) -> bytes | None:
    if not job.candidate:
        return None
    with job.candidate.storage.open(str(job.candidate.name), "rb") as handle:
        return bytes(handle.read())


def run(job: SceneJob) -> SceneJob | None:
    """The worker side: the analyst's checks, then the save."""
    row = scenes.get_settings()
    usage = pipeline.Usage()
    edit = template_edits.Edit.of_inputs(dict(job.inputs or {}))
    checked = template_edits.Checked()
    try:
        picture = _picture(job)
        used, config = scenes.providers(row), scenes.config_of(row)
        if edit.text:
            checked.english = pipeline.custom_scene(edit.text, used, config, usage)
        elif edit.text is not None:
            checked.english = ""  # the scene's words were cleared
        if picture is not None:
            verdict = pipeline.describe_reference(pipeline.load_photo(picture), used, config, usage)
            for flag, code in REFERENCE_REFUSALS:
                if verdict[flag]:
                    return scenes.fail(job, code, usage=usage)
            name = str(job.candidate.name).rsplit("/", 1)[-1]
            checked.reference = ContentFile(picture, name=name)
            checked.reference_description = str(verdict["scene"])
    except pipeline.SceneError as exc:
        return scenes.fail(job, exc.code, usage=exc.usage or usage)
    except LLMError as exc:
        return scenes.fail(job, exc.code, usage=usage)
    except VaultError:
        return scenes.fail(job, "vault_unavailable", usage=usage)
    except OSError:  # the stored picture is gone
        return scenes.fail(job, scenes.SERVER_ERROR, usage=usage)
    return _save(job, edit, checked, usage)


def _save(
    job: SceneJob, edit: template_edits.Edit, checked: template_edits.Checked, usage: pipeline.Usage
) -> SceneJob | None:
    snapshot = dict(job.template_snapshot or {})
    try:
        with transaction.atomic():
            locked = SceneJob.objects.select_for_update().filter(pk=job.pk).first()
            if locked is None or locked.state != enums.JobState.RUNNING:
                return None
            actor = locked.created_by
            if locked.template_id is None:
                saved = template_edits.create(str(snapshot.get("key") or ""), edit, checked, actor=actor)
            else:
                saved = template_edits.update(locked.template_id, edit, checked, actor=actor)
            instant = timeutils.now()
            snapshot["saved_template_id"] = saved.pk
            locked.template = saved
            locked.template_snapshot = snapshot
            locked.state = enums.JobState.SUCCEEDED
            locked.outcome = enums.SceneJobOutcome.ACCEPTED  # nothing to review: never expires
            locked.usage = scenes._usage_dict(usage)
            locked.cost_usd = scenes._cost(scenes.get_settings(), usage)
            locked.finished_at = instant
            locked.heartbeat_at = instant
            locked.reviewed_at = instant
            locked.save()
            scenes._drop_files_on_commit(locked)  # the picture now lives on the template
            return locked
    except NotFound:
        return scenes.fail(job, "scene_template_unavailable", usage=usage)
    except ValidationError:  # the template lost its scene meanwhile
        return scenes.fail(job, "template_scene_needed", usage=usage)
