"""Banner images (contract §2.20.8): layout, pipeline with fakes, API and using one for a banner."""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.test import override_settings
from PIL import Image
from rest_framework.test import APIClient

from glowderma.assistant.models import SceneJob, SceneSettings
from glowderma.assistant.scenes import banner, pipeline, prompts
from glowderma.assistant.scenes import services as scene_services
from glowderma.assistant.scenes.templates import Template
from glowderma.assistant.tests.test_scene_jobs import ADMIN, photo_bytes, saved_image, template
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Banner, Product
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import compose, enums
from glowderma.core.media import variant_name
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm.ports import GenerationResult
from glowderma.integrations.llm.registry import fake_content_model, fake_image_model
from glowderma.integrations.secret import Secret
from glowderma.integrations.segmentation.registry import fake_segmenter

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes")]

SAMPLE = Path(scene_services.__file__).with_name("assets") / "sample-product.webp"


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
def ai_on() -> SceneSettings:
    row = SceneSettings.objects.get()
    row.enabled = row.ai_enabled = True
    row.save()
    return row


def sample_cutout(product_id: int = 1) -> banner.Cutout:
    with Image.open(SAMPLE) as source:
        analysis = pipeline.analysis_of_cutout(source.convert("RGBA"))
    return banner.Cutout(product_id=product_id, photo=analysis.photo, mask=analysis.mask, box=analysis.box)


def fake_providers() -> pipeline.Providers:
    return pipeline.Providers(
        segmenter=fake_segmenter(),
        image_model=fake_image_model(),
        content_model=fake_content_model(),
        api_key=Secret("k"),
    )


MARBLE = Template(key="marble", kind="ai_scene", name={}, params={}, prompt="White marble, soft light.")


# --- layout -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boxes",
    [
        [(0, 0, 200, 600)],  # a tall bottle
        [(0, 0, 600, 300)],  # a wide jar
        [(0, 0, 200, 600), (0, 0, 300, 300)],
        [(0, 0, 200, 600), (0, 0, 220, 500), (0, 0, 900, 400)],
        [(0, 0, 40, 60)] * 3,  # small photos: never enlarged past 1.5x
    ],
)
def test_products_stand_on_the_right_inside_every_screen_and_never_overlap(
    boxes: list[compose.Box],
) -> None:
    size = banner.canvas_size()
    assert size == (2333, 1000)
    placed = banner.plan(boxes, size)
    web = banner.web_window(size)
    master = banner.master_window(size)
    middle = web[0] + (web[2] - web[0]) * 0.5
    for target, box in zip(placed, boxes, strict=True):
        assert web[0] <= target[0] and target[2] <= web[2]  # the website (2:1) shows every product
        assert master[0] <= web[0] and web[2] <= master[2]  # and the app (2.15:1) shows more
        assert target[0] >= middle  # the title's half stays free
        assert target[3] == round(banner.BASELINE * size[1])  # one baseline
        assert (target[3] - target[1]) / (box[3] - box[1]) <= banner.MAX_UPSCALE + 0.01
    ordered = sorted(placed)
    assert all(left[2] <= right[0] for left, right in zip(ordered, ordered[1:], strict=False))
    if len(boxes) > 1:  # the first product is the main one: the tallest
        assert placed[0][3] - placed[0][1] >= max(box[3] - box[1] for box in placed[1:]) or boxes[0] == (
            0,
            0,
            40,
            60,
        )


def test_the_master_is_the_apps_ratio_and_the_website_its_middle() -> None:
    assert banner.master_window((2333, 1000)) == (91, 0, 2241, 1000)
    width, height = 2150, 1000
    assert width / height == banner.MASTER_RATIO
    assert banner.web_window((2150, 1000)) == (75, 0, 2075, 1000)


# --- pipeline (fakes) ---------------------------------------------------------------------------------------


def test_products_banner_keeps_the_products_pixels_and_is_exactly_2_15() -> None:
    usage = pipeline.Usage()
    cutouts = [sample_cutout(1), sample_cutout(2)]
    result = banner.with_products(cutouts, MARBLE, fake_providers(), pipeline.Config(), usage)
    assert result.final.size == (2150, 1000)
    request = fake_image_model().calls[0]
    assert request.aspect_ratio == "21:9" and "THE PRODUCTS ARE LOCKED" in request.prompt
    assert "SCENE: White marble, soft light." in request.prompt and "banner" not in request.prompt.lower()
    with Image.open(BytesIO(request.images[0].data)) as sent:
        assert sent.size == (2333, 1000)
    web = banner.web_window(result.final.size)
    for box in result.product_boxes:
        assert web[0] <= box[0] and box[2] <= web[2]
    # the products' own pixels, untouched (the pipeline raises otherwise; checked again here)
    size = banner.canvas_size()
    placed, _ = banner.place_products(cutouts, size)
    whole = Image.new("RGB", size)
    whole.paste(result.final, (banner.master_window(size)[0], 0))
    core = compose.binary(placed.core.crop(banner.master_window(size)), 250)
    final_core = Image.composite(result.final, Image.new("RGB", result.final.size), core)
    expected = Image.composite(
        placed.product.crop(banner.master_window(size)), Image.new("RGB", result.final.size), core
    )
    assert final_core.tobytes() == expected.tobytes()
    assert usage.image_calls == 1 and usage.text_calls == 1  # the scene and the QA check


def test_a_model_that_moves_a_product_is_retried_then_refused() -> None:
    drifting = Template(key="x", kind="ai_scene", name={}, params={}, prompt="Marble [fake-drift]")
    with pytest.raises(pipeline.SceneError) as caught:
        banner.with_products(
            [sample_cutout()], drifting, fake_providers(), pipeline.Config(max_attempts=2), pipeline.Usage()
        )
    assert caught.value.code == pipeline.PRODUCT_DRIFT and len(fake_image_model().calls) == 2
    assert "previous attempt moved" in fake_image_model().calls[1].prompt


def test_a_scene_alone_is_made_wide_and_cropped_to_2_15() -> None:
    usage = pipeline.Usage()
    result = banner.scene_only(MARBLE, fake_providers(), pipeline.Config(), usage)
    width, height = result.final.size
    assert abs(width / height - banner.MASTER_RATIO) < 0.01 and height == 576  # the model's own pixels
    request = fake_image_model().calls[0]
    assert request.images == () and request.aspect_ratio == "21:9"
    assert "no product" in request.prompt and "lower left" in request.prompt


def test_the_banner_qa_check_rejects_text_in_the_image() -> None:
    lettered = {name: False for name in prompts.BANNER_QA_FIELDS} | {"text_or_letters": True}
    for _ in range(2):
        fake_content_model().push(
            GenerationResult(data=lettered, input_tokens=10, output_tokens=5, model="q")
        )
    with pytest.raises(pipeline.SceneError) as caught:
        banner.scene_only(MARBLE, fake_providers(), pipeline.Config(max_attempts=2), pipeline.Usage())
    assert caught.value.code == pipeline.RESULT_REJECTED
    assert fake_content_model().calls[0].response_schema == prompts.BANNER_QA_SCHEMA


def test_bria_gets_plain_scenes_for_banners() -> None:
    words = prompts.bria_banner("Silk — warm light «soft».", prompts.DEFAULT_STYLE, with_products=False)
    assert "no bottle" in words and "—" not in words and "«" not in words
    assert len(words.split()) <= prompts.BRIA_MAX_WORDS
    assert "products" in prompts.bria_banner("Silk.", "", with_products=True)


# --- API ----------------------------------------------------------------------------------------------------


def start(client: APIClient, **body: Any) -> Any:
    return client.post(f"{ADMIN}scenes/banner-images/", body, format="json")


def run_and_poll(client: APIClient, job_id: int) -> dict[str, Any]:
    scene_services.run(job_id)
    body: dict[str, Any] = client.get(f"{ADMIN}scenes/jobs/{job_id}/").json()
    return body


def named_product(name: str) -> Product:
    product, _ = saved_image()
    Product.objects.filter(pk=product.pk).update(name_uz=name)
    product.refresh_from_db()
    return product


def test_a_banner_with_products_is_made_and_used_for_a_new_banner(
    staff_client: APIClient, ai_on: SceneSettings
) -> None:
    serum, cream = named_product("Serum"), named_product("Krem")
    response = start(
        staff_client, mode="products", product_ids=[serum.pk, cream.pk], template_id=template("marble").pk
    )
    assert response.status_code == 202, response.json()
    job_id = response.json()["id"]
    assert response.json()["kind"] == "banner"
    again = start(staff_client, mode="scene", template_id=template("marble").pk)
    assert again.status_code == 200 and again.json()["id"] == job_id  # one banner image at a time
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    assert body["banner"] == {
        "mode": "products",
        "products": [{"id": serum.pk, "name": "Serum"}, {"id": cream.pk, "name": "Krem"}],
        "failed_product": None,
        "web_crop": [0.03488, 0.0, 0.93023, 1.0],
        "accepted_banner_id": None,
    }
    assert body["result"]["url"].endswith(".webp")
    job = SceneJob.objects.get(pk=job_id)
    assert job.cost_usd > 0 and job.usage["image_calls"] >= 1
    used = staff_client.post(
        f"{ADMIN}scenes/jobs/{job_id}/banner/",
        {
            "banner": {
                "title": {"uz": "Yangi kolleksiya"},
                "link": {"kind": "product", "product_id": serum.pk},
            }
        },
        format="json",
    )
    assert used.status_code == 400  # the linked product is hidden: the banner's own checks apply
    Product.objects.filter(pk=serum.pk).update(status=enums.ProductStatus.ACTIVE)
    used = staff_client.post(
        f"{ADMIN}scenes/jobs/{job_id}/banner/",
        {
            "banner": {
                "title": {"uz": "Yangi kolleksiya"},
                "link": {"kind": "product", "product_id": serum.pk},
            }
        },
        format="json",
    )
    assert used.status_code == 200, used.json()
    saved = Banner.objects.get(pk=used.json()["banner"]["id"])
    assert saved.title_uz == "Yangi kolleksiya" and saved.link_product_id == serum.pk
    assert round(saved.image_width / saved.image_height, 2) == 2.15  # exactly the app's banner
    assert saved.image.storage.exists(variant_name(str(saved.image.name), "medium"))  # the copy the app loads
    assert used.json()["job"]["outcome"] == "accepted"
    assert used.json()["job"]["banner"]["accepted_banner_id"] == saved.pk
    assert AuditEntry.objects.filter(action="assistant.banner_image_used", entity_id=str(saved.pk)).exists()
    twice = staff_client.post(
        f"{ADMIN}scenes/jobs/{job_id}/banner/", {"banner": {"title": {"uz": "Yana"}}}, format="json"
    )
    assert twice.status_code == 409 and twice.json()["code"] == "scene_job_not_ready"


def test_a_scene_alone_replaces_an_existing_banners_image(
    staff_client: APIClient, ai_on: SceneSettings
) -> None:
    existing = catalog_services.create_banner(
        catalog_services.BannerInput(title={"uz": "Eski"}, link_kind=enums.BannerLinkKind.NONE),
        photo_bytes(),
        actor=None,
    )
    old_name = existing.image.name
    job_id = start(staff_client, mode="scene", custom_prompt="ipak va iliq yorug‘lik").json()["id"]
    fake_content_model().push(
        GenerationResult(
            data={"scene": "Silk drapes, warm light.", "refused": False, "reason": ""},
            input_tokens=10,
            output_tokens=5,
            model="t",
        )
    )
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "succeeded", body
    assert body["banner"]["mode"] == "scene" and body["template"]["key"] == "custom"
    used = staff_client.post(
        f"{ADMIN}scenes/jobs/{job_id}/banner/",
        {"banner_id": existing.pk, "banner": {"title": {"uz": "Qish"}}},
        format="json",
    )
    assert used.status_code == 200, used.json()
    existing.refresh_from_db()
    assert existing.title_uz == "Qish" and existing.image.name != old_name
    assert abs(existing.image_width / existing.image_height - 2.15) < 0.01
    missing = start(staff_client, mode="scene", template_id=template("marble").pk).json()["id"]
    run_and_poll(staff_client, missing)
    gone = staff_client.post(
        f"{ADMIN}scenes/jobs/{missing}/banner/", {"banner_id": 999999, "banner": {}}, format="json"
    )
    assert gone.status_code == 404


def test_banner_image_requests_are_checked(staff_client: APIClient, ai_on: SceneSettings) -> None:
    marble = template("marble").pk
    products = [named_product(f"P{i}").pk for i in range(4)]
    assert start(staff_client, mode="products", product_ids=[], template_id=marble).status_code == 400
    assert start(staff_client, mode="products", product_ids=products, template_id=marble).status_code == 400
    duplicate = start(staff_client, mode="products", product_ids=[products[0]] * 2, template_id=marble)
    assert duplicate.status_code == 400
    assert start(staff_client, mode="scene", product_ids=products[:1], template_id=marble).status_code == 400
    studio = start(staff_client, mode="scene", template_id=template("studio-ivory").pk)
    assert studio.status_code == 400  # banners are AI scenes
    bare = ProductFactory.create(name_uz="Rasmsiz krem")
    no_photo = start(staff_client, mode="products", product_ids=[bare.pk], template_id=marble)
    assert no_photo.status_code == 400 and "Rasmsiz krem" in str(no_photo.json())
    ai_on.ai_enabled = False
    ai_on.save()
    off = start(staff_client, mode="scene", template_id=marble)
    assert off.status_code == 409 and off.json()["code"] == "scene_ai_disabled"
    assert SceneJob.objects.filter(kind=enums.SceneJobKind.BANNER).count() == 0


def test_a_product_photo_that_cannot_be_used_names_the_product(
    staff_client: APIClient, ai_on: SceneSettings
) -> None:
    good = named_product("Serum")
    cut_off, _ = saved_image(box=(0, 300, 400, 1000))
    Product.objects.filter(pk=cut_off.pk).update(name_uz="Kesilgan krem")
    job_id = start(
        staff_client, mode="products", product_ids=[good.pk, cut_off.pk], template_id=template("marble").pk
    ).json()["id"]
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["error"]["code"] == banner.PRODUCT_PHOTO_UNUSABLE
    assert body["banner"]["failed_product"] == {"id": cut_off.pk, "name": "Kesilgan krem"}
    assert "asosiy rasmini" in body["error"]["message"] and fake_image_model().calls == []


def test_a_product_photo_replaced_meanwhile_fails_the_job(
    staff_client: APIClient, ai_on: SceneSettings
) -> None:
    product = named_product("Serum")
    job_id = start(
        staff_client, mode="products", product_ids=[product.pk], template_id=template("marble").pk
    ).json()["id"]
    first = product.images.order_by("position").first()
    assert first is not None
    catalog_services.delete_image(product.pk, first.pk, actor=None)
    catalog_services.add_image(product.pk, photo_bytes(), actor=None)
    body = run_and_poll(staff_client, job_id)
    assert body["state"] == "failed" and body["error"]["code"] == "scene_source_changed"


def test_a_banner_image_is_not_a_product_scene(staff_client: APIClient, ai_on: SceneSettings) -> None:
    job_id = start(staff_client, mode="scene", template_id=template("marble").pk).json()["id"]
    run_and_poll(staff_client, job_id)
    accepted = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/accept/", {}, format="json")
    assert accepted.status_code == 409 and accepted.json()["code"] == "scene_job_not_ready"
    product, image = saved_image()
    wrong = staff_client.post(
        f"{ADMIN}products/{product.pk}/images/{image.pk}/scenes/",
        {"kind": "banner", "template_id": template("marble").pk},
        format="json",
    )
    assert wrong.status_code == 400
    discarded = staff_client.post(f"{ADMIN}scenes/jobs/{job_id}/discard/", format="json")
    assert discarded.status_code == 200 and discarded.json()["outcome"] == "discarded"


def test_a_templates_props_on_the_left_go_right_for_a_banner() -> None:
    """Live (Bria): «a sprig of eucalyptus … at the far left edge» landed under the title until the scene
    was turned for banners and the layout rule led the description."""
    marble = "A marble surface, a sprig of eucalyptus lying flat at the far left edge."
    assert prompts.banner_scene(marble).endswith("at the far right edge.")
    words = prompts.bria_banner(marble, prompts.DEFAULT_STYLE, with_products=True)
    assert words.startswith("Wide panoramic view. The left half is empty") and "left edge" not in words
    gemini = prompts.banner_scene_prompt(marble, "")
    assert "far right edge" in gemini and "even where the SCENE puts it on the left" in gemini
