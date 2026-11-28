"""Scene templates in the admin panel (contract §2.20.9): references (the scene alone), the editor's create,
edit and hide, «Rasmini yaratish», and how a reference reaches the image models."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from glowderma.assistant.models import SceneJob, SceneSettings, SceneTemplate
from glowderma.assistant.scenes import pipeline, prompts
from glowderma.assistant.scenes import services as scene_services
from glowderma.assistant.scenes.templates import Template
from glowderma.assistant.tests.test_scene_jobs import ADMIN, run_and_poll, saved_image, start, template
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm.ports import GenerationResult
from glowderma.integrations.llm.registry import fake_content_model, fake_image_model
from glowderma.integrations.segmentation.registry import fake_segmenter

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes")]


@pytest.fixture(autouse=True)
def media_root(tmp_path: Path) -> Iterator[None]:
    with override_settings(MEDIA_ROOT=tmp_path / "media"):
        for fake in (fake_segmenter(), fake_image_model(), fake_content_model()):
            fake.reset()
        yield
        for fake in (fake_segmenter(), fake_image_model(), fake_content_model()):
            fake.reset()


@pytest.fixture
def ai_on() -> SceneSettings:
    row = SceneSettings.objects.get()
    row.enabled = row.ai_enabled = True
    row.save()
    return row


def scene_photo(colour: tuple[int, int, int] = (214, 200, 182)) -> SimpleUploadedFile:
    picture = Image.new("RGB", (800, 1000), colour)
    output = BytesIO()
    picture.save(output, format="PNG")
    return SimpleUploadedFile("scene.png", output.getvalue(), content_type="image/png")


def described(
    scene: str = "A white marble ledge, a beige wall, soft side light.", *, flag: str | None = None
) -> None:
    flags = {flag: True} if flag else {}
    data = {"scene": scene, "has_product": False, "has_people": False, "has_text": False, **flags}
    fake_content_model().push(GenerationResult(data=data, input_tokens=300, output_tokens=60, model="a"))


def translated(scene: str) -> None:
    fake_content_model().push(
        GenerationResult(
            data={"scene": scene, "refused": False, "reason": ""},
            input_tokens=50,
            output_tokens=20,
            model="t",
        )
    )


def templates(client: APIClient) -> list[dict[str, Any]]:
    body: list[dict[str, Any]] = client.get(f"{ADMIN}scenes/templates/").json()["results"]
    return body


# --- the cards show the scene alone -------------------------------------------------------------------------


def test_studio_cards_are_redrawn_without_the_sample_product(staff_client: APIClient) -> None:
    ivory = template("studio-ivory")
    first = templates(staff_client)
    ivory.refresh_from_db()
    assert ivory.preview_scene_only and ivory.preview
    card = next(row for row in first if row["key"] == "studio-ivory")
    assert card["preview_url"].endswith(".webp") and card["reference_url"] is None
    with ivory.preview.open("rb") as handle, Image.open(handle) as picture:
        assert picture.size == (600, 750) and len(set(picture.getdata())) < 4000  # a background, no bottle
    name = ivory.preview.name
    templates(staff_client)  # idempotent: drawn once
    ivory.refresh_from_db()
    assert ivory.preview.name == name
    marble = next(row for row in first if row["key"] == "marble")
    assert marble["text"] == template("marble").prompt and marble["names"]["ru"] == "Мрамор"


# --- create, edit, hide -------------------------------------------------------------------------------------


def test_a_template_from_a_photo_of_a_table(staff_client: APIClient, ai_on: SceneSettings) -> None:
    described()
    response = staff_client.post(
        f"{ADMIN}scenes/templates/",
        {"name.uz": "Stol", "name.ru": "Стол", "reference": scene_photo()},
        format="multipart",
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body["kind"] == "ai_scene" and body["reference_source"] == "upload"
    assert body["preview_url"] == body["reference_url"] and body["saved_by_staff"] is True
    row = SceneTemplate.objects.get(pk=body["id"])
    assert row.reference_description.startswith("A white marble ledge") and row.prompt == ""
    sent = fake_content_model().calls[0]
    assert sent.response_schema == prompts.REFERENCE_DESCRIBE_SCHEMA and len(sent.images) == 1
    assert AuditEntry.objects.filter(action="assistant.scene_template_created", entity_id=row.key).exists()
    assert body["id"] in [card["id"] for card in templates(staff_client)]


@pytest.mark.parametrize(
    ("flag", "words"),
    [("has_product", "mahsulot bor"), ("has_people", "odam"), ("has_text", "yozuv")],
)
def test_a_reference_with_a_product_a_person_or_text_is_refused(
    staff_client: APIClient, ai_on: SceneSettings, flag: str, words: str
) -> None:
    described(flag=flag)
    response = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
    )
    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["field"] == "reference" and words in error["message"]
    assert not SceneTemplate.objects.filter(name_uz="Stol").exists()


def test_a_template_from_words_in_any_language(staff_client: APIClient, ai_on: SceneSettings) -> None:
    translated("Pink silk folds, warm morning light.")
    response = staff_client.post(
        f"{ADMIN}scenes/templates/",
        {"name": {"uz": "Ipak"}, "text": "pushti ipak, iliq tong yorug‘i"},
        format="json",
    )
    assert response.status_code == 201, response.json()
    row = SceneTemplate.objects.get(pk=response.json()["id"])
    assert row.prompt == "Pink silk folds, warm morning light."
    assert row.prompt_original == "pushti ipak, iliq tong yorug‘i"
    assert (
        response.json()["text"] == "pushti ipak, iliq tong yorug‘i" and response.json()["preview_url"] is None
    )


def test_create_needs_a_name_and_a_scene(staff_client: APIClient, ai_on: SceneSettings) -> None:
    neither = staff_client.post(f"{ADMIN}scenes/templates/", {"name": {"uz": "Bo‘sh"}}, format="json")
    assert neither.status_code == 400 and neither.json()["errors"][0]["field"] == "text"
    nameless = staff_client.post(f"{ADMIN}scenes/templates/", {"text": "marble"}, format="json")
    assert nameless.status_code == 400
    fake_content_model().push(
        GenerationResult(
            data={"scene": "", "refused": True, "reason": "people"},
            input_tokens=1,
            output_tokens=1,
            model="t",
        )
    )
    refused = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name": {"uz": "Qiz"}, "text": "qiz krem tutib turibdi"}, format="json"
    )
    assert refused.status_code == 400 and "odamlar" in refused.json()["errors"][0]["message"]
    ai_on.ai_enabled = False
    ai_on.save()
    off = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name": {"uz": "X"}, "text": "marble"}, format="json"
    )
    assert off.status_code == 409 and off.json()["code"] == "scene_ai_disabled"


def test_edit_names_text_reference_and_hide(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble")
    renamed = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/",
        {"name": {"uz": "Oq marmar", "ru": "Белый мрамор", "en": "White marble"}},
        format="json",
    )
    assert renamed.status_code == 200 and renamed.json()["names"]["uz"] == "Oq marmar"
    assert fake_content_model().calls == []  # names alone: no AI call
    described("Grey stone, cool light.")
    replaced = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo()}, format="multipart"
    )
    assert replaced.status_code == 200 and replaced.json()["reference_source"] == "upload"
    marble.refresh_from_db()
    assert marble.reference_description == "Grey stone, cool light." and not marble.preview
    translated("Black slate, a single orchid.")
    retexted = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "qora shifer, bitta orxideya"}, format="json"
    )
    assert retexted.status_code == 200
    marble.refresh_from_db()
    assert marble.prompt == "Black slate, a single orchid." and marble.reference  # an upload stays
    removed = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"remove_reference": True}, format="json"
    )
    assert removed.status_code == 200 and removed.json()["reference_url"] is None
    hidden = staff_client.patch(f"{ADMIN}scenes/templates/{marble.pk}/", {"enabled": False}, format="json")
    assert hidden.status_code == 200
    assert marble.pk not in [card["id"] for card in templates(staff_client)]
    studio = staff_client.patch(
        f"{ADMIN}scenes/templates/{template('studio-ivory').pk}/", {"text": "x"}, format="json"
    )
    assert studio.status_code == 400
    assert (
        AuditEntry.objects.filter(action="assistant.scene_template_updated", entity_id="marble").count() >= 4
    )


# --- «Rasmini yaratish» -------------------------------------------------------------------------------------


def test_a_reference_is_made_from_the_text(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble")
    response = staff_client.post(f"{ADMIN}scenes/templates/{marble.pk}/reference/", format="json")
    assert response.status_code == 202 and response.json()["kind"] == "preview"
    job_id = response.json()["id"]
    again = staff_client.post(f"{ADMIN}scenes/templates/{marble.pk}/reference/", format="json")
    assert again.json()["id"] == job_id  # one pending per template
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    marble.refresh_from_db()
    assert marble.reference_source == "generated" and not marble.preview
    with marble.reference.open("rb") as handle, Image.open(handle) as picture:
        assert picture.size == (960, 1200)
    request = fake_image_model().calls[-1]
    assert request.images == () and request.aspect_ratio == "4:5" and "No product" in request.prompt
    # a new text: the picture of the old one goes
    translated("A wet black slate ledge.")
    staff_client.patch(f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "ho‘l qora shifer"}, format="json")
    marble.refresh_from_db()
    assert not marble.reference and marble.reference_source == ""


def test_a_reference_needs_words_to_be_made(staff_client: APIClient, ai_on: SceneSettings) -> None:
    described()
    created = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
    ).json()
    response = staff_client.post(f"{ADMIN}scenes/templates/{created['id']}/reference/", format="json")
    assert response.status_code == 409 and response.json()["code"] == "scene_template_needs_text"


# --- the reference in a scene -------------------------------------------------------------------------------


def test_an_ai_scene_shows_gemini_the_reference(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble")
    described("Grey stone, cool light.")
    staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo()}, format="multipart"
    )
    marble.refresh_from_db()
    product, image = saved_image()
    job_id = start(staff_client, product, image, kind="ai_scene", template_id=marble.pk).json()["id"]
    job = SceneJob.objects.get(pk=job_id)
    assert job.template_snapshot["reference"] == marble.reference.name
    # replaced while the job waits: the file the job reads stays
    old = str(marble.reference.name)
    described("Another stone.")
    staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo((90, 90, 90))}, format="multipart"
    )
    assert marble.reference.storage.exists(old)
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    request = fake_image_model().calls[-1]
    assert len(request.images) == 2 and "REFERENCE photo of the scene alone" in request.prompt
    assert "Grey stone, cool light." in request.prompt


def test_bria_follows_a_reference_by_its_description() -> None:
    with_both = Template(
        "t", "ai_scene", {}, {}, "Marble.", reference=b"x", reference_description="Grey stone."
    )
    assert with_both.scene == "Marble. Grey stone."
    only_picture = Template("t", "ai_scene", {}, {}, "", reference=b"x", reference_description="Grey stone.")
    assert prompts.bria_scene(only_picture.scene, "").startswith("Grey stone")
    assert pipeline.FALLBACK_SCENE and Template("t", "ai_scene", {}).scene == ""


def test_a_saved_typed_scene_gets_a_picture_of_its_scene_alone(
    staff_client: APIClient, ai_on: SceneSettings
) -> None:
    product, image = saved_image()
    translated("A white marble surface with rose petals.")
    job_id = start(staff_client, product, image, kind="ai_scene", custom_prompt="atirgul gulbargi").json()[
        "id"
    ]
    run_and_poll(staff_client, job_id)
    saved = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/template/", {"name": "Atirgul"}, format="json")
    assert saved.status_code == 201, saved.json()
    row = SceneTemplate.objects.get(pk=saved.json()["id"])
    assert not row.preview and row.prompt_original == "atirgul gulbargi"
    reference_job = SceneJob.objects.get(kind=enums.SceneJobKind.PREVIEW, template=row)
    scene_services.run(reference_job.pk)
    row.refresh_from_db()
    assert row.reference_source == "generated"
