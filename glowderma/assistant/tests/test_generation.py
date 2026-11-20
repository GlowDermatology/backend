"""Generation and review (services + worker) with the fake content model and the fake Vault.

SPEC phase 11: missing filled / existing unchanged, languages, improve selected, unsupported claims rejected,
malformed output safe; workers use the active key version, Vault failures do not leak; no mutation before
approval, selective apply, conflicts detected, duplicates, provider failures and retries.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from django.conf import settings
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.assistant import prompts, services, validation
from glowderma.assistant.models import AssistantSettings, GenerationJob, PromptTemplate, ProviderConfig
from glowderma.assistant.tests.factories import GenerationJobFactory, PromptVersionFactory
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Product
from glowderma.catalog.tests.factories import ProductFactory, ProductImageFactory
from glowderma.core import enums, timeutils
from glowderma.core.models import AuditEntry
from glowderma.integrations.llm.fake import FakeContentModel
from glowderma.integrations.llm.ports import (
    GenerationRequest,
    GenerationResult,
    LLMBadOutput,
    LLMRefused,
    LLMUnavailable,
)
from glowderma.integrations.secret import Secret
from glowderma.integrations.vault.ports import VaultUnavailable
from glowderma.integrations.vault.registry import fake_vault

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("clean_fakes", "assistant_on")]

FILL = enums.AssistantMode.FILL_MISSING
ALL = ["uz", "ru", "en"]
SECRET = "sk-live-0123456789-secret"  # noqa: S105 - a test value that must never leak


def make_product(**fields: Any) -> Product:
    defaults: dict[str, Any] = {
        "name_uz": "Gialuron serum",
        "description_uz": "Namlovchi serum, 30 ml.",
        "ingredients_uz": "Aqua, Glycerin, Sodium Hyaluronate",
        "status": enums.ProductStatus.HIDDEN,  # an active product needs an image (catalog's active rule)
    }
    return ProductFactory.create(**{**defaults, **fields})


def start(product: Product, mode: str = FILL, **kwargs: Any) -> GenerationJob:
    actor = kwargs.pop("actor", None) or StaffUserFactory.create()
    result = services.start_job(
        product.pk, mode=mode, languages=kwargs.pop("languages", ALL), actor=actor, **kwargs
    )
    assert result.job is not None
    return result.job


def run(job: GenerationJob) -> GenerationJob:
    services.run_job(job.pk)
    return GenerationJob.objects.get(pk=job.pk)


def answer(*items: tuple[str, str], tokens: tuple[int, int] = (1000, 500), **extra: Any) -> GenerationResult:
    data = {"suggestions": [{"key": key, "value": value} for key, value in items], **extra}
    return GenerationResult(data=data, input_tokens=tokens[0], output_tokens=tokens[1], model="gemini-test")


def store_key(value: str) -> ProviderConfig:
    config = ProviderConfig.objects.get(provider="gemini")
    version = fake_vault().write(f"{settings.VAULT_AI_PREFIX}/{config.vault_path}", {"api_key": value})
    config.active_version = version
    config.save()
    return config


# --- target selection ---------------------------------------------------------------------------------------


def test_fill_missing_targets_only_empty_keys_and_never_invents_ingredients(
    fake_llm: FakeContentModel,
) -> None:
    product = make_product()
    job = start(product)
    expected = ["texts.uz.short_description", "seo.uz.title", "seo.uz.description"]
    for language in ("ru", "en"):
        expected += [f"texts.{language}.{name}" for name in ("name", "short_description", "description")]
        expected += [f"texts.{language}.ingredients", f"seo.{language}.title", f"seo.{language}.description"]
    assert sorted(job.fields) == sorted(expected)
    assert not any(
        key.endswith(".usage") for key in job.fields
    )  # no usage anywhere: never written from nothing
    assert job.input_snapshot == dict.fromkeys(job.fields, "")
    done = run(job)
    assert done.state == enums.JobState.SUCCEEDED
    assert {s["key"] for s in done.suggestions} == set(expected)
    assert all(s["current"] == "" for s in done.suggestions)


def test_existing_values_are_never_overwritten_by_fill_missing(fake_llm: FakeContentModel) -> None:
    product = make_product(description_ru="Готовое описание")
    job = start(product)
    assert "texts.ru.description" not in job.fields
    fake_llm.push(answer(("texts.ru.description", "Новое описание"), ("texts.ru.name", "Сыворотка")))
    done = run(job)
    assert [s["key"] for s in done.suggestions] == ["texts.ru.name"]
    assert {"code": "dropped", "count": 1} in done.warnings


def test_nothing_to_fill_creates_no_job() -> None:
    product = make_product()
    result = services.start_job(
        product.pk, mode=FILL, languages=ALL, fields=["texts.uz.name"], actor=StaffUserFactory.create()
    )
    assert result.job is None and result.created is False
    assert not GenerationJob.objects.exists()


def test_languages_mode_only_translates_into_empty_keys() -> None:
    product = make_product(name_ru="Сыворотка")
    job = start(product, enums.AssistantMode.LANGUAGES, languages=["ru", "en"])
    assert job.fields == [
        "texts.ru.description",
        "texts.ru.ingredients",
        "texts.en.name",
        "texts.en.description",
        "texts.en.ingredients",
    ]


def test_improve_targets_the_selected_keys_even_empty_or_placeholder(fake_llm: FakeContentModel) -> None:
    product = make_product(description_ru="Описание", description_en="Test", usage_ru="Утром и вечером")
    job = start(
        product,
        enums.AssistantMode.IMPROVE,
        fields=["texts.uz.description", "texts.ru.description", "texts.en.description", "texts.en.usage"],
        goal=enums.AssistantImproveGoal.SHORTER,
        languages=["uz", "ru", "en"],
    )
    # a placeholder or empty key is written from the facts
    assert job.fields == [
        "texts.uz.description",
        "texts.ru.description",
        "texts.en.description",
        "texts.en.usage",
    ]
    assert job.goal == "shorter"
    done = run(job)
    call = fake_llm.calls[-1]
    assert "make the texts shorter" in call.prompt
    assert '"Test"' not in call.prompt  # the placeholder is not a fact
    assert set(done.prompt_versions) == {"improve", "describe", "seo"}
    for purpose in ("improve", "describe", "seo"):
        version = PromptTemplate.objects.get(purpose=purpose).versions.get(status="active")
        assert version.instructions.strip() in call.system_instruction


def test_improve_needs_a_chosen_key() -> None:
    product = make_product()
    with pytest.raises(ValidationError):
        start(product, enums.AssistantMode.IMPROVE)
    with pytest.raises(ValidationError):  # chosen, but not in the requested languages
        start(product, enums.AssistantMode.IMPROVE, fields=["texts.ru.description"], languages=["uz"])


def test_improve_never_writes_an_empty_name_ingredients_or_usage_from_nothing() -> None:
    product = make_product(name_uz="", ingredients_uz="Test")
    chosen = ["texts.uz.name", "texts.uz.ingredients", "texts.uz.usage", "texts.ru.ingredients"]
    with pytest.raises(ValidationError) as caught:
        start(product, enums.AssistantMode.IMPROVE, fields=chosen)
    detail: Any = caught.value.detail
    assert str(detail["keys"][0]) == services.IMPROVE_NOTHING_TO_WRITE
    product.ingredients_en = "Aqua, Glycerin"
    product.save()
    job = start(product, enums.AssistantMode.IMPROVE, fields=[*chosen, "texts.uz.description"])
    # ingredients have a source to translate from; the name and usage have none
    assert job.fields == ["texts.uz.description", "texts.uz.ingredients", "texts.ru.ingredients"]


def test_unknown_keys_are_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        start(make_product(), fields=["slug", "texts.uz.name"])
    detail: Any = caught.value.detail
    assert "keys.0" in detail


def test_photos_send_images_and_mark_suggestions_unconfirmed(fake_llm: FakeContentModel) -> None:
    product = make_product()
    image = ProductImageFactory.create(product=product)
    job = start(product, enums.AssistantMode.PHOTOS, languages=["uz"])
    assert job.image_ids == [image.pk]
    assert job.fields == ["texts.uz.name", "texts.uz.ingredients", "texts.uz.usage"]
    fake_llm.push(
        answer(("texts.uz.usage", "Kuniga 2 marta"), extracted={"brand": "Glow", "volume": "50 ml"})
    )
    done = run(job)
    call = fake_llm.calls[-1]
    assert len(call.images) == 1 and call.images[0].mime_type == "image/webp" and call.images[0].data
    assert done.suggestions[0]["origin"] == "image" and done.suggestions[0]["checked"] is False
    assert "confirm_packaging" in done.suggestions[0]["warnings"]
    assert {"code": "extracted", "brand": "Glow", "volume": "50 ml"} in done.warnings


def test_photos_need_an_image() -> None:
    with pytest.raises(ValidationError):
        start(make_product(), enums.AssistantMode.PHOTOS)
    other = ProductImageFactory.create()
    with pytest.raises(ValidationError):
        start(make_product(), enums.AssistantMode.PHOTOS, image_ids=[other.pk])


def test_image_alt_is_suggested_and_applied(fake_llm: FakeContentModel) -> None:
    product = make_product()
    image = ProductImageFactory.create(product=product)
    job = run(start(product, languages=["uz"]))
    key = f"images.{image.pk}.alt.uz"
    assert key in {s["key"] for s in job.suggestions}
    result = services.apply_job(job.pk, [services.ApplyItem(key, "Serum shishasi")], actor=job.created_by)
    assert result.applied == [key]
    image.refresh_from_db()
    assert image.alt_uz == "Serum shishasi"


# --- placeholders and the unsaved draft ---------------------------------------------------------------------


def test_placeholders_count_as_empty_for_fill_missing() -> None:
    product = make_product(name_ru="Test", short_description_uz="...", description_en="asdf")
    job = start(product)
    assert {"texts.ru.name", "texts.uz.short_description", "texts.en.description"} <= set(job.fields)
    assert job.input_snapshot["texts.ru.name"] == "Test"  # the saved value (apply's conflict check)


def test_a_placeholder_name_is_no_source_to_translate_from() -> None:
    product = make_product(name_uz="Test", description_uz="", ingredients_uz="")
    result = services.start_job(product.pk, mode=FILL, languages=ALL, actor=StaffUserFactory.create())
    assert result.job is None  # no real name anywhere: nothing can be written truthfully


def test_the_draft_is_the_working_value(fake_llm: FakeContentModel) -> None:
    product = make_product(name_ru="Сыворотка")
    draft = {
        "texts.ru.name": "",  # cleared in the form → a target
        "texts.ru.description": "Увлажняющая сыворотка, 50 мл.",  # typed in the form → not a target
        "texts.en.name": "Test",  # a placeholder → still a target
    }
    job = start(product, languages=["ru", "en"], draft=draft)
    assert job.draft == draft
    assert "texts.ru.name" in job.fields and "texts.en.name" in job.fields
    assert "texts.ru.description" not in job.fields
    assert job.input_snapshot["texts.ru.name"] == "Сыворотка"  # saved values, not the draft
    fake_llm.push(
        answer(
            ("texts.ru.name", "Сыворотка Glow"),
            ("texts.en.name", "Serum"),
            ("texts.en.description", "Moisturising serum, 50 ml."),  # 50 is a fact of the draft only
        )
    )
    done = run(job)
    by_key = {s["key"]: s for s in done.suggestions}
    assert by_key["texts.ru.name"]["current"] == ""  # what the person sees in the form
    assert by_key["texts.en.name"]["current"] == "Test"
    assert by_key["texts.en.description"]["warnings"] == []
    prompt = fake_llm.calls[-1].prompt
    assert "Увлажняющая сыворотка, 50 мл." in prompt
    assert '"name": "Сыворотка"' not in prompt and '"Test"' not in prompt


def test_the_draft_is_checked() -> None:
    product = make_product()
    own = ProductImageFactory.create(product=product)
    other = ProductImageFactory.create()
    cases: list[tuple[dict[str, Any], str]] = [
        ({"slug": "x"}, "draft.slug"),
        ({"texts.uz.price": "x"}, "draft.texts.uz.price"),
        ({f"images.{other.pk}.alt.uz": "x"}, f"draft.images.{other.pk}.alt.uz"),
        ({"texts.uz.name": "x" * 121}, "draft.texts.uz.name"),
        ({f"images.{own.pk}.alt.ru": "x" * 161}, f"draft.images.{own.pk}.alt.ru"),
        ({"texts.uz.name": 5}, "draft.texts.uz.name"),
        ({f"texts.uz.x{i}": "" for i in range(151)}, "draft"),
    ]
    for draft, field in cases:
        with pytest.raises(ValidationError) as caught:
            start(product, draft=draft)
        detail: Any = caught.value.detail
        assert field in detail, (field, detail)
    job = start(product, draft={f"images.{own.pk}.alt.ru": "x" * 160, "texts.uz.name": "x" * 120})
    assert job.draft[f"images.{own.pk}.alt.ru"] == "x" * 160


def test_apply_checks_edited_values_against_the_draft_facts(fake_llm: FakeContentModel) -> None:
    job = start(make_product(), languages=["ru"], draft={"texts.uz.usage": "Kuniga 3 marta, 75 ml"})
    fake_llm.push(answer(("texts.ru.name", "Сыворотка")))
    done = run(job)
    result = services.apply_job(
        done.pk, [services.ApplyItem("texts.ru.name", "Сыворотка 75 мл")], actor=done.created_by
    )
    assert result.applied == ["texts.ru.name"]


def test_the_pos_stock_label_is_rejected(fake_llm: FakeContentModel) -> None:
    product = make_product(billz_name="NEW TONER 200ML", name_uz="", description_uz="", ingredients_uz="")
    product.name_ru = "Тоник"
    product.save()
    job = start(product, languages=["uz"])
    assert "texts.uz.name" in job.fields
    fake_llm.push(answer(("texts.uz.name", "NEW TONER 200ML"), ("texts.uz.description", "Tonik.")))
    done = run(job)
    assert [s["key"] for s in done.suggestions] == ["texts.uz.description"]
    assert {"code": "rejected", "key": "texts.uz.name", "reason": "pos_label"} in done.warnings
    prompt = fake_llm.calls[-1].prompt
    assert '"pos_stock_label": "NEW TONER 200ML"' in prompt and "billz_name" not in prompt


def test_skipped_and_unchanged_keys_are_reported(fake_llm: FakeContentModel) -> None:
    product = make_product(description_ru="Описание", usage_ru="Утром")
    job = start(
        product,
        enums.AssistantMode.IMPROVE,
        fields=["texts.uz.description", "texts.ru.description", "texts.en.usage"],
    )
    fake_llm.push(
        answer(
            ("texts.uz.description", "Namlovchi serum, 30 ml."),  # equal to the current text
            ("texts.ru.description", "Увлажняющая сыворотка."),
            skipped=[
                {"key": "texts.en.usage", "reason": "No directions for use in the data."},
                {"key": "texts.ru.description", "reason": "answered as well: ignored"},
            ],
        )
    )
    done = run(job)
    assert [s["key"] for s in done.suggestions] == ["texts.ru.description"]
    assert done.warnings == [
        {"code": "unchanged", "key": "texts.uz.description"},
        {"code": "skipped", "key": "texts.en.usage", "reason": "No directions for use in the data."},
    ]


def test_a_key_without_an_answer_is_reported_as_skipped(fake_llm: FakeContentModel) -> None:
    job = start(make_product(), languages=["ru"])
    fake_llm.push(answer(("texts.ru.name", "Сыворотка")))
    done = run(job)
    missing = [key for key in done.fields if key != "texts.ru.name"]
    assert missing
    assert [w for w in done.warnings if w["code"] == "skipped"] == [
        {"code": "skipped", "key": key, "reason": "no answer"} for key in missing
    ]


def test_the_fake_model_answers_every_key_and_skips_none(fake_llm: FakeContentModel) -> None:
    done = run(start(make_product()))
    assert {s["key"] for s in done.suggestions} == set(done.fields)
    assert not [w for w in done.warnings if w["code"] in ("skipped", "unchanged", "rejected")]


# --- the run ------------------------------------------------------------------------------------------------


def test_the_answer_budget_grows_with_the_targets(fake_llm: FakeContentModel) -> None:
    assert services.answer_budget(["texts.uz.name"], 8192) == 8192
    assert services.answer_budget(["texts.uz.description"] * 20, 8192) == 16384  # capped
    assert services.answer_budget(["texts.uz.description"] * 5, 4096) == 4512  # 10000 / 2.5 + 512
    assert services.answer_budget(["texts.uz.description"] * 5, 20000) == 20000  # the configured value wins
    job = start(make_product())
    run(job)
    expected = int(sum(validation.max_length(key) for key in job.fields) / 2.5) + 512
    assert fake_llm.calls[-1].max_output_tokens == max(8192, min(expected, 16384))


def test_a_new_provider_row_defaults_to_8192_output_tokens() -> None:
    assert ProviderConfig.objects.get(provider="gemini").max_output_tokens == 8192


def test_run_records_usage_cost_versions_and_rules(fake_llm: FakeContentModel) -> None:
    product = make_product()
    job = start(product)
    fake_llm.push(answer(("texts.ru.name", "Сыворотка")))
    done = run(job)
    assert (done.input_tokens, done.output_tokens) == (1000, 500)
    assert done.cost_usd == Decimal("0.000300")  # 1000 × 0.10 + 500 × 0.40 per 1M
    describe = PromptTemplate.objects.get(purpose="describe").versions.get(status="active")
    seo = PromptTemplate.objects.get(purpose="seo").versions.get(status="active")
    assert done.prompt_versions == {"describe": describe.pk, "seo": seo.pk}
    assert done.rules_version == prompts.RULES_VERSION
    assert done.model_id == "gemini-2.5-flash-lite"
    call = fake_llm.calls[-1]
    assert call.system_instruction.startswith(prompts.APP_RULES)
    assert describe.instructions.strip() in call.system_instruction
    assert (
        call.response_schema["properties"]["suggestions"]["items"]["properties"]["key"]["enum"] == job.fields
    )
    assert "185000" not in call.prompt  # the price never reaches the model


def test_generation_does_not_touch_the_product(fake_llm: FakeContentModel) -> None:
    product = make_product()
    before = catalog_services.product_snapshot(product)
    done = run(start(product))
    assert done.suggestions
    product = Product.objects.get(pk=product.pk)
    assert catalog_services.product_snapshot(product) == before
    assert not AuditEntry.objects.filter(action="product.updated").exists()


def test_unsupported_claims_are_rejected_or_flagged(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    fake_llm.push(
        answer(
            ("texts.ru.description", "Сыворотка с SPF 50 и 15% витамина C"),
            ("texts.en.description", "Dermatologist tested serum"),
            ("texts.ru.name", "Сыворотка"),
        )
    )
    done = run(job)
    by_key = {s["key"]: s for s in done.suggestions}
    assert "texts.ru.description" not in by_key
    assert {"code": "rejected", "key": "texts.ru.description", "reason": "unsupported_spf"} in done.warnings
    assert by_key["texts.en.description"]["warnings"] == ["unsupported_claim"]
    assert by_key["texts.en.description"]["checked"] is False
    assert by_key["texts.ru.name"]["checked"] is True


def test_forbidden_keys_in_the_answer_are_dropped(fake_llm: FakeContentModel) -> None:
    product = make_product()
    job = start(product)
    fake_llm.push(answer(("slug", "hacked"), ("price", "1"), ("texts.ru.name", "Сыворотка")))
    done = run(job)
    assert [s["key"] for s in done.suggestions] == ["texts.ru.name"]
    assert {"code": "dropped", "count": 2} in done.warnings
    assert Product.objects.get(pk=product.pk).slug == product.slug


def test_malformed_answer_is_retried_once(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    fake_llm.push(LLMBadOutput("truncated"))
    done = run(job)
    assert done.state == enums.JobState.SUCCEEDED
    assert len(fake_llm.calls) == 2


def test_wrong_answer_shape_is_retried_once_and_counted(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    fake_llm.push(GenerationResult(data={"notes": []}, input_tokens=10, output_tokens=5, model="m"))
    done = run(job)
    assert done.state == enums.JobState.SUCCEEDED
    assert done.input_tokens > 10  # both answers are billed


def test_malformed_twice_fails_safely(fake_llm: FakeContentModel) -> None:
    job = start(make_product(name_uz="Serum [fake-malformed]"))
    done = run(job)
    assert done.state == enums.JobState.FAILED and done.error_code == "ai_bad_output"
    assert done.suggestions == [] and done.finished_at is not None
    assert len(fake_llm.calls) == 2


def test_billed_but_unusable_answers_count_toward_the_cost(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    fake_llm.push(LLMBadOutput("truncated", input_tokens=1000, output_tokens=500))
    fake_llm.push(LLMBadOutput("truncated", input_tokens=1000, output_tokens=500))
    done = run(job)
    assert done.state == enums.JobState.FAILED and done.error_code == "ai_bad_output"
    assert (done.input_tokens, done.output_tokens) == (2000, 1000)
    assert done.cost_usd == Decimal("0.000600")  # both attempts are billed


def test_refused_answer_records_its_billed_tokens(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    fake_llm.push(LLMRefused("safety", input_tokens=300, output_tokens=7))
    done = run(job)
    assert done.error_code == "ai_refused" and (done.input_tokens, done.output_tokens) == (300, 7)
    assert len(fake_llm.calls) == 1  # never retried


def test_provider_unavailable_fails_without_our_own_retry(fake_llm: FakeContentModel) -> None:
    job = start(make_product(name_uz="Serum [fake-unavailable]"))
    done = run(job)
    assert done.state == enums.JobState.FAILED and done.error_code == "provider_unavailable"
    assert len(fake_llm.calls) == 1  # transport retries are the SDK's (bounded by max_retries)
    assert fake_llm.calls[0].max_retries == 2 and fake_llm.calls[0].timeout_seconds == 20


def test_each_call_fits_the_run_time_budget(
    fake_llm: FakeContentModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    targets = ["texts.ru.name"]
    request = GenerationRequest(
        model="m",
        system_instruction="s",
        prompt="p",
        response_schema=prompts.response_schema(targets, mode=FILL),
        timeout_seconds=20,
    )
    clock = iter([0.0, 44.0])  # first call at once, the retry after a malformed answer 44 s later
    monkeypatch.setattr(services, "_clock", lambda: next(clock))
    fake_llm.push(LLMBadOutput("truncated"))
    services._generate(fake_llm, request, Secret("k"), targets, [0, 0], deadline=80.0)
    # (80 - now - 2 retries × 8 s back-off) // 3 attempts: 21 → the configured 20 s, then 20 // 3 = 6 s
    assert [call.timeout_seconds for call in fake_llm.calls] == [20, 6]
    monkeypatch.setattr(services, "_clock", lambda: 60.0)
    with pytest.raises(LLMUnavailable):
        services._generate(fake_llm, request, Secret("k"), targets, [0, 0], deadline=80.0)


def test_refused_request(fake_llm: FakeContentModel) -> None:
    done = run(start(make_product(name_uz="Serum [fake-refused]")))
    assert done.error_code == "ai_refused"


def test_switch_turned_off_before_the_run(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    AssistantSettings.objects.update(enabled=False)
    done = run(job)
    assert done.error_code == "assistant_disabled" and fake_llm.calls == []


def test_product_deleted_before_the_run(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    GenerationJob.objects.filter(pk=job.pk).update(product=None)
    assert run(job).error_code == "not_found"


# --- key resolution -----------------------------------------------------------------------------------------


def test_worker_uses_the_active_vault_version_without_restart(
    fake_llm: FakeContentModel, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    store_key("invalid-old-key")
    first = run(start(make_product()))
    assert first.error_code == "ai_not_configured"  # the fake refuses keys starting "invalid"
    store_key(SECRET)  # a rotation: a new version becomes active in PostgreSQL
    second = run(start(make_product()))
    assert second.state == enums.JobState.SUCCEEDED
    for job in (first, second):
        row = json.dumps(GenerationJob.objects.filter(pk=job.pk).values().get(), default=str)
        assert SECRET not in row and "invalid-old-key" not in row
    assert SECRET not in caplog.text
    assert all(SECRET not in repr(call) for call in fake_llm.calls)


def test_vault_down_fails_the_job_without_leaking(
    fake_llm: FakeContentModel, caplog: pytest.LogCaptureFixture
) -> None:
    store_key(SECRET)
    fake_vault().fail_next(VaultUnavailable("vault down"))
    caplog.set_level(logging.DEBUG)
    done = run(start(make_product()))
    assert done.state == enums.JobState.FAILED and done.error_code == "vault_unavailable"
    assert fake_llm.calls == []
    assert SECRET not in caplog.text


def test_destroyed_version_is_not_configured(fake_llm: FakeContentModel) -> None:
    config = store_key(SECRET)
    fake_vault().destroy(f"{settings.VAULT_AI_PREFIX}/{config.vault_path}", config.active_version or 0)
    assert run(start(make_product())).error_code == "ai_not_configured"


def test_live_mode_without_a_key_is_not_configured(settings: Any) -> None:
    settings.AI_MODE = "live"
    assert services.is_available() is False
    with pytest.raises(services.AssistantNotConfigured):
        start(make_product())


# --- limits and duplicates ----------------------------------------------------------------------------------


def test_one_active_job_per_product() -> None:
    product = make_product()
    first = start(product)
    again = services.start_job(product.pk, mode=FILL, languages=ALL, actor=StaffUserFactory.create())
    assert again.job == first and again.created is False
    assert GenerationJob.objects.count() == 1


def test_disabled_assistant_is_refused() -> None:
    AssistantSettings.objects.update(enabled=False)
    with pytest.raises(services.AssistantDisabled):
        start(make_product())


def test_disabled_provider_is_refused() -> None:
    ProviderConfig.objects.update(enabled=False)
    with pytest.raises(services.AssistantNotConfigured):
        start(make_product())


def test_per_user_daily_limit() -> None:
    AssistantSettings.objects.update(per_user_daily_jobs=1)
    actor = StaffUserFactory.create()
    GenerationJobFactory.create(created_by=actor, state=enums.JobState.FAILED, error_code="x")
    with pytest.raises(services.DailyLimitReached):
        start(make_product(), actor=actor)
    start(make_product())  # someone else may still generate


def test_daily_and_monthly_budgets() -> None:
    GenerationJobFactory.create(state=enums.JobState.FAILED, error_code="x", cost_usd=Decimal("5"))
    with pytest.raises(services.BudgetExceeded):
        start(make_product())
    AssistantSettings.objects.update(daily_budget_usd=Decimal("100"), monthly_budget_usd=Decimal("5"))
    with pytest.raises(services.BudgetExceeded):
        start(make_product())
    AssistantSettings.objects.update(monthly_budget_usd=Decimal("100"))
    start(make_product())


def test_test_mode_is_not_an_admin_job() -> None:
    with pytest.raises(ValidationError):
        start(make_product(), enums.AssistantMode.TEST)


# --- worker claim and reaper --------------------------------------------------------------------------------


def test_a_duplicate_delivery_skips_a_live_run(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    claimed = services.claim_job(job.pk)
    assert claimed is not None and claimed.attempts == 1
    assert services.run_job(job.pk) is None
    assert fake_llm.calls == []


def test_a_lost_worker_is_reclaimed(fake_llm: FakeContentModel) -> None:
    job = start(make_product())
    services.claim_job(job.pk)
    GenerationJob.objects.filter(pk=job.pk).update(heartbeat_at=timeutils.now() - timedelta(minutes=10))
    done = run(job)
    assert done.state == enums.JobState.SUCCEEDED and done.attempts == 2


def test_finished_jobs_are_not_rerun(fake_llm: FakeContentModel) -> None:
    job = run(start(make_product()))
    calls = len(fake_llm.calls)
    assert services.run_job(job.pk) is None and len(fake_llm.calls) == calls


def test_reaper_fails_stale_and_lost_jobs() -> None:
    stale = start(make_product())
    services.claim_job(stale.pk)
    lost = start(make_product())
    fresh = start(make_product())
    past = timeutils.now() - timedelta(minutes=6)
    GenerationJob.objects.filter(pk=stale.pk).update(heartbeat_at=past)
    GenerationJob.objects.filter(pk=lost.pk).update(created_at=past)
    assert sorted(services.reap_stale_jobs()) == sorted([stale.pk, lost.pk])
    for job in (stale, lost):
        job.refresh_from_db()
        assert job.state == enums.JobState.FAILED and job.error_code == "worker_lost"
    fresh.refresh_from_db()
    assert fresh.state == enums.JobState.QUEUED
    start(product_of(stale))  # the product's slot is free again


# --- console prompt tests -----------------------------------------------------------------------------------


def test_prompt_test_runs_the_draft_version(fake_llm: FakeContentModel) -> None:
    AssistantSettings.objects.update(enabled=False)  # a superuser tests before switching it on
    draft = PromptVersionFactory.create(instructions="DRAFT-STYLE-MARKER")
    product = make_product()
    job = services.start_prompt_test(version=draft, product_id=product.pk, actor=StaffUserFactory.create())
    assert job.mode == enums.AssistantMode.TEST and job.prompt_versions == {"describe": draft.pk}
    AssistantSettings.objects.update(enabled=True)
    start(product)  # a test job does not hold the product's slot
    done = run(job)
    assert done.state == enums.JobState.SUCCEEDED and done.prompt_versions == {"describe": draft.pk}
    call = fake_llm.calls[-1]
    assert "DRAFT-STYLE-MARKER" in call.system_instruction
    assert call.system_instruction.startswith(prompts.APP_RULES)
    assert set(job.fields) == {
        f"texts.{lang}.{name}" for lang in ALL for name in ("short_description", "description")
    }


@pytest.mark.parametrize("purpose", ["translate", "improve", "seo", "image_extract"])
def test_prompt_test_of_every_purpose(purpose: str, fake_llm: FakeContentModel) -> None:
    version = PromptVersionFactory.create(template=PromptTemplate.objects.get(purpose=purpose))
    product = make_product()
    ProductImageFactory.create(product=product)
    job = services.start_prompt_test(version=version, product_id=product.pk, actor=StaffUserFactory.create())
    assert run(job).state == enums.JobState.SUCCEEDED
    assert fake_llm.calls[-1].system_instruction.count("### ") == 1


def test_prompt_test_counts_towards_the_budget() -> None:
    GenerationJobFactory.create(state=enums.JobState.FAILED, error_code="x", cost_usd=Decimal("5"))
    version = PromptVersionFactory.create()
    with pytest.raises(services.BudgetExceeded):
        services.start_prompt_test(
            version=version, product_id=make_product().pk, actor=StaffUserFactory.create()
        )
    with pytest.raises(NotFound):
        services.start_prompt_test(version=version, product_id=999_999, actor=StaffUserFactory.create())


# --- apply and discard --------------------------------------------------------------------------------------


def product_of(job: GenerationJob) -> Product:
    return Product.objects.get(pk=int(job.product_id or 0))


def generated(fake_llm: FakeContentModel, **product_fields: Any) -> GenerationJob:
    job = start(make_product(**product_fields))
    fake_llm.push(
        answer(("texts.ru.name", "Сыворотка"), ("texts.ru.description", "Увлажняющая сыворотка, 30 мл."))
    )
    return run(job)


def test_apply_writes_only_the_selected_keys(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    actor = StaffUserFactory.create()
    result = services.apply_job(job.pk, [services.ApplyItem("texts.ru.name", "Сыворотка")], actor=actor)
    assert result.applied == ["texts.ru.name"] and result.conflicts == []
    product = product_of(job)
    assert product.name_ru == "Сыворотка" and product.description_ru == ""
    job.refresh_from_db()
    assert job.outcome == enums.AssistantJobOutcome.APPLIED and job.applied_keys == ["texts.ru.name"]
    assert job.applied_by == actor
    entry = AuditEntry.objects.get(action="assistant.suggestions_applied")
    assert entry.after == {"job_id": job.pk, "applied": ["texts.ru.name"], "conflicts": []}
    assert AuditEntry.objects.filter(action="product.updated", entity_id=str(product.pk)).count() == 1


def test_edited_values_are_normalised_and_checked_again(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    with pytest.raises(ValidationError) as caught:
        services.apply_job(
            job.pk,
            [
                services.ApplyItem("texts.ru.name", "Сыворотка SPF 30"),
                services.ApplyItem("texts.ru.description", "x" * 2001),
            ],
            actor=job.created_by,
        )
    detail: Any = caught.value.detail
    assert detail["items.0.value"][0].code == "invalid"
    assert detail["items.1.value"][0].code == "max_length"
    with pytest.raises(ValidationError):
        services.apply_job(job.pk, [services.ApplyItem("texts.uz.usage", "x")], actor=job.created_by)
    assert product_of(job).name_ru == ""
    services.apply_job(
        job.pk, [services.ApplyItem("texts.ru.name", "  Сыворотка Glow ")], actor=job.created_by
    )
    assert product_of(job).name_ru == "Сыворотка Glow"


def test_a_value_changed_meanwhile_is_a_conflict(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    staff = StaffUserFactory.create()
    catalog_services.update_product(
        job.product_id or 0,
        catalog_services.ProductInput(texts={"ru": {"description": "Ручное описание"}}),
        actor=staff,
    )
    result = services.apply_job(
        job.pk,
        [
            services.ApplyItem("texts.ru.name", "Сыворотка"),
            services.ApplyItem("texts.ru.description", "Новое"),
        ],
        actor=staff,
    )
    assert result.applied == ["texts.ru.name"] and result.conflicts == ["texts.ru.description"]
    product = product_of(job)
    assert product.description_ru == "Ручное описание" and product.name_ru == "Сыворотка"
    job.refresh_from_db()
    assert job.outcome == enums.AssistantJobOutcome.PARTIAL


def test_all_conflicts_write_nothing_and_keep_the_review_open(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    Product.objects.filter(pk=product_of(job).pk).update(name_ru="Кто-то другой")
    result = services.apply_job(
        job.pk, [services.ApplyItem("texts.ru.name", "Сыворотка")], actor=job.created_by
    )
    assert result.applied == [] and result.conflicts == ["texts.ru.name"]
    assert result.product.name_ru == "Кто-то другой"
    job.refresh_from_db()
    assert job.outcome == enums.AssistantJobOutcome.PENDING
    services.discard_job(job.pk, actor=job.created_by)


def test_a_reviewed_job_cannot_be_applied_again(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    services.apply_job(job.pk, [services.ApplyItem("texts.ru.name", "Сыворотка")], actor=job.created_by)
    with pytest.raises(services.JobNotReviewable):
        services.apply_job(
            job.pk, [services.ApplyItem("texts.ru.description", "Новое")], actor=job.created_by
        )
    with pytest.raises(services.JobNotReviewable):
        services.discard_job(job.pk, actor=job.created_by)


def test_discard_leaves_the_product_alone(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    before = catalog_services.product_snapshot(product_of(job))
    services.discard_job(job.pk, actor=job.created_by)
    job.refresh_from_db()
    assert job.outcome == enums.AssistantJobOutcome.DISCARDED
    assert catalog_services.product_snapshot(product_of(job)) == before
    assert AuditEntry.objects.filter(action="assistant.suggestions_discarded").exists()
    with pytest.raises(services.JobNotReviewable):
        services.apply_job(job.pk, [services.ApplyItem("texts.ru.name", "Сыворотка")], actor=job.created_by)


def test_unfinished_and_failed_jobs_cannot_be_reviewed(fake_llm: FakeContentModel) -> None:
    queued = start(make_product())
    with pytest.raises(services.JobNotReviewable):
        services.discard_job(queued.pk, actor=queued.created_by)
    failed = run(start(make_product(name_uz="x [fake-refused]")))
    with pytest.raises(services.JobNotReviewable):
        services.apply_job(failed.pk, [services.ApplyItem("texts.ru.name", "a")], actor=failed.created_by)
    with pytest.raises(NotFound):
        services.discard_job(999_999, actor=failed.created_by)


# --- accept (the suggestions go into the unsaved form) ------------------------------------------------------


def test_accept_records_the_outcome_without_writing_the_product(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    before = catalog_services.product_snapshot(product_of(job))
    actor = StaffUserFactory.create()
    accepted = services.accept_job(job.pk, ["texts.ru.description"], actor=actor)
    assert accepted.outcome == enums.AssistantJobOutcome.PARTIAL
    assert accepted.applied_keys == ["texts.ru.description"] and accepted.applied_by == actor
    assert accepted.applied_at is not None
    assert catalog_services.product_snapshot(product_of(job)) == before
    assert not AuditEntry.objects.filter(action="product.updated").exists()
    entry = AuditEntry.objects.get(action="assistant.suggestions_accepted")
    assert entry.entity_type == "product" and entry.entity_id == str(job.product_id)
    assert entry.after == {"job_id": job.pk, "accepted": ["texts.ru.description"], "suggestions": 2}
    with pytest.raises(services.JobNotReviewable):  # a second call
        services.accept_job(job.pk, [], actor=actor)


@pytest.mark.parametrize(
    ("keys", "outcome", "applied"),
    [
        (["texts.ru.description", "texts.ru.name", "texts.ru.name"], "applied",
         ["texts.ru.name", "texts.ru.description"]),
        (["texts.ru.name"], "partial", ["texts.ru.name"]),
        ([], "discarded", []),
    ],
)  # fmt: skip
def test_accept_outcomes(
    keys: list[str], outcome: str, applied: list[str], fake_llm: FakeContentModel
) -> None:
    job = generated(fake_llm)
    accepted = services.accept_job(job.pk, keys, actor=job.created_by)
    assert accepted.outcome == outcome and accepted.applied_keys == applied
    assert (accepted.applied_at is None) is (not applied)


def test_accept_refuses_keys_that_were_not_suggested(fake_llm: FakeContentModel) -> None:
    job = generated(fake_llm)
    with pytest.raises(ValidationError) as caught:
        services.accept_job(job.pk, ["texts.ru.name", "texts.uz.usage"], actor=job.created_by)
    detail: Any = caught.value.detail
    assert list(detail) == ["keys.1"]
    job.refresh_from_db()
    assert job.outcome == enums.AssistantJobOutcome.PENDING


def test_accept_needs_a_pending_succeeded_job(fake_llm: FakeContentModel) -> None:
    queued = start(make_product())
    with pytest.raises(services.JobNotReviewable):
        services.accept_job(queued.pk, [], actor=queued.created_by)
    test_job = GenerationJobFactory.create(mode=enums.AssistantMode.TEST)
    with pytest.raises(NotFound):
        services.accept_job(test_job.pk, [], actor=queued.created_by)
