"""Scene templates in the admin panel (contract §2.20.9): references (the scene alone), the editor's create,
edit and hide, «Rasmini yaratish», and how a reference reaches the image models."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from glowderma.assistant.models import SceneJob, SceneSettings, SceneTemplate
from glowderma.assistant.scenes import pipeline, prompts
from glowderma.assistant.scenes import services as scene_services
from glowderma.assistant.scenes.templates import Template
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


def checked(client: APIClient, response: Any) -> dict[str, Any]:
    """A save the AI checks: the request makes no AI call and answers with the job; the worker runs it."""
    assert response.status_code == 202, response.json()
    job = response.json()
    assert job["kind"] == "template" and job["state"] == "queued" and job["result"] is None
    return run_and_poll(client, job["id"])


def saved(client: APIClient, response: Any) -> dict[str, Any]:
    """The template a checked save wrote, as the templates list shows it."""
    body = checked(client, response)
    assert body["state"] == "succeeded", body
    template_id = body["template"]["saved_template_id"]
    row = SceneTemplate.objects.get(pk=template_id)
    card = next((card for card in templates(client) if card["id"] == template_id), None)
    assert card is not None or not row.enabled
    return card or {"id": template_id}


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
    assert fake_content_model().calls == []  # the API reads no AI key: the worker asks the analyst
    body = saved(staff_client, response)
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
    staff_client: APIClient,
    ai_on: SceneSettings,
    flag: str,
    words: str,
    django_capture_on_commit_callbacks: Any,
) -> None:
    described(flag=flag)
    response = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
    )
    upload = str(SceneJob.objects.get(pk=response.json()["id"]).candidate.name)
    with django_capture_on_commit_callbacks(execute=True):
        body = checked(staff_client, response)
    assert body["state"] == "failed" and body["error"]["code"] == f"template_reference_{flag[4:]}"
    assert words in body["error"]["message"]
    assert not SceneTemplate.objects.filter(name_uz="Stol").exists()
    job = SceneJob.objects.get(pk=body["id"])
    assert not job.candidate and not job.candidate.storage.exists(upload)  # the refused picture is gone


def test_a_template_from_words_in_any_language(staff_client: APIClient, ai_on: SceneSettings) -> None:
    translated("Pink silk folds, warm morning light.")
    response = staff_client.post(
        f"{ADMIN}scenes/templates/",
        {"name": {"uz": "Ipak"}, "text": "pushti ipak, iliq tong yorug‘i"},
        format="json",
    )
    card = saved(staff_client, response)
    row = SceneTemplate.objects.get(pk=card["id"])
    assert row.prompt == "Pink silk folds, warm morning light."
    assert row.prompt_original == "pushti ipak, iliq tong yorug‘i"
    assert card["text"] == "pushti ipak, iliq tong yorug‘i" and card["preview_url"] is None


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
    refused = checked(
        staff_client,
        staff_client.post(
            f"{ADMIN}scenes/templates/",
            {"name": {"uz": "Qiz"}, "text": "qiz krem tutib turibdi"},
            format="json",
        ),
    )
    assert refused["state"] == "failed" and refused["error"]["code"] == "scene_custom_refused"
    assert "odamlar" in refused["error"]["message"]
    assert not SceneTemplate.objects.filter(name_uz="Qiz").exists()
    unreadable = staff_client.post(
        f"{ADMIN}scenes/templates/",
        {"name.uz": "Buzuq", "reference": SimpleUploadedFile("x.png", b"not a picture", "image/png")},
        format="multipart",
    )
    assert unreadable.status_code == 400 and unreadable.json()["errors"][0]["field"] == "reference"
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
    replaced = saved(
        staff_client,
        staff_client.patch(
            f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo()}, format="multipart"
        ),
    )
    assert replaced["reference_source"] == "upload"
    marble.refresh_from_db()
    assert marble.reference_description == "Grey stone, cool light." and not marble.preview
    translated("Black slate, a single orchid.")
    saved(
        staff_client,
        staff_client.patch(
            f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "qora shifer, bitta orxideya"}, format="json"
        ),
    )
    unchanged = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "qora shifer,  bitta orxideya"}, format="json"
    )
    assert unchanged.status_code == 200  # the same words: nothing for the AI
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
    saved(
        staff_client,
        staff_client.patch(
            f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "ho‘l qora shifer"}, format="json"
        ),
    )
    marble.refresh_from_db()
    assert not marble.reference and marble.reference_source == ""


def test_a_reference_needs_words_to_be_made(staff_client: APIClient, ai_on: SceneSettings) -> None:
    described()
    created = saved(
        staff_client,
        staff_client.post(
            f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
        ),
    )
    response = staff_client.post(f"{ADMIN}scenes/templates/{created['id']}/reference/", format="json")
    assert response.status_code == 409 and response.json()["code"] == "scene_template_needs_text"


# --- the reference in a scene -------------------------------------------------------------------------------


def test_an_ai_scene_shows_gemini_the_reference(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble")
    described("Grey stone, cool light.")
    saved(
        staff_client,
        staff_client.patch(
            f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo()}, format="multipart"
        ),
    )
    marble.refresh_from_db()
    product, image = saved_image()
    job_id = start(staff_client, product, image, kind="ai_scene", template_id=marble.pk).json()["id"]
    job = SceneJob.objects.get(pk=job_id)
    assert job.template_snapshot["reference"] == marble.reference.name
    # replaced while the job waits: the file the job reads stays
    old = str(marble.reference.name)
    described("Another stone.")
    replacing = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"reference": scene_photo((90, 90, 90))}, format="multipart"
    )
    scene_services.run(replacing.json()["id"])  # a template save does not wait for the queued scene
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


# --- a save the AI checks runs on the worker ---------------------------------------------------------------


def test_one_checked_save_of_a_template_at_a_time(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble")
    first = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "kulrang tosh"}, format="json"
    )
    assert first.status_code == 202
    second = staff_client.patch(f"{ADMIN}scenes/templates/{marble.pk}/", {"text": "oq tosh"}, format="json")
    assert second.status_code == 409 and second.json()["code"] == "scene_template_saving"
    renamed = staff_client.patch(
        f"{ADMIN}scenes/templates/{marble.pk}/", {"name": {"uz": "Tosh", "ru": "", "en": ""}}, format="json"
    )
    assert renamed.status_code == 200  # names need no AI: saved at once
    translated("Grey stone.")
    body = run_and_poll(staff_client, first.json()["id"])
    assert body["state"] == "succeeded" and body["template"]["saved_template_id"] == marble.pk
    marble.refresh_from_db()
    assert marble.prompt == "Grey stone." and marble.name_uz == "Tosh"  # the other edit is kept


def test_a_checked_save_runs_beside_a_scene_and_fails_cleanly(
    staff_client: APIClient, ai_on: SceneSettings, django_capture_on_commit_callbacks: Any
) -> None:
    product, image = saved_image()
    scene_id = start(staff_client, product, image, kind="ai_scene", template_id=template("marble").pk).json()[
        "id"
    ]
    scene_services.claim(scene_id)  # a scene is running
    response = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
    )
    job_id = response.json()["id"]
    upload = str(SceneJob.objects.get(pk=job_id).candidate.name)
    # the AI does not answer: the job fails with its message, nothing is saved and the picture goes
    fake_content_model().push(LLMUnavailable("down"))
    with django_capture_on_commit_callbacks(execute=True):
        body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["error"]["code"] == "provider_unavailable", body
    assert body["error"]["message"]
    job = SceneJob.objects.get(pk=job_id)
    assert not job.candidate and not job.candidate.storage.exists(upload)
    assert not SceneTemplate.objects.filter(name_uz="Stol").exists()


def test_a_lost_checked_save_leaves_no_picture(
    staff_client: APIClient, ai_on: SceneSettings, django_capture_on_commit_callbacks: Any
) -> None:
    response = staff_client.post(
        f"{ADMIN}scenes/templates/", {"name.uz": "Stol", "reference": scene_photo()}, format="multipart"
    )
    job = SceneJob.objects.get(pk=response.json()["id"])
    upload = str(job.candidate.name)
    SceneJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(hours=1))
    with django_capture_on_commit_callbacks(execute=True):
        assert scene_services.reap_stale() == [job.pk]
    job.refresh_from_db()
    assert job.error_code == "worker_lost" and not job.candidate and not job.candidate.storage.exists(upload)
