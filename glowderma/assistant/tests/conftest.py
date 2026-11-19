"""Fixtures of the assistant tests: ``clean_fakes`` (the fake content model, the fake Vault and the key cache
reset around a test; the generation tests use it for every test), ``fake_llm``, ``assistant_on``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from glowderma.assistant import credentials
from glowderma.assistant.models import AssistantSettings
from glowderma.integrations.llm.fake import FakeContentModel
from glowderma.integrations.llm.registry import fake_content_model
from glowderma.integrations.vault.registry import fake_vault


@pytest.fixture
def fake_llm(clean_fakes: None) -> Iterator[FakeContentModel]:
    model = fake_content_model()
    model.reset()
    yield model
    model.reset()


@pytest.fixture
def clean_fakes() -> Iterator[None]:
    fake_vault().reset()
    credentials.clear_cache()
    fake_content_model().reset()
    yield
    fake_vault().reset()
    credentials.clear_cache()
    fake_content_model().reset()


@pytest.fixture
def assistant_on(db: None) -> AssistantSettings:
    row = AssistantSettings.objects.get()
    row.enabled = True
    row.save()
    return row
