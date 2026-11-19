"""factory-boy factories for the assistant models (call them as ``XFactory.create(...)``).

The settings singleton, the Gemini provider row and the five templates with their v1 are seeded rows
(``assistant.0002`` / ``post_migrate``): read them with ``AssistantSettings.objects.get()``,
``ProviderConfig.objects.get(provider="gemini")`` and ``PromptTemplate.objects.get(purpose=...)``.
"""

from factory.declarations import LazyAttribute, SubFactory
from factory.django import DjangoModelFactory

from glowderma.accounts.tests.factories import StaffUserFactory
from glowderma.assistant.models import GenerationJob, PromptTemplate, PromptVersion
from glowderma.catalog.tests.factories import ProductFactory
from glowderma.core import enums


class PromptVersionFactory(DjangoModelFactory[PromptVersion]):
    class Meta:
        model = PromptVersion

    template = LazyAttribute(lambda _: PromptTemplate.objects.get(purpose=enums.PromptPurpose.DESCRIBE))  # type: ignore[no-untyped-call]
    number = LazyAttribute(lambda obj: obj.template.versions.count() + 1)  # type: ignore[no-untyped-call]
    instructions = "Write calmly and clearly."
    status = enums.PromptVersionStatus.DRAFT


class GenerationJobFactory(DjangoModelFactory[GenerationJob]):
    class Meta:
        model = GenerationJob

    product = SubFactory(ProductFactory)  # type: ignore[no-untyped-call]
    created_by = SubFactory(StaffUserFactory)  # type: ignore[no-untyped-call]
    mode = enums.AssistantMode.FILL_MISSING
    languages = ["uz", "ru", "en"]
    provider = enums.AssistantProvider.GEMINI
    model_id = "gemini-2.5-flash-lite"
