"""The assistant's seeded rows and the database constraints of its models."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from django.apps import apps as global_apps
from django.db import IntegrityError, transaction

from glowderma.assistant import defaults
from glowderma.assistant.apps import ensure_rows
from glowderma.assistant.models import (
    AssistantSettings,
    GenerationJob,
    PromptTemplate,
    PromptVersion,
    ProviderConfig,
)
from glowderma.assistant.tests.factories import GenerationJobFactory, PromptVersionFactory
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums

pytestmark = pytest.mark.django_db


def raises_integrity(create: Any) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


def test_seeded_rows() -> None:
    settings_row = AssistantSettings.objects.get()
    assert settings_row.pk == 1 and settings_row.enabled is False
    assert settings_row.active_provider == "gemini" and settings_row.max_images == 3
    provider = ProviderConfig.objects.get()
    assert provider.provider == "gemini" and provider.model_id == "gemini-2.5-flash-lite"
    assert provider.price_input_per_mtok_usd == Decimal("0.10")
    assert provider.price_output_per_mtok_usd == Decimal("0.40")
    assert provider.vault_path == "gemini" and provider.active_version is None and not provider.configured
    assert provider.test_status == enums.CredentialTestStatus.UNKNOWN
    purposes = set(PromptTemplate.objects.values_list("purpose", flat=True))
    assert purposes == set(enums.PromptPurpose.values)
    for template in PromptTemplate.objects.all():
        [version] = template.versions.all()
        assert version.number == 1 and version.status == enums.PromptVersionStatus.ACTIVE
        assert version.instructions == defaults.INSTRUCTIONS[template.purpose]
        assert len(version.instructions) < 2000


def test_ensure_rows_is_idempotent_and_keeps_edits() -> None:
    ProviderConfig.objects.filter(provider="gemini").update(model_id="gemini-3.1-flash-lite")
    AssistantSettings.objects.update(enabled=True)
    ensure_rows(global_apps.get_app_config("assistant"), using="default")
    assert ProviderConfig.objects.get().model_id == "gemini-3.1-flash-lite"
    assert AssistantSettings.objects.get().enabled is True
    assert PromptVersion.objects.count() == 5


def test_ensure_rows_refills_after_a_flush() -> None:
    PromptVersion.objects.all().delete()
    PromptTemplate.objects.all().delete()
    ProviderConfig.objects.all().delete()
    AssistantSettings.objects.all().delete()
    ensure_rows(global_apps.get_app_config("assistant"), using="default")
    assert AssistantSettings.objects.count() == 1 and ProviderConfig.objects.count() == 1
    assert PromptVersion.objects.filter(status="active").count() == 5


def test_settings_singleton_and_ranges() -> None:
    raises_integrity(lambda: AssistantSettings.objects.create(id=2))
    raises_integrity(lambda: AssistantSettings.objects.filter(pk=1).update(max_images=9))
    raises_integrity(lambda: AssistantSettings.objects.filter(pk=1).update(daily_budget_usd=Decimal("-1")))


@pytest.mark.parametrize(
    "fields",
    [
        {"temperature": Decimal("2.5")},
        {"max_output_tokens": 100},
        {"timeout_seconds": 1},
        {"timeout_seconds": 21},
        {"max_retries": 3},
        {"price_input_per_mtok_usd": Decimal("-0.1")},
        {"vault_path": ""},
        {"active_version": 2, "previous_version": 2},
    ],
)
def test_provider_constraints(fields: dict[str, Any]) -> None:
    raises_integrity(lambda: ProviderConfig.objects.filter(provider="gemini").update(**fields))


def test_provider_has_no_key_column() -> None:
    names = {field.name for field in ProviderConfig._meta.get_fields()}
    assert not {name for name in names if "key" in name and name != "id"}


def test_one_active_prompt_version_per_template() -> None:
    template = PromptTemplate.objects.get(purpose=enums.PromptPurpose.SEO)
    draft = PromptVersionFactory.create(template=template)
    assert draft.number == 2 and str(draft) == "SEO matnlari v2"
    raises_integrity(lambda: PromptVersion.objects.filter(pk=draft.pk).update(status="active"))
    raises_integrity(lambda: PromptVersionFactory.create(template=template, number=1))
    raises_integrity(lambda: PromptVersionFactory.create(template=template, instructions=""))
    # retiring the active version first lets another one become active (a rollback activates a retired one)
    template.versions.filter(number=1).update(status="retired")
    PromptVersion.objects.filter(pk=draft.pk).update(status="active")
    template.versions.filter(number=1).update(status="tested")
    assert template.versions.get(status="active").pk == draft.pk


def test_one_active_job_per_product_excludes_tests_and_finished_jobs() -> None:
    product = ProductFactory.create()
    job = GenerationJobFactory.create(product=product)
    assert job.is_active and job.outcome == enums.AssistantJobOutcome.PENDING
    raises_integrity(lambda: GenerationJobFactory.create(product=product, created_by=job.created_by))
    GenerationJobFactory.create(product=product, mode=enums.AssistantMode.TEST)  # console prompt test
    GenerationJob.objects.filter(pk=job.pk).update(state="succeeded", finished_at=job.created_at)
    GenerationJobFactory.create(product=product)


def test_job_state_constraints() -> None:
    job = GenerationJobFactory.create()
    raises_integrity(lambda: GenerationJob.objects.filter(pk=job.pk).update(state="failed"))
    raises_integrity(lambda: GenerationJob.objects.filter(pk=job.pk).update(finished_at=job.created_at))
    raises_integrity(lambda: GenerationJob.objects.filter(pk=job.pk).update(outcome="applied"))
    raises_integrity(lambda: GenerationJob.objects.filter(pk=job.pk).update(cost_usd=Decimal("-1")))
    GenerationJob.objects.filter(pk=job.pk).update(
        state="failed", error_code="provider_unavailable", finished_at=job.created_at
    )
    job.refresh_from_db()
    assert not job.is_active and str(job) == f"#{job.pk} Bo‘sh maydonlarni to‘ldirish"


def test_improve_guidance_v1_is_upgraded_only_when_untouched() -> None:
    import importlib

    from django.db import connection

    from glowderma.assistant.models import PromptTemplate, PromptVersion

    migration = importlib.import_module("glowderma.assistant.migrations.0004_improve_guidance_v2")

    class Editor:
        pass

    editor = Editor()
    editor.connection = connection  # type: ignore[attr-defined]
    template = PromptTemplate.objects.get(purpose="improve")
    v1 = template.versions.get()
    assert v1.instructions == defaults.INSTRUCTIONS["improve"]  # a fresh database seeds the new text
    migration.upgrade(global_apps, editor)
    assert template.versions.count() == 1  # nothing to upgrade
    PromptVersion.objects.filter(pk=v1.pk).update(instructions=migration.OLD_IMPROVE_V1)
    migration.upgrade(global_apps, editor)
    versions = {v.number: v for v in template.versions.all()}
    assert versions[1].status == "retired" and versions[2].status == "active"
    assert versions[2].instructions == defaults.INSTRUCTIONS["improve"]
    migration.upgrade(global_apps, editor)  # idempotent: an already versioned template stays as it is
    assert template.versions.count() == 2
