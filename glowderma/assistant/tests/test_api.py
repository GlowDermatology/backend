"""Admin API of the assistant (contract §2.19): status, start → outbox → worker → poll, apply, discard."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from rest_framework.test import APIClient

from glowderma.assistant.api.admin.views import AssistantGenerateThrottle
from glowderma.assistant.models import AssistantSettings, GenerationJob
from glowderma.assistant.tests.factories import GenerationJobFactory
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory
from glowderma.core import enums
from glowderma.core.models import AuditEntry
from glowderma.core.throttling import DEFAULT_THROTTLE_RATES
from glowderma.integrations.llm.fake import FakeContentModel
from glowderma.integrations.llm.ports import GenerationResult

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes")]

BASE = "/api/v1/admin/"
STATUS = f"{BASE}assistant/status/"


def jobs_url(product: Product) -> str:
    return f"{BASE}products/{product.pk}/assistant/jobs/"


def job_url(job_id: int, action: str = "") -> str:
    return f"{BASE}assistant/jobs/{job_id}/{action + '/' if action else ''}"


def make_product(**fields: Any) -> Product:
    """A product whose data holds no number but 30: the factory's sequence-numbered POS label
    (``TEST PRODUCT 5 30ML``) would otherwise make a suggested "5%" a supported number in one test order and
    not in another."""
    return ProductFactory.create(
        name_uz="Gialuron serum",
        description_uz="Namlovchi serum, 30 ml.",
        billz_name="GIALURON SERUM 30ML",
        status=enums.ProductStatus.HIDDEN,
        **fields,
    )


def generate(client: APIClient, product: Product, capture: Any, body: dict[str, Any] | None = None) -> Any:
    with capture(execute=True):
        response = client.post(jobs_url(product), body or {}, format="json")
    return response


def test_status(staff_client: APIClient) -> None:
    assert staff_client.get(STATUS).json() == {"enabled": False, "max_images": 3}
    AssistantSettings.objects.update(enabled=True)
    assert staff_client.get(STATUS).json() == {"enabled": True, "max_images": 3}


@pytest.mark.usefixtures("assistant_on")
def test_generate_poll_review_apply(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    product = make_product()
    response = generate(staff_client, product, django_capture_on_commit_callbacks, {"languages": ["ru"]})
    assert response.status_code == 202, response.content
    body = response.json()
    assert body["nothing_to_fill"] is False
    job = body["job"]
    assert job["state"] == "queued" and job["mode"] == "fill_missing" and job["languages"] == ["ru"]
    assert job["result"] is None and job["error"] is None and job["progress"] is None
    # the outbox handler ran the eager Celery task after commit
    polled = staff_client.get(job_url(job["id"])).json()
    assert polled["state"] == "succeeded" and polled["outcome"] == "pending"
    suggestions = polled["result"]["suggestions"]
    assert {s["key"] for s in suggestions} == set(polled["keys"])
    first = next(s for s in suggestions if s["key"] == "texts.ru.name")
    assert first == {
        "key": "texts.ru.name",
        "current": "",
        "suggested": "[fake] texts.ru.name",
        "warnings": [],
        "origin": "ai",
        "checked": True,
        "max_length": 120,
    }
    for secret_field in ("input_tokens", "output_tokens", "cost_usd", "prompt_versions", "model_id"):
        assert secret_field not in polled
    applied = staff_client.post(
        job_url(job["id"], "apply"),
        {"items": [{"key": "texts.ru.name", "value": "Сыворотка"}]},
        format="json",
    )
    assert applied.status_code == 200, applied.content
    result = applied.json()
    assert result["applied"] == ["texts.ru.name"] and result["conflicts"] == []
    assert result["job"]["outcome"] == "applied" and result["job"]["applied_keys"] == ["texts.ru.name"]
    assert result["product"]["texts"]["ru"]["name"] == "Сыворотка"
    assert result["product"]["texts"]["ru"]["description"] == ""


@pytest.mark.usefixtures("assistant_on")
def test_second_generate_returns_the_active_job(staff_client: APIClient) -> None:
    product = make_product()
    first = staff_client.post(jobs_url(product), {}, format="json")  # no on_commit: stays queued
    again = staff_client.post(jobs_url(product), {}, format="json")
    assert first.status_code == 202 and again.status_code == 200
    assert again.json()["job"]["id"] == first.json()["job"]["id"]


@pytest.mark.usefixtures("assistant_on")
def test_idempotent_replay(staff_client: APIClient) -> None:
    product = make_product()
    key = str(uuid.uuid4())
    first = staff_client.post(jobs_url(product), {}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    replay = staff_client.post(jobs_url(product), {}, format="json", HTTP_IDEMPOTENCY_KEY=key)
    assert replay.status_code == 202 and replay.headers["Idempotent-Replayed"] == "true"
    assert replay.json() == first.json()
    assert GenerationJob.objects.count() == 1


@pytest.mark.usefixtures("assistant_on")
def test_nothing_to_fill(staff_client: APIClient) -> None:
    response = staff_client.post(jobs_url(make_product()), {"keys": ["texts.uz.name"]}, format="json")
    assert response.status_code == 200
    assert response.json() == {"nothing_to_fill": True, "job": None}


def test_disabled_is_a_409(staff_client: APIClient) -> None:
    response = staff_client.post(jobs_url(make_product()), {}, format="json", HTTP_ACCEPT_LANGUAGE="ru")
    assert response.status_code == 409
    assert response.json()["code"] == "assistant_disabled"


@pytest.mark.usefixtures("assistant_on")
def test_daily_limit_is_a_429(staff_client: APIClient, staff: Any) -> None:
    AssistantSettings.objects.update(per_user_daily_jobs=1)
    GenerationJobFactory.create(created_by=staff, state=enums.JobState.FAILED, error_code="x")
    response = staff_client.post(jobs_url(make_product()), {}, format="json")
    assert response.status_code == 429 and response.json()["code"] == "assistant_daily_limit"


@pytest.mark.usefixtures("assistant_on")
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"mode": "test"}, "mode"),
        ({"mode": "nope"}, "mode"),
        ({"languages": ["de"]}, "languages.0"),
        ({"keys": ["slug"]}, "keys.0"),
        ({"mode": "improve"}, "keys"),
        ({"mode": "photos"}, "image_ids"),
        ({"goal": "louder"}, "goal"),
    ],
)
def test_invalid_requests(staff_client: APIClient, body: dict[str, Any], field: str) -> None:
    response = staff_client.post(jobs_url(make_product()), body, format="json")
    assert response.status_code == 400, response.content
    assert field in [error["field"] for error in response.json()["errors"]]


@pytest.mark.usefixtures("assistant_on")
def test_unknown_product_is_a_404(staff_client: APIClient) -> None:
    assert staff_client.post(f"{BASE}products/999999/assistant/jobs/", {}, format="json").status_code == 404


@pytest.mark.usefixtures("assistant_on")
def test_failed_job_shows_a_safe_message(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    product = make_product(name_ru="Сыворотка [fake-unavailable]")
    job_id = generate(staff_client, product, django_capture_on_commit_callbacks).json()["job"]["id"]
    polled = staff_client.get(job_url(job_id)).json()
    assert polled["state"] == "failed" and polled["result"] is None
    assert polled["error"] == {
        "code": "provider_unavailable",
        "message": "Tashqi xizmat javob bermadi. Qayta urinib ko‘ring.",
    }
    russian = staff_client.get(job_url(job_id), HTTP_ACCEPT_LANGUAGE="ru").json()
    assert russian["error"]["code"] == "provider_unavailable"
    assert russian["error"]["message"] != polled["error"]["message"]  # rendered in the request language
    assert "fake" not in json.dumps(polled["error"])


@pytest.mark.usefixtures("assistant_on")
def test_apply_conflict_and_validation(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    product = make_product()
    fake_llm.push(
        GenerationResult(
            data={"suggestions": [{"key": "texts.ru.name", "value": "Сыворотка"},
                                  {"key": "texts.en.name", "value": "Serum"}]},
            input_tokens=1, output_tokens=1, model="m",
        )
    )  # fmt: skip
    job_id = generate(staff_client, product, django_capture_on_commit_callbacks).json()["job"]["id"]
    bad = staff_client.post(
        job_url(job_id, "apply"), {"items": [{"key": "texts.ru.name", "value": "5%"}]}, format="json"
    )
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "items.0.value"
    assert staff_client.post(job_url(job_id, "apply"), {"items": []}, format="json").status_code == 400
    Product.objects.filter(pk=product.pk).update(name_en="Edited by hand")
    response = staff_client.post(
        job_url(job_id, "apply"),
        {
            "items": [
                {"key": "texts.ru.name", "value": "Сыворотка"},
                {"key": "texts.en.name", "value": "Serum"},
            ]
        },
        format="json",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] == ["texts.ru.name"] and body["conflicts"] == ["texts.en.name"]
    assert body["job"]["outcome"] == "partial"
    assert body["product"]["texts"]["en"]["name"] == "Edited by hand"
    again = staff_client.post(job_url(job_id, "discard"), {}, format="json")
    assert again.status_code == 409 and again.json()["code"] == "assistant_job_not_ready"


@pytest.mark.usefixtures("assistant_on")
def test_discard(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    job_id = generate(staff_client, make_product(), django_capture_on_commit_callbacks).json()["job"]["id"]
    response = staff_client.post(job_url(job_id, "discard"), {}, format="json")
    assert response.status_code == 200 and response.json()["outcome"] == "discarded"


def test_console_test_jobs_are_not_served(staff_client: APIClient) -> None:
    job = GenerationJobFactory.create(mode=enums.AssistantMode.TEST)
    assert staff_client.get(job_url(job.pk)).status_code == 404
    assert staff_client.post(job_url(job.pk, "discard"), {}, format="json").status_code == 404
    assert staff_client.get(job_url(999_999)).status_code == 404


def test_anonymous_is_refused(anon_client: APIClient) -> None:
    assert anon_client.get(STATUS).status_code == 401


def test_generate_throttle_scope() -> None:
    assert AssistantGenerateThrottle.scope == "assistant_generate"
    assert DEFAULT_THROTTLE_RATES["assistant_generate"] == "30/hour"


@pytest.mark.usefixtures("assistant_on")
def test_generate_with_the_unsaved_draft(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    product = make_product(name_ru="Сыворотка")
    body = {"languages": ["ru"], "draft": {"texts.ru.name": "", "texts.ru.description": "Черновик"}}
    response = generate(staff_client, product, django_capture_on_commit_callbacks, body)
    assert response.status_code == 202, response.content
    keys = response.json()["job"]["keys"]
    assert "texts.ru.name" in keys and "texts.ru.description" not in keys
    polled = staff_client.get(job_url(response.json()["job"]["id"])).json()
    name = next(s for s in polled["result"]["suggestions"] if s["key"] == "texts.ru.name")
    assert name["current"] == ""
    assert "Черновик" in fake_llm.calls[-1].prompt


@pytest.mark.usefixtures("assistant_on")
def test_invalid_drafts(staff_client: APIClient) -> None:
    product = make_product()
    other = ProductImageFactory.create()
    cases: list[tuple[Any, str]] = [
        ({"slug": "x"}, "draft.slug"),
        ({"texts.uz.name": "x" * 121}, "draft.texts.uz.name"),
        ({f"images.{other.pk}.alt.uz": "x"}, f"draft.images.{other.pk}.alt.uz"),
        ({f"texts.uz.k{i}": "" for i in range(151)}, "draft"),
        (["texts.uz.name"], "draft"),
        ({"texts.uz.name": None}, "draft.texts.uz.name"),
    ]
    for draft, field in cases:
        response = staff_client.post(jobs_url(product), {"draft": draft}, format="json")
        assert response.status_code == 400, (field, response.content)
        assert field in [error["field"] for error in response.json()["errors"]], response.json()
    assert not GenerationJob.objects.exists()


@pytest.mark.usefixtures("assistant_on")
def test_accept(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    product = make_product()
    fake_llm.push(
        GenerationResult(
            data={"suggestions": [{"key": "texts.ru.name", "value": "Сыворотка"},
                                  {"key": "texts.en.name", "value": "Serum"}]},
            input_tokens=1, output_tokens=1, model="m",
        )
    )  # fmt: skip
    job_id = generate(staff_client, product, django_capture_on_commit_callbacks).json()["job"]["id"]
    bad = staff_client.post(job_url(job_id, "accept"), {"keys": ["texts.uz.usage"]}, format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "keys.0"
    missing = staff_client.post(job_url(job_id, "accept"), {}, format="json")
    assert missing.status_code == 400 and missing.json()["errors"][0]["field"] == "keys"
    response = staff_client.post(job_url(job_id, "accept"), {"keys": ["texts.ru.name"]}, format="json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert set(body) == {"job"}
    assert body["job"]["outcome"] == "partial" and body["job"]["applied_keys"] == ["texts.ru.name"]
    product.refresh_from_db()
    assert product.name_ru == "" and product.name_en == ""  # the product is never written
    again = staff_client.post(job_url(job_id, "accept"), {"keys": []}, format="json")
    assert again.status_code == 409 and again.json()["code"] == "assistant_job_not_ready"
    assert AuditEntry.objects.filter(action="assistant.suggestions_accepted").count() == 1


@pytest.mark.usefixtures("assistant_on")
def test_accept_none_is_discarded(
    staff_client: APIClient, django_capture_on_commit_callbacks: Any, fake_llm: FakeContentModel
) -> None:
    job_id = generate(staff_client, make_product(), django_capture_on_commit_callbacks).json()["job"]["id"]
    response = staff_client.post(job_url(job_id, "accept"), {"keys": []}, format="json")
    assert response.status_code == 200 and response.json()["job"]["outcome"] == "discarded"


def test_accept_of_a_console_test_job_is_a_404(staff_client: APIClient) -> None:
    job = GenerationJobFactory.create(mode=enums.AssistantMode.TEST)
    assert staff_client.post(job_url(job.pk, "accept"), {"keys": []}, format="json").status_code == 404
