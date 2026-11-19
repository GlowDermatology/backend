"""Transactional writes of the AI product content assistant: start, run, apply, discard.

* :func:`start_job` — the admin panel's "Generate": checks the switch, the provider, the person's daily
  limit and the day / month budgets, picks the target keys of the mode (``fill_missing`` only empty keys,
  ``languages`` only translations into empty keys, ``improve`` only the chosen non-empty keys, ``photos``
  the label fields), and queues one job per product (an active job is returned instead of a second one).
  Nothing to fill → no job.
* :func:`run_job` — the worker: claims the job, reads the key (``assistant.credentials``, never stored),
  composes the prompt, calls the content model (one retry on a malformed answer), validates the answer and
  stores the suggestions, the usage and the cost. The product is never touched.
* :func:`apply_job` — a person's selection (values may be edited; they are checked again) written through
  ``catalog.services.update_product`` with ``expected`` = the values seen at generation: a key changed
  meanwhile is a conflict and is not written.
* :func:`discard_job`, :func:`reap_stale_jobs`, :func:`start_test_job` (the ops console's prompt test).

Every job error is a stable ``error_code``; the message is rendered from :data:`ERROR_MESSAGES` when a
response is built. Logs carry ids, codes and counts only — never prompts, answers or the key.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from rest_framework import exceptions as drf_exceptions
from rest_framework.exceptions import NotFound, ValidationError

from glowderma.assistant import credentials, prompts, topics, validation
from glowderma.assistant.models import (
    ACTIVE_JOB_STATES,
    SETTINGS_PK,
    AssistantSettings,
    GenerationJob,
    PromptVersion,
    ProviderConfig,
)
from glowderma.catalog import selectors as catalog_selectors
from glowderma.catalog import services as catalog_services
from glowderma.catalog.models import Product, ProductImage
from glowderma.core import audit, enums, outbox, timeutils
from glowderma.core.api.exceptions import MESSAGES, Conflict, field_error
from glowderma.core.images import make_variants
from glowderma.core.media import variant_name
from glowderma.core.uzsource import N_
from glowderma.integrations.llm import registry as llm_registry
from glowderma.integrations.llm.ports import (
    CODE_BAD_OUTPUT,
    CODE_NOT_CONFIGURED,
    GenerationRequest,
    GenerationResult,
    ImagePart,
    LLMBadOutput,
    LLMError,
    LLMUnavailable,
)
from glowderma.integrations.secret import Secret
from glowderma.integrations.services import WORKER_LOST_CODE, WORKER_LOST_MESSAGE
from glowderma.integrations.vault.ports import VaultError, VaultUnavailable

logger = logging.getLogger(__name__)

LANGUAGES = catalog_services.LANGUAGES
TEXT_FIELDS = catalog_services.TEXT_FIELDS
SEO_FIELDS = catalog_services.SEO_FIELDS
# A running job whose worker sent no heartbeat for this long lost its worker (the task's hard limit is 120 s).
STALE_JOB_AFTER = timedelta(minutes=5)
MAX_ANSWER_ATTEMPTS = 2  # one retry on a malformed answer
# The provider calls of one run must end before the task's soft limit (tasks.RUN_SOFT_TIME_LIMIT = 90 s), with
# room left to save the result: each call's per-attempt timeout is cut to what the remaining budget allows
# after the SDK's retries and their back-off (at most RETRY_BACKOFF_SECONDS each).
RUN_BUDGET_SECONDS = 80
RETRY_BACKOFF_SECONDS = 8
MIN_ATTEMPT_SECONDS = 5


def _clock() -> float:
    return time.monotonic()


FAKE_KEY = "fake-key"  # AI_MODE=fake without a stored key (dev / tests / e2e): the fake ignores it
PER_MTOK = Decimal(1_000_000)
COST_QUANT = Decimal("0.000001")

# error codes of a failed job
VAULT_UNAVAILABLE_CODE = "vault_unavailable"
ASSISTANT_DISABLED_CODE = "assistant_disabled"
NOT_FOUND_CODE = "not_found"
SERVER_ERROR_CODE = "server_error"

ERROR_MESSAGES: dict[str, str] = {
    "provider_unavailable": MESSAGES["provider_unavailable"],
    CODE_NOT_CONFIGURED: N_("AI yordamchi hali sozlanmagan. Administratorga murojaat qiling."),
    "ai_refused": N_("AI bu so‘rovni bajarmadi. Ma’lumotlarni tekshirib, qayta urinib ko‘ring."),
    CODE_BAD_OUTPUT: N_("AI javobini tushunib bo‘lmadi. Qayta urinib ko‘ring."),
    VAULT_UNAVAILABLE_CODE: N_("AI xizmati vaqtincha ishlamayapti. Birozdan so‘ng qayta urinib ko‘ring."),
    ASSISTANT_DISABLED_CODE: N_("AI yordamchi o‘chirilgan."),
    WORKER_LOST_CODE: WORKER_LOST_MESSAGE,
    NOT_FOUND_CODE: MESSAGES["not_found"],
    SERVER_ERROR_CODE: MESSAGES["server_error"],
}

MODE_REQUIRED_FIELDS = N_("Yaxshilash uchun kamida bitta to‘ldirilgan maydonni tanlang.")
NO_IMAGES = N_("Mahsulotda rasm yo‘q.")
UNKNOWN_FIELD = N_("Bu maydonni AI to‘ldira olmaydi.")
TEST_MODE_REFUSED = N_("Bu rejim faqat boshqaruv konsolida ishlatiladi.")
VALUE_REJECTED = N_("Mahsulot ma’lumotlarida yo‘q raqam yoki da’vo bor: matnni tekshiring.")
NOT_SUGGESTED = N_("Bu maydon uchun taklif yo‘q.")


class AssistantDisabled(Conflict):
    default_code = ASSISTANT_DISABLED_CODE
    default_detail = ERROR_MESSAGES[ASSISTANT_DISABLED_CODE]


class AssistantNotConfigured(Conflict):
    default_code = CODE_NOT_CONFIGURED
    default_detail = ERROR_MESSAGES[CODE_NOT_CONFIGURED]


class BudgetExceeded(Conflict):
    default_code = "assistant_budget_exceeded"
    default_detail = N_("AI uchun ajratilgan byudjet tugadi. Administratorga murojaat qiling.")


class DailyLimitReached(drf_exceptions.APIException):
    status_code = 429
    default_code = "assistant_daily_limit"
    default_detail = N_("Bugungi AI so‘rovlari chegarasiga yetdingiz. Ertaga qayta urinib ko‘ring.")


class JobNotReviewable(Conflict):
    default_code = "assistant_job_not_ready"
    default_detail = N_("Bu takliflar hali tayyor emas yoki allaqachon ko‘rib chiqilgan.")


def error_message(code: str) -> str:
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[SERVER_ERROR_CODE])


# --- configuration ------------------------------------------------------------------------------------------


def get_settings() -> AssistantSettings:
    row, _ = AssistantSettings.objects.get_or_create(pk=SETTINGS_PK)
    return row


def get_provider(assistant: AssistantSettings) -> ProviderConfig | None:
    return ProviderConfig.objects.filter(provider=assistant.active_provider).first()


def provider_ready(config: ProviderConfig | None) -> bool:
    """Enabled, and a key is active (with the fake content model no key is needed)."""
    if config is None or not config.enabled:
        return False
    return config.configured or settings.AI_MODE == "fake"


def is_available() -> bool:
    """Whether the admin panel shows the assistant: switched on and its provider ready."""
    assistant = get_settings()
    return assistant.enabled and provider_ready(get_provider(assistant))


def _resolve_key(config: ProviderConfig) -> Secret:
    """The active key of ``config`` from Vault (cached per ``(provider, active_version)`` by
    ``assistant.credentials``); a placeholder for the fake content model when no key is stored."""
    if config.active_version is None:
        if settings.AI_MODE == "fake":
            return Secret(FAKE_KEY)
        raise AssistantNotConfigured()
    key: Secret = credentials.resolve_active_key(config)
    return key


# --- usage and limits ---------------------------------------------------------------------------------------


def _spent(**bounds: Any) -> Decimal:
    total = GenerationJob.objects.filter(**bounds).aggregate(total=Sum("cost_usd"))["total"]
    return Decimal(total or 0)


def spent_today(now: Any = None) -> Decimal:
    start, end = timeutils.day_bounds(timeutils.local_date(now or timeutils.now()))
    return _spent(created_at__gte=start, created_at__lt=end)


def spent_this_month(now: Any = None) -> Decimal:
    instant = now or timeutils.now()
    start, _ = timeutils.day_bounds(timeutils.month_start(timeutils.local_date(instant)))
    return _spent(created_at__gte=start)


def jobs_today(actor: Any, now: Any = None) -> int:
    start, end = timeutils.day_bounds(timeutils.local_date(now or timeutils.now()))
    return (
        GenerationJob.objects.filter(created_by=actor, created_at__gte=start, created_at__lt=end)
        .exclude(mode=enums.AssistantMode.TEST)
        .count()
    )


def _check_budgets(assistant: AssistantSettings) -> None:
    if spent_today() >= assistant.daily_budget_usd or spent_this_month() >= assistant.monthly_budget_usd:
        raise BudgetExceeded()


def cost_of(config: ProviderConfig, input_tokens: int, output_tokens: int) -> Decimal:
    cost = (
        Decimal(input_tokens) * config.price_input_per_mtok_usd
        + Decimal(output_tokens) * config.price_output_per_mtok_usd
    ) / PER_MTOK
    return cost.quantize(COST_QUANT, rounding=ROUND_HALF_UP)


# --- target selection ---------------------------------------------------------------------------------------


def _text_key(language: str, name: str) -> str:
    return f"texts.{language}.{name}"


def _universe(languages: Sequence[str], image_ids: Iterable[int]) -> list[str]:
    """Every key the assistant may write, in display order (language, then field)."""
    keys: list[str] = []
    images = list(image_ids)
    for language in languages:
        keys += [_text_key(language, name) for name in TEXT_FIELDS]
        keys += [f"seo.{language}.{leaf}" for leaf in SEO_FIELDS]
        keys += [f"images.{image}.alt.{language}" for image in images]
    return keys


def _in_language(key: str, language: str) -> str:
    """``key`` in another language (``texts.uz.name`` → ``texts.ru.name``, ``images.7.alt.uz`` → ``….ru``)."""
    parts = key.split(".")
    parts[3 if parts[0] == "images" else 1] = language
    return ".".join(parts)


def _translatable(key: str, snapshot: dict[str, Any]) -> bool:
    """The same field is filled in another language (a source to translate from)."""
    language = prompts.key_language(key)
    return any(snapshot.get(_in_language(key, other)) for other in LANGUAGES if other != language)


def select_targets(
    mode: str,
    snapshot: dict[str, Any],
    *,
    languages: Sequence[str],
    fields: Sequence[str],
    image_ids: Sequence[int],
) -> list[str]:
    """The keys a job of ``mode`` asks for (see the module docstring); ``fields`` narrows the automatic
    choice (``fill_missing`` / ``languages`` / ``photos``) or is the choice (``improve``)."""
    all_image_ids = sorted(
        {int(key.split(".")[1]) for key in snapshot if key.startswith("images.")},
    )
    universe = _universe(languages, all_image_ids)
    chosen = set(fields)
    if mode == enums.AssistantMode.IMPROVE:
        return [key for key in universe if key in chosen and snapshot.get(key)]
    if mode == enums.AssistantMode.PHOTOS:
        label_fields = ("name", "ingredients", "usage")
        keys = [_text_key(language, name) for language in languages for name in label_fields]
        return [key for key in keys if not chosen or key in chosen]
    targets: list[str] = []
    has_name = any(snapshot.get(_text_key(language, "name")) for language in LANGUAGES)
    for key in universe:
        if snapshot.get(key) or (chosen and key not in chosen):
            continue
        field = key.split(".")[2] if key.startswith("texts.") else ""
        if mode == enums.AssistantMode.LANGUAGES or field in ("name", "ingredients", "usage"):
            # translate only: these are never written from nothing
            if _translatable(key, snapshot):
                targets.append(key)
        elif has_name:
            targets.append(key)
    return targets


def _validate_fields(fields: Sequence[str]) -> None:
    errors = [(index, key) for index, key in enumerate(fields) if validation.parse_key(key) is None]
    if errors:
        raise ValidationError(
            {f"keys.{index}": [field_error("invalid", UNKNOWN_FIELD)] for index, _key in errors}
        )


def _product_images(product: Product) -> list[ProductImage]:
    return list(product.images.order_by("position", "id"))


def _pick_images(images: Sequence[ProductImage], image_ids: Sequence[int], limit: int) -> list[int]:
    own = [image.pk for image in images]
    if image_ids:
        unknown = [i for i in image_ids if i not in own]
        if unknown:
            raise ValidationError({"image_ids": [field_error("invalid")]})
        own = [i for i in own if i in set(image_ids)]
    if not own:
        raise ValidationError({"image_ids": [field_error("invalid", NO_IMAGES)]})
    return own[:limit]


# --- start --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StartResult:
    job: GenerationJob | None  # None = nothing to fill
    created: bool


def _active_job(product_id: int) -> GenerationJob | None:
    return (
        GenerationJob.objects.select_related("created_by")
        .filter(product_id=product_id, state__in=ACTIVE_JOB_STATES)
        .exclude(mode=enums.AssistantMode.TEST)
        .first()
    )


def start_job(
    product_id: int,
    *,
    mode: str,
    languages: Sequence[str],
    fields: Sequence[str] = (),
    goal: str = "",
    image_ids: Sequence[int] = (),
    actor: Any,
) -> StartResult:
    """Queue a generation for the product (``created``) or return its active job; ``job=None`` when the
    mode finds nothing to do."""
    if mode == enums.AssistantMode.TEST:
        raise ValidationError({"mode": [field_error("invalid_choice", TEST_MODE_REFUSED)]})
    assistant = get_settings()
    if not assistant.enabled:
        raise AssistantDisabled()
    config = get_provider(assistant)
    if config is None or not provider_ready(config):
        raise AssistantNotConfigured()
    _validate_fields(fields)
    product = catalog_selectors.get_product(product_id)
    if product is None:
        raise NotFound
    images = _product_images(product)
    snapshot = catalog_services.product_snapshot(product, images)
    chosen_images: list[int] = []
    if mode == enums.AssistantMode.PHOTOS:
        chosen_images = _pick_images(images, image_ids, assistant.max_images)
    if mode == enums.AssistantMode.IMPROVE and not fields:
        raise ValidationError({"keys": [field_error("required", MODE_REQUIRED_FIELDS)]})
    targets = select_targets(mode, snapshot, languages=languages, fields=fields, image_ids=chosen_images)
    if not targets:
        if mode == enums.AssistantMode.IMPROVE:
            raise ValidationError({"keys": [field_error("invalid", MODE_REQUIRED_FIELDS)]})
        return StartResult(job=None, created=False)
    active = _active_job(product.pk)
    if active is not None:
        return StartResult(job=active, created=False)
    if jobs_today(actor) >= assistant.per_user_daily_jobs:
        raise DailyLimitReached()
    _check_budgets(assistant)
    try:
        with transaction.atomic():
            job = GenerationJob.objects.create(
                product=product,
                created_by=actor,
                mode=mode,
                languages=list(languages),
                fields=targets,
                goal=goal if mode == enums.AssistantMode.IMPROVE else "",
                image_ids=chosen_images,
                input_snapshot={key: snapshot.get(key) for key in targets},
                provider=config.provider,
                model_id=config.model_id,
                rules_version=prompts.RULES_VERSION,
            )
            _announce(job)
    except IntegrityError:  # a concurrent start for the same product won the one-active-job slot
        active = _active_job(product.pk)
        if active is None:  # pragma: no cover - it finished between the two statements
            raise
        return StartResult(job=active, created=False)
    logger.info(
        "assistant job %s queued (product %s, mode %s, %s keys)", job.pk, product.pk, mode, len(targets)
    )
    return StartResult(job=job, created=True)


def _announce(job: GenerationJob) -> None:
    outbox.enqueue(topics.JOB_QUEUED, {"job_id": job.pk}, dedupe_key=f"{topics.JOB_QUEUED}:{job.pk}")


# The ops console's prompt test: the purpose's task on a sample product, with the tested version.
TEST_TASKS: dict[str, str] = {
    enums.PromptPurpose.DESCRIBE: enums.AssistantMode.FILL_MISSING,
    enums.PromptPurpose.TRANSLATE: enums.AssistantMode.LANGUAGES,
    enums.PromptPurpose.IMPROVE: enums.AssistantMode.IMPROVE,
    enums.PromptPurpose.SEO: enums.AssistantMode.FILL_MISSING,
    enums.PromptPurpose.IMAGE_EXTRACT: enums.AssistantMode.PHOTOS,
}


def _test_targets(purpose: str, snapshot: dict[str, Any], image_ids: Sequence[int]) -> list[str]:
    if purpose == enums.PromptPurpose.SEO:
        return [f"seo.{language}.{leaf}" for language in LANGUAGES for leaf in SEO_FIELDS]
    if purpose == enums.PromptPurpose.DESCRIBE:
        return [
            _text_key(language, name)
            for language in LANGUAGES
            for name in ("short_description", "description")
        ]
    if purpose == enums.PromptPurpose.TRANSLATE:
        source = next(
            (language for language in LANGUAGES if snapshot.get(_text_key(language, "description"))), "uz"
        )
        return [
            _text_key(language, name)
            for language in LANGUAGES
            if language != source
            for name in ("name", "short_description", "description")
        ]
    if purpose == enums.PromptPurpose.IMPROVE:
        return [key for key in _universe(LANGUAGES, ()) if key.startswith("texts.") and snapshot.get(key)]
    return [_text_key(language, name) for language in LANGUAGES for name in ("name", "ingredients", "usage")]


def start_prompt_test(
    *, version: PromptVersion, product_id: int, actor: Any, request: Any = None
) -> GenerationJob:
    """Queue an ops-console test of ``version`` (any status, a draft included) on a sample product: the run
    uses exactly that version for its purpose's task; the job counts towards the budgets (not the daily
    limit or the one-active-job rule) and is never applied. The console reads the outcome from the job
    (``prompt_versions`` names the version)."""
    assistant = get_settings()
    config = get_provider(assistant)
    if config is None or not provider_ready(config):
        raise AssistantNotConfigured()
    product = catalog_selectors.get_product(product_id)
    if product is None:
        raise NotFound
    images = _product_images(product)
    snapshot = catalog_services.product_snapshot(product, images)
    purpose = version.template.purpose
    chosen_images: list[int] = []
    if purpose == enums.PromptPurpose.IMAGE_EXTRACT:
        chosen_images = _pick_images(images, (), assistant.max_images)
    targets = _test_targets(purpose, snapshot, chosen_images)
    if not targets:
        raise ValidationError({"product": [field_error("invalid", MODE_REQUIRED_FIELDS)]})
    _check_budgets(assistant)
    with transaction.atomic():
        job = GenerationJob.objects.create(
            product=product,
            created_by=actor,
            mode=enums.AssistantMode.TEST,
            languages=list(LANGUAGES),
            fields=targets,
            goal=enums.AssistantImproveGoal.PROFESSIONAL if purpose == enums.PromptPurpose.IMPROVE else "",
            image_ids=chosen_images,
            input_snapshot={key: snapshot.get(key) for key in targets},
            provider=config.provider,
            model_id=config.model_id,
            prompt_versions={purpose: version.pk},
            rules_version=prompts.RULES_VERSION,
        )
        _announce(job)
    return job


# --- run (worker) -------------------------------------------------------------------------------------------


def _is_stale(job: GenerationJob, instant: Any) -> bool:
    reference = job.heartbeat_at or job.started_at or job.created_at
    return bool(reference < instant - STALE_JOB_AFTER)


def claim_job(job_id: int) -> GenerationJob | None:
    """``queued`` → ``running`` (or re-claim a running job whose worker was lost); ``None`` otherwise (a
    duplicate delivery while the first run is alive, or a finished job)."""
    with transaction.atomic():
        job = GenerationJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        instant = timeutils.now()
        if job.state == enums.JobState.QUEUED:
            job.started_at = instant
        elif job.state != enums.JobState.RUNNING or not _is_stale(job, instant):
            return None
        job.state = enums.JobState.RUNNING
        job.heartbeat_at = instant
        job.attempts += 1
        job.save(update_fields=["state", "started_at", "heartbeat_at", "attempts", "updated_at"])
        return job


def heartbeat(job: GenerationJob) -> None:
    GenerationJob.objects.filter(pk=job.pk, state=enums.JobState.RUNNING).update(heartbeat_at=timeutils.now())


def _finish(job_id: int, **values: Any) -> GenerationJob | None:
    """Write the final state of a running job under its row lock (``None`` if it is no longer running)."""
    with transaction.atomic():
        job = GenerationJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None or job.state != enums.JobState.RUNNING:
            return None
        instant = timeutils.now()
        for name, value in values.items():
            setattr(job, name, value)
        job.finished_at = instant
        job.heartbeat_at = instant
        job.save()
        return job


def fail_job(
    job: GenerationJob,
    code: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    config: ProviderConfig | None = None,
) -> GenerationJob | None:
    cost = cost_of(config, input_tokens, output_tokens) if config is not None else Decimal(0)
    logger.warning("assistant job %s failed: %s", job.pk, code)
    return _finish(
        job.pk,
        state=enums.JobState.FAILED,
        error_code=code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
    )


def _guidance(job: GenerationJob, purposes: Sequence[str]) -> tuple[list[prompts.Guidance], dict[str, int]]:
    """The style guidance of ``purposes``: the version a test job names, else each template's active one."""
    preset = {purpose: int(pk) for purpose, pk in (job.prompt_versions or {}).items()}
    guidance: list[prompts.Guidance] = []
    used: dict[str, int] = {}
    for purpose in purposes:
        query = PromptVersion.objects.select_related("template").filter(template__purpose=purpose)
        version = (
            query.filter(pk=preset[purpose]).first()
            if purpose in preset
            else query.filter(status=enums.PromptVersionStatus.ACTIVE).first()
        )
        if version is None:
            continue  # no active guidance: the app rules alone still apply
        guidance.append(prompts.Guidance(purpose=purpose, instructions=version.instructions))
        used[purpose] = version.pk
    return guidance, used


def _task_mode(job: GenerationJob) -> str:
    if job.mode != enums.AssistantMode.TEST:
        return job.mode
    purpose = next(iter(job.prompt_versions), enums.PromptPurpose.DESCRIBE)
    return TEST_TASKS.get(purpose, enums.AssistantMode.FILL_MISSING)


def _purposes(job: GenerationJob) -> tuple[str, ...]:
    if job.mode == enums.AssistantMode.TEST:
        return tuple(job.prompt_versions)
    return prompts.MODE_PURPOSES[job.mode]


def image_part(image: ProductImage) -> ImagePart:
    """The 1000 px WebP copy of a product photo; the original resized in memory when it is missing."""
    storage = image.file.storage
    name = str(image.file.name)
    try:
        with storage.open(variant_name(name, "medium")) as handle:
            return ImagePart(data=handle.read(), mime_type="image/webp")
    except OSError, ValueError:
        with storage.open(name) as handle:
            original = handle.read()
        return ImagePart(data=make_variants(original)["medium"], mime_type="image/webp")


def _limits(targets: Sequence[str]) -> dict[str, int]:
    return {key: validation.max_length(key) for key in targets}


def _generate(
    model: Any,
    request: GenerationRequest,
    key: Secret,
    targets: Sequence[str],
    usage: list[int],
    deadline: float | None = None,
) -> tuple[dict[str, str], int, dict[str, Any]]:
    """Call the model; a malformed answer is retried once. ``usage`` accumulates [input, output] tokens,
    including what the provider billed for an answer it refused or cut off (carried on the error)."""
    for attempt in range(1, MAX_ANSWER_ATTEMPTS + 1):
        if deadline is not None:
            left = deadline - _clock() - request.max_retries * RETRY_BACKOFF_SECONDS
            per_attempt = int(left // (1 + request.max_retries))
            if per_attempt < MIN_ATTEMPT_SECONDS:
                raise LLMUnavailable("run time budget spent")
            request = replace(request, timeout_seconds=min(request.timeout_seconds, per_attempt))
        try:
            result: GenerationResult = model.generate(request, api_key=key)
        except LLMError as exc:
            usage[0] += exc.input_tokens
            usage[1] += exc.output_tokens
            if isinstance(exc, LLMBadOutput) and attempt < MAX_ANSWER_ATTEMPTS:
                continue
            raise
        usage[0] += result.input_tokens
        usage[1] += result.output_tokens
        try:
            values, dropped = validation.parse_answer(result.data, targets)
        except validation.BadAnswer:
            if attempt == MAX_ANSWER_ATTEMPTS:
                raise LLMBadOutput("answer shape") from None
            continue
        return values, dropped, result.data
    raise LLMBadOutput("answer shape")  # pragma: no cover - the loop returns or raises


def run_job(job_id: int) -> GenerationJob | None:
    """The worker's whole run of a job (idempotent per delivery: an alive or finished job is skipped)."""
    deadline = _clock() + RUN_BUDGET_SECONDS
    job = claim_job(job_id)
    if job is None:
        return None
    assistant = get_settings()
    config = get_provider(assistant)
    usage = [0, 0]
    try:
        if job.mode != enums.AssistantMode.TEST and not assistant.enabled:
            return fail_job(job, ASSISTANT_DISABLED_CODE)
        if config is None or not provider_ready(config):
            return fail_job(job, CODE_NOT_CONFIGURED)
        product = (
            Product.objects.select_related("category").filter(pk=job.product_id).first()
            if job.product_id is not None
            else None
        )
        if product is None:
            return fail_job(job, NOT_FOUND_CODE)
        images = _product_images(product)
        targets = [key for key in job.fields if validation.parse_key(key) is not None]
        mode = _task_mode(job)
        guidance, used = _guidance(job, _purposes(job))
        GenerationJob.objects.filter(pk=job.pk).update(prompt_versions=used, model_id=config.model_id)
        data = prompts.product_data(product, images)
        parts = tuple(image_part(image) for image in images if image.pk in set(job.image_ids))
        request = GenerationRequest(
            model=config.model_id,
            system_instruction=prompts.system_instruction(guidance),
            prompt=prompts.user_prompt(
                mode=mode,
                targets=targets,
                limits=_limits(targets),
                data=data,
                goal=job.goal,
                image_count=len(parts),
            ),
            response_schema=prompts.response_schema(targets, mode=mode),
            images=parts,
            temperature=float(config.temperature),
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
        )
        key = _resolve_key(config)
        model = llm_registry.get_content_model(config.provider)
        heartbeat(job)
        values, dropped, answer = _generate(model, request, key, targets, usage, deadline)
        suggestions, warnings = validation.build_suggestions(
            values, targets=targets, snapshot=job.input_snapshot, facts=validation.Facts.of(data), mode=mode
        )
        if dropped:
            warnings.append({"code": "dropped", "count": dropped})
        if mode == enums.AssistantMode.PHOTOS and (info := validation.extracted_info(answer)):
            warnings.append({"code": "extracted", **info})
        finished = _finish(
            job.pk,
            state=enums.JobState.SUCCEEDED,
            suggestions=suggestions,
            warnings=warnings,
            input_tokens=usage[0],
            output_tokens=usage[1],
            cost_usd=cost_of(config, usage[0], usage[1]),
        )
        logger.info(
            "assistant job %s succeeded: %s suggestions, %s rejected, tokens %s/%s",
            job.pk,
            len(suggestions),
            len(warnings),
            usage[0],
            usage[1],
        )
        return finished
    except AssistantNotConfigured, credentials.KeyUnavailable:
        return fail_job(job, CODE_NOT_CONFIGURED, config=config)
    except LLMError as exc:
        return fail_job(job, exc.code, input_tokens=usage[0], output_tokens=usage[1], config=config)
    except VaultUnavailable:
        return fail_job(job, VAULT_UNAVAILABLE_CODE, config=config)
    except VaultError:  # not found / not configured: the stored version is unusable
        return fail_job(job, CODE_NOT_CONFIGURED, config=config)


def reap_stale_jobs(*, now: Any = None) -> list[int]:
    """Fail running jobs without a heartbeat for :data:`STALE_JOB_AFTER`, and jobs still queued after it,
    with ``worker_lost`` (the product's one-active-job slot is freed); their ids."""
    instant = now or timeutils.now()
    cutoff = instant - STALE_JOB_AFTER
    candidates = GenerationJob.objects.filter(
        Q(state=enums.JobState.RUNNING, heartbeat_at__lt=cutoff)
        | Q(state=enums.JobState.QUEUED, created_at__lt=cutoff)
    ).values_list("pk", flat=True)
    reaped: list[int] = []
    for job_id in list(candidates):
        with transaction.atomic():
            job = GenerationJob.objects.select_for_update().filter(pk=job_id).first()
            if job is None or job.state not in ACTIVE_JOB_STATES:
                continue
            if job.state == enums.JobState.RUNNING and not _is_stale(job, instant):
                continue
            if job.state == enums.JobState.QUEUED and job.created_at >= cutoff:  # pragma: no cover - race
                continue
            job.state = enums.JobState.FAILED
            job.error_code = WORKER_LOST_CODE
            job.started_at = job.started_at or instant
            job.finished_at = instant
            job.save()
        logger.warning("assistant job %s reaped (worker lost)", job_id)
        reaped.append(job_id)
    return reaped


# --- review -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyItem:
    key: str
    value: str


@dataclass(frozen=True)
class ApplyResult:
    job: GenerationJob
    product: Product
    applied: list[str]
    conflicts: list[str]


def _locked_reviewable(job_id: int) -> GenerationJob:
    job = (
        GenerationJob.objects.select_for_update()
        .filter(pk=job_id)
        .exclude(mode=enums.AssistantMode.TEST)
        .first()
    )
    if job is None:
        raise NotFound
    if job.state != enums.JobState.SUCCEEDED or job.outcome != enums.AssistantJobOutcome.PENDING:
        raise JobNotReviewable()
    return job


def _product_input(items: Sequence[ApplyItem], snapshot: dict[str, Any]) -> catalog_services.ProductInput:
    texts: dict[str, dict[str, str]] = {}
    seo: dict[str, dict[str, str]] = {}
    image_alt: dict[int, dict[str, str]] = {}
    for item in items:
        kind, language, name, image_id = validation.parse_key(item.key)  # type: ignore[misc]
        if kind == "texts":
            texts.setdefault(language, {})[name] = item.value
        elif kind == "seo":
            seo.setdefault(language, {})[name] = item.value
        elif image_id is not None:
            image_alt.setdefault(image_id, {})[language] = item.value
    return catalog_services.ProductInput(
        texts=texts or None,
        seo=seo or None,
        image_alt=image_alt or None,
        expected={item.key: snapshot.get(item.key) for item in items},
    )


def _checked_items(job: GenerationJob, items: Sequence[ApplyItem], product: Product) -> list[ApplyItem]:
    """Each item must be one of the job's suggestions; an edited value is normalised and linted again."""
    suggested = {s["key"] for s in job.suggestions}
    facts = validation.Facts.of(prompts.product_data(product, _product_images(product)))
    errors: dict[str, list[Any]] = {}
    checked: list[ApplyItem] = []
    mode = _task_mode(job)
    for index, item in enumerate(items):
        if item.key not in suggested:
            errors[f"items.{index}.key"] = [field_error("invalid", NOT_SUGGESTED)]
            continue
        value = validation.normalize(item.key, item.value)
        verdict = validation.check(item.key, value, facts, mode=mode)
        if verdict.rejected == validation.R_TOO_LONG:
            errors[f"items.{index}.value"] = [
                field_error("max_length", limit=validation.max_length(item.key))
            ]
        elif verdict.rejected == validation.R_EMPTY:
            errors[f"items.{index}.value"] = [field_error("blank")]
        elif verdict.rejected:
            errors[f"items.{index}.value"] = [field_error("invalid", VALUE_REJECTED)]
        checked.append(ApplyItem(key=item.key, value=value))
    if errors:
        raise ValidationError(errors)
    return checked


def apply_job(job_id: int, items: Sequence[ApplyItem], *, actor: Any, request: Any = None) -> ApplyResult:
    """Write the selected suggestions; keys whose product value changed since generation are conflicts."""
    with transaction.atomic():
        job = _locked_reviewable(job_id)
        product = catalog_selectors.get_product(job.product_id) if job.product_id else None
        if product is None:
            raise NotFound
        unique = list({item.key: item for item in items}.values())
        checked = _checked_items(job, unique, product)
        conflicts: list[str] = []
        pending = checked
        for _round in range(2):  # one retry without the keys that changed meanwhile
            if not pending:
                break
            try:
                product = catalog_services.update_product(
                    product.pk, _product_input(pending, job.input_snapshot), actor=actor, request=request
                )
            except catalog_services.ProductChanged as exc:
                changed = set(exc.keys)
                conflicts += [item.key for item in pending if item.key in changed]
                pending = [item for item in pending if item.key not in changed]
                if _round == 1:
                    conflicts += [item.key for item in pending]
                    pending = []
                continue
            break
        applied = [item.key for item in pending]
        if applied:
            job.outcome = (
                enums.AssistantJobOutcome.PARTIAL if conflicts else enums.AssistantJobOutcome.APPLIED
            )
            job.applied_keys = applied
            job.applied_at = timeutils.now()
            job.applied_by = actor
            job.save(update_fields=["outcome", "applied_keys", "applied_at", "applied_by", "updated_at"])
        audit.record(
            action="assistant.suggestions_applied",
            entity_type="product",
            entity_id=str(product.pk),
            actor=actor,
            request=request,
            after={"job_id": job.pk, "applied": applied, "conflicts": conflicts},
        )
        if not applied:
            product = catalog_selectors.get_product(product.pk) or product
    return ApplyResult(job=job, product=product, applied=applied, conflicts=conflicts)


def discard_job(job_id: int, *, actor: Any, request: Any = None) -> GenerationJob:
    with transaction.atomic():
        job = _locked_reviewable(job_id)
        job.outcome = enums.AssistantJobOutcome.DISCARDED
        job.save(update_fields=["outcome", "updated_at"])
        audit.record(
            action="assistant.suggestions_discarded",
            entity_type="product",
            entity_id=str(job.product_id),
            actor=actor,
            request=request,
            after={"job_id": job.pk, "suggestions": len(job.suggestions)},
        )
    return job
