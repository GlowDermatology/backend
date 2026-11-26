"""Product scene jobs end to end with the fakes (no network): start, run, poll, accept, discard, fit, the
switches, limits and housekeeping (admin contract §2.20)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings
from PIL import Image, ImageDraw
from rest_framework.test import APIClient

from glowderma.assistant import tasks
from glowderma.assistant.models import SceneJob, SceneSettings, SceneTemplate
from glowderma.assistant.scenes import services as scene_services
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Product, ProductImage
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm.registry import fake_image_model
from glowderma.integrations.segmentation.registry import fake_segmenter

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes")]

ADMIN = "/api/v1/admin/"


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path) -> Iterator[None]:
    with override_settings(MEDIA_ROOT=tmp_path / "media"):
        fake_segmenter().reset()
        fake_image_model().reset()
        yield
        fake_segmenter().reset()
        fake_image_model().reset()


@pytest.fixture
def scenes_on() -> SceneSettings:
    row = SceneSettings.objects.get()
    row.enabled = True
    row.save()
    return row


def photo_bytes(box: tuple[int, int, int, int] = (330, 300, 570, 1000)) -> bytes:
    """A studio-like photo: a labelled pack on a plain background (the fake segmenter keys the corners)."""
    image = Image.new("RGB", (900, 1200), (200, 196, 188))
    draw = ImageDraw.Draw(image)
    draw.rectangle([box[0], box[1], box[2] - 1, box[3] - 1], fill=(30, 60, 140))
    for y in range(box[1] + 150, box[1] + 450, 14):
        draw.line([(box[0] + 30, y), (box[2] - 30, y)], fill=(250, 230, 120), width=4)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def saved_image(box: tuple[int, int, int, int] = (330, 300, 570, 1000)) -> tuple[Product, ProductImage]:
    product = ProductFactory.create(status="hidden")
    image = catalog_services.add_image(product.pk, photo_bytes(box), actor=None)
    return product, image


def template(key: str) -> SceneTemplate:
    return SceneTemplate.objects.get(key=key)


def start(client: APIClient, product: Product, image: ProductImage, **body: Any) -> Any:
    return client.post(f"{ADMIN}products/{product.pk}/images/{image.pk}/scenes/", body, format="json")


def run_and_poll(client: APIClient, job_id: int) -> dict[str, Any]:
    scene_services.run(job_id)
    response = client.get(f"{ADMIN}scenes/jobs/{job_id}/")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


def test_status_and_templates(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    status = staff_client.get(f"{ADMIN}scenes/status/").json()
    assert status == {
        "enabled": True,
        "ai_enabled": False,
        "max_occluded_pct": "15.00",
        "segmenter": "gemini_matte",
    }
    templates = staff_client.get(f"{ADMIN}scenes/templates/", HTTP_ACCEPT_LANGUAGE="ru").json()["results"]
    assert [t["key"] for t in templates][:2] == ["studio-ivory", "studio-gold-glow"] and len(templates) == 8
    ivory = templates[0]
    assert ivory["name"] == "Слоновая кость" and ivory["kind"] == "studio" and "#f7f2ea" in ivory["colours"]


def test_scenes_ship_switched_off(staff_client: APIClient) -> None:
    product, image = saved_image()
    response = start(staff_client, product, image, kind="studio", template_id=template("studio-ivory").pk)
    assert response.status_code == 409 and response.json()["code"] == "scenes_disabled"


def test_studio_scene_start_run_accept(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    response = start(staff_client, product, image, kind="studio", template_id=template("studio-ivory").pk)
    assert response.status_code == 202, response.json()
    job_id = response.json()["id"]
    again = start(staff_client, product, image, kind="studio", template_id=template("studio-white").pk)
    assert again.status_code == 200 and again.json()["id"] == job_id  # one active job per image
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded" and body["outcome"] == "pending" and body["error"] is None
    result = body["result"]
    assert result["url"].endswith(".webp") and result["overlay_url"] is None
    assert result["crops"]["portrait"] == [0.0, 0.0, 1.0, 1.0] and len(result["crops"]["square"]) == 4
    assert body["template"]["key"] == "studio-ivory"
    accepted = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {"make_main": True}, format="json")
    assert accepted.status_code == 200, accepted.json()
    new = accepted.json()["image"]
    assert new["source"] == "studio" and new["derived_from_id"] == image.pk and new["position"] == 0
    assert new["renditions"]["portrait"] is not None and new["renditions"]["square"] is not None
    image.refresh_from_db()
    assert image.position == 1  # the original stays, after the scene
    assert accepted.json()["job"]["outcome"] == "accepted"
    assert AuditEntry.objects.filter(action="assistant.scene_accepted").count() == 1
    twice = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json")
    assert twice.status_code == 409 and twice.json()["code"] == "scene_job_not_ready"


def test_ai_scene_needs_its_own_switch(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    marble = template("marble")
    refused = start(staff_client, product, image, kind="ai_scene", template_id=marble.pk)
    assert refused.status_code == 409 and refused.json()["code"] == "scene_ai_disabled"
    scenes_on.ai_enabled = True
    scenes_on.save()
    job_id = start(staff_client, product, image, kind="ai_scene", template_id=marble.pk).json()["id"]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    job = SceneJob.objects.get(pk=job_id)
    assert job.usage["image_calls"] >= 1 and job.cost_usd > 0
    assert marble.prompt in fake_image_model().calls[-1].prompt


def test_a_cut_off_product_is_refused_with_a_message(
    staff_client: APIClient, scenes_on: SceneSettings
) -> None:
    product, image = saved_image(box=(0, 300, 400, 1000))
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["result"] is None
    assert body["error"]["code"] == "scene_product_cut_off" and "to‘liq" in body["error"]["message"]


def test_fit_proposes_crops_and_accept_sets_renditions(
    staff_client: APIClient, scenes_on: SceneSettings
) -> None:
    product, image = saved_image()
    job_id = start(staff_client, product, image, kind="fit").json()["id"]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded" and body["result"]["url"] is None
    box = body["result"]["product_box"]
    assert box[0] < 0.4 and box[2] > 0.6
    accepted = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json").json()
    assert accepted["image"]["id"] == image.pk
    assert accepted["image"]["renditions"]["square"]["mode"] == "auto"
    assert accepted["image"]["product_box"] == box


def test_generated_parts_must_be_acknowledged(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    scene_services.run(job_id)
    SceneJob.objects.filter(pk=job_id).update(generated_inside_pct=3)
    refused = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json")
    assert refused.status_code == 400
    ok = staff_client.post(
        f"{ADMIN}scenes/jobs/{job_id}/accept/", {"acknowledge_generated": True}, format="json"
    )
    assert ok.status_code == 200


def test_discard_and_a_changed_source(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    scene_services.run(job_id)
    discarded = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/discard/", format="json")
    assert discarded.status_code == 200 and discarded.json()["outcome"] == "discarded"
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    scene_services.run(job_id)
    ProductImage.objects.filter(pk=image.pk).update(sha256="0" * 64)
    changed = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json")
    assert changed.status_code == 409 and changed.json()["code"] == "scene_source_changed"


def test_templates_must_match_and_be_enabled(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    wrong_kind = start(staff_client, product, image, kind="studio", template_id=template("marble").pk)
    assert wrong_kind.status_code == 400
    SceneTemplate.objects.filter(key="studio-white").update(enabled=False)
    disabled = start(staff_client, product, image, kind="studio", template_id=template("studio-white").pk)
    assert disabled.status_code == 400
    other = ProductFactory.create(status="hidden")
    assert start(staff_client, other, image, kind="fit").status_code == 404


def test_daily_limit_and_budget(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    scenes_on.per_user_daily_jobs = 0
    scenes_on.save()
    assert start(staff_client, product, image, kind="fit").status_code == 429
    scenes_on.per_user_daily_jobs = 5
    scenes_on.daily_budget_usd = 0
    scenes_on.save()
    response = start(staff_client, product, image, kind="fit")
    assert response.status_code == 409 and response.json()["code"] == "assistant_budget_exceeded"


def test_housekeeping_reaps_and_expires(staff_client: APIClient, scenes_on: SceneSettings) -> None:
    product, image = saved_image()
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    SceneJob.objects.filter(pk=job_id).update(created_at=timeutils.now() - timedelta(minutes=30))
    assert tasks.scenes_housekeeping() == {"reaped": 1, "expired": 0}
    assert SceneJob.objects.get(pk=job_id).error_code == "worker_lost"
    job_id = start(
        staff_client, product, image, kind="studio", template_id=template("studio-white").pk
    ).json()["id"]
    scene_services.run(job_id)
    SceneJob.objects.filter(pk=job_id).update(expires_at=timeutils.now() - timedelta(minutes=1))
    assert tasks.scenes_housekeeping() == {"reaped": 0, "expired": 1}
    body = staff_client.get(f"{ADMIN}scenes/jobs/{job_id}/").json()
    assert body["outcome"] == "expired" and body["result"] is None


def test_the_task_runs_a_job_and_survives_a_crash(
    scenes_on: SceneSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    product, image = saved_image()
    from glowderma.accounts.models import StaffUser

    actor = StaffUser.objects.first() or StaffUser.objects.create(email="x@example.uz", full_name="X")
    job = scene_services.start_job(product.pk, image.pk, kind="fit", template_id=None, actor=actor).job
    tasks.run_scene(job.pk)
    assert SceneJob.objects.get(pk=job.pk).state == enums.JobState.SUCCEEDED
    job = scene_services.start_job(
        product.pk, image.pk, kind="studio", template_id=template("studio-white").pk, actor=actor
    ).job

    def boom(job_id: int) -> None:
        scene_services.claim(job_id)
        raise RuntimeError("boom")

    monkeypatch.setattr(scene_services, "run", boom)
    tasks.run_scene(job.pk)
    assert SceneJob.objects.get(pk=job.pk).error_code == "server_error"
