"""Scenes staff type themselves, saving one as a template, and the templates' sample pictures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from django.test import Client, override_settings
from django.urls import reverse
from PIL import Image
from rest_framework.test import APIClient

from glowderma.assistant.models import SceneJob, SceneSettings, SceneTemplate
from glowderma.assistant.scenes import pipeline, prompts
from glowderma.assistant.scenes import services as scene_services
from glowderma.assistant.tests.test_console import superuser_client
from glowderma.assistant.tests.test_scene_jobs import ADMIN, run_and_poll, saved_image, start, template
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm.ports import GenerationResult, LLMUnavailable
from glowderma.integrations.llm.registry import fake_content_model, fake_image_model
from glowderma.integrations.segmentation.registry import fake_segmenter

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes")]


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path) -> Iterator[None]:
    with override_settings(MEDIA_ROOT=tmp_path / "media"):
        fake_segmenter().reset()
        fake_image_model().reset()
        fake_content_model().reset()
        yield
        fake_segmenter().reset()
        fake_image_model().reset()
        fake_content_model().reset()


@pytest.fixture
def su() -> Client:
    return superuser_client()


@pytest.fixture
def ai_on() -> SceneSettings:
    row = SceneSettings.objects.get()
    row.enabled = row.ai_enabled = True
    row.save()
    return row


def translation(scene: str, *, refused: bool = False, reason: str = "") -> GenerationResult:
    return GenerationResult(
        data={"scene": scene, "refused": refused, "reason": reason},
        input_tokens=200,
        output_tokens=40,
        model="t",
    )


ROSES = "на мраморе с лепестками роз, мягкий утренний свет"
ENGLISH = "A white marble surface with scattered rose petals, soft morning light."


# --- a scene staff typed ------------------------------------------------------------------------------------


def test_a_typed_scene_is_translated_once_and_runs(staff_client: APIClient, ai_on: SceneSettings) -> None:
    product, image = saved_image()
    fake_content_model().push(translation(ENGLISH))
    response = start(staff_client, product, image, kind="ai_scene", custom_prompt=f"  {ROSES}  ")
    assert response.status_code == 202, response.json()
    job_id = response.json()["id"]
    assert response.json()["template"] == {
        "key": "custom",
        "name": "O‘z sahnasi",
        "custom_text": ROSES,
        "saved_template_id": None,
    }
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    sent = fake_content_model().calls[0]
    assert ROSES in sent.prompt and sent.response_schema == prompts.CUSTOM_SCHEMA
    job = SceneJob.objects.get(pk=job_id)
    assert job.template_snapshot["prompt"] == ENGLISH  # kept for a retry and «save as template»
    assert f"SCENE: {ENGLISH}" in fake_image_model().calls[0].prompt  # the English text, inside the rules
    assert job.usage["text_calls"] >= 1


def test_a_typed_scene_asking_for_people_is_refused(staff_client: APIClient, ai_on: SceneSettings) -> None:
    product, image = saved_image()
    fake_content_model().push(translation("", refused=True, reason="asks for a model holding it"))
    job_id = start(staff_client, product, image, kind="ai_scene", custom_prompt="девушка держит крем").json()[
        "id"
    ]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["error"]["code"] == pipeline.CUSTOM_REFUSED
    assert "odamlar" in body["error"]["message"] and fake_image_model().calls == []


def test_a_typed_scene_follows_the_rules_of_ai_scenes(staff_client: APIClient, ai_on: SceneSettings) -> None:
    product, image = saved_image()
    too_long = start(staff_client, product, image, kind="ai_scene", custom_prompt="x" * 401)
    assert too_long.status_code == 400
    studio = start(staff_client, product, image, kind="studio", custom_prompt=ROSES)
    assert studio.status_code == 400
    assert "custom_prompt" in [error["field"] for error in studio.json()["errors"]]
    preview = start(staff_client, product, image, kind="preview", template_id=template("marble").pk)
    assert preview.status_code == 400  # previews are the console's, never staff's
    ai_on.ai_enabled = False
    ai_on.save()
    off = start(staff_client, product, image, kind="ai_scene", custom_prompt=ROSES)
    assert off.status_code == 409 and off.json()["code"] == "scene_ai_disabled"


def test_a_translation_failure_fails_the_job(staff_client: APIClient, ai_on: SceneSettings) -> None:
    product, image = saved_image()
    fake_content_model().push(LLMUnavailable("down"))
    job_id = start(staff_client, product, image, kind="ai_scene", custom_prompt=ROSES).json()["id"]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["error"]["code"] == "provider_unavailable"


# --- saving it as a template --------------------------------------------------------------------------------


def finished_custom_job(client: APIClient) -> int:
    product, image = saved_image()
    fake_content_model().push(translation(ENGLISH))
    job_id: int = start(client, product, image, kind="ai_scene", custom_prompt=ROSES).json()["id"]
    assert run_and_poll(client, job_id)["state"] == "succeeded"
    return job_id


def test_a_typed_scene_saved_as_a_template(staff_client: APIClient, ai_on: SceneSettings) -> None:
    job_id = finished_custom_job(staff_client)
    response = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": "  Розы "}, format="json")
    assert response.status_code == 201, response.json()
    saved = response.json()
    assert saved["key"] == f"custom-{job_id}" and saved["kind"] == "ai_scene" and saved["name"] == "Розы"
    assert saved["preview_url"].endswith(".webp")
    row = SceneTemplate.objects.get(key=f"custom-{job_id}")
    assert row.prompt == ENGLISH and row.enabled and row.created_by is not None
    assert row.position == SceneTemplate.objects.exclude(pk=row.pk).order_by("-position").first().position + 1
    with Image.open(row.preview.path) as picture:
        assert picture.width == scene_services.PREVIEW_WIDTH
    listed = staff_client.get(f"{ADMIN}scenes/templates/").json()["results"]
    assert listed[-1]["key"] == row.key
    job = staff_client.get(f"{ADMIN}scenes/jobs/{job_id}/").json()
    assert job["template"]["saved_template_id"] == row.pk
    assert AuditEntry.objects.filter(action="assistant.scene_template_created", entity_id=row.key).exists()
    twice = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": "Again"}, format="json")
    assert twice.status_code == 409 and twice.json()["code"] == "scene_template_exists"


def test_saving_after_the_scene_was_accepted(staff_client: APIClient, ai_on: SceneSettings) -> None:
    job_id = finished_custom_job(staff_client)
    accepted = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json")
    assert accepted.status_code == 200, accepted.json()
    response = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": "Roses"}, format="json")
    assert response.status_code == 201 and response.json()["preview_url"]  # from the accepted photo


def test_only_a_finished_typed_scene_can_be_saved(staff_client: APIClient, ai_on: SceneSettings) -> None:
    product, image = saved_image()
    job_id = start(staff_client, product, image, kind="ai_scene", template_id=template("marble").pk).json()[
        "id"
    ]
    run_and_poll(staff_client, job_id)
    refused = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": "Marble"}, format="json")
    assert refused.status_code == 409 and refused.json()["code"] == "scene_not_custom"
    blank = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": " "}, format="json")
    assert blank.status_code == 400
    missing = staff_client.post(f"{ADMIN}scenes/jobs/999999/template/", {"name": "X"}, format="json")
    assert missing.status_code == 404


# --- the templates' sample pictures -------------------------------------------------------------------------


def test_the_sample_product_is_a_cut_out() -> None:
    sample = scene_services.sample_product()
    analysis = pipeline.analysis_of_cutout(sample)
    assert sample.mode == "RGBA" and analysis.occluded_pct == 0 and analysis.box[3] > analysis.box[1]


def test_a_studio_preview_is_drawn_at_once_and_redrawn_on_change(ai_on: SceneSettings, staff: Any) -> None:
    ivory = template("studio-ivory")
    assert scene_services.start_preview(ivory.pk, actor=staff) is None  # no job: drawn now
    ivory.refresh_from_db()
    first = ivory.preview.name
    assert first.startswith("scene-previews/studio-ivory-") and ivory.preview_url
    from glowderma.assistant.console import writes

    writes.update_scene_template(ivory.pk, {"params": {**ivory.params, "colours": ["#000000"]}}, actor=staff)
    ivory.refresh_from_db()
    assert ivory.preview.name and ivory.preview.name != first  # a new picture for the new colours


def test_an_ai_preview_is_a_billed_job_once_per_template(ai_on: SceneSettings, staff: Any) -> None:
    marble = template("marble")
    job = scene_services.start_preview(marble.pk, actor=staff)
    assert job is not None and job.kind == enums.SceneJobKind.PREVIEW and job.source_image_id is None
    assert scene_services.start_preview(marble.pk, actor=staff) == job  # a double click bills once
    done = scene_services.run(job.pk)
    assert done is not None and done.state == enums.JobState.SUCCEEDED, done and done.error_code
    assert done.outcome == enums.SceneJobOutcome.ACCEPTED and done.expires_at is None
    marble.refresh_from_db()
    assert marble.preview_url and fake_image_model().calls  # the image model drew it
    assert scene_services.expire_candidates() == 0  # nothing to review, nothing expires

    # The prompt changes: the old picture shows another scene, so it goes (an AI one is not redrawn).
    from glowderma.assistant.console import writes

    writes.update_scene_template(marble.pk, {"prompt": "Black slate, wet, moody light."}, actor=staff)
    marble.refresh_from_db()
    assert not marble.preview


def test_a_preview_of_a_template_edited_meanwhile_is_dropped(ai_on: SceneSettings, staff: Any) -> None:
    marble = template("marble")
    job = scene_services.start_preview(marble.pk, actor=staff)
    assert job is not None
    SceneTemplate.objects.filter(pk=marble.pk).update(prompt="Something else entirely.")
    done = scene_services.run(job.pk)
    assert done is not None and done.state == enums.JobState.SUCCEEDED
    marble.refresh_from_db()
    assert not marble.preview


def test_previews_respect_the_scene_budget(ai_on: SceneSettings, staff: Any) -> None:
    ai_on.daily_budget_usd = 0
    ai_on.save()
    with pytest.raises(scene_services.SceneBudgetExceeded):
        scene_services.start_preview(template("marble").pk, actor=staff)
    assert scene_services.start_preview(template("studio-ivory").pk, actor=staff) is None  # free


def test_the_console_makes_the_missing_previews(su: Client) -> None:
    response = su.post(reverse("admin:assistant_scenes"), {"action": "missing_previews"})
    assert response.status_code == 302
    studio = SceneTemplate.objects.filter(kind=enums.SceneTemplateKind.STUDIO)
    assert all(row.preview for row in studio)
    queued = SceneJob.objects.filter(kind=enums.SceneJobKind.PREVIEW).count()
    assert queued == SceneTemplate.objects.filter(kind=enums.SceneTemplateKind.AI_SCENE).count()
    marble = template("marble")
    page = su.get(reverse("admin:assistant_scene_template", args=[marble.pk]))
    assert page.status_code == 200 and "Namuna tayyorlanmoqda" in page.content.decode()
    clicked = su.post(reverse("admin:assistant_scene_template", args=[marble.pk]), {"action": "preview"})
    assert clicked.status_code == 302
    assert SceneJob.objects.filter(kind=enums.SceneJobKind.PREVIEW, template=marble).count() == 1
