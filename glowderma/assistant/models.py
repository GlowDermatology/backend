"""Tables of the AI product content assistant.

* :class:`AssistantSettings` — the singleton (pk 1) the superusers edit in the ops console: on/off, budgets,
  per-user limit, image limit, the active provider.
* :class:`ProviderConfig` — one row per provider (``gemini``): model and generation parameters, prices, and
  the **metadata** of its API key in Vault KV v2 (path, active / previous / pending version, rotation and
  test status). There is no key column, ever: the key lives only in Vault (``integrations.vault``).
* :class:`PromptTemplate` / :class:`PromptVersion` — the five editable style-guidance templates and their
  numbered versions (draft → tested → active → retired; one active version per template). The mandatory app
  rules are code (``assistant.prompts``), never rows.
* :class:`GenerationJob` — one generation (admin panel jobs, or a console prompt test with mode ``test``):
  its request, input snapshot, suggestions, usage, cost and outcome. Suggestions never touch the product
  until a person applies them. Usage and cost reports are aggregates over these rows.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q

from glowderma.core import enums
from glowderma.core.models import TimeStampedModel

SETTINGS_PK = 1
DEFAULT_MODEL_ID = "gemini-2.5-flash-lite"
ACTIVE_JOB_STATES = (enums.JobState.QUEUED, enums.JobState.RUNNING)


class AssistantSettings(models.Model):
    """The singleton (pk 1): created by ``assistant.0002`` and re-ensured after ``migrate`` / ``flush``."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=SETTINGS_PK)
    enabled = models.BooleanField(default=False, verbose_name="yoqilgan")
    active_provider = models.CharField(
        max_length=32,
        choices=enums.AssistantProvider.choices,
        default=enums.AssistantProvider.GEMINI,
        verbose_name="faol provayder",
    )
    daily_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("5"), verbose_name="kunlik byudjet (USD)"
    )
    monthly_budget_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("50"), verbose_name="oylik byudjet (USD)"
    )
    per_user_daily_jobs = models.PositiveIntegerField(
        default=50, verbose_name="bir xodim uchun kunlik so‘rovlar"
    )
    max_images = models.PositiveSmallIntegerField(default=3, verbose_name="bir so‘rovdagi rasmlar soni")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="yangilangan")
    updated_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yangilagan",
    )

    class Meta:
        verbose_name = "AI yordamchi sozlamalari"
        verbose_name_plural = "AI yordamchi sozlamalari"
        constraints = [
            models.CheckConstraint(condition=Q(id=SETTINGS_PK), name="assistant_settings_singleton"),
            models.CheckConstraint(
                condition=Q(daily_budget_usd__gte=0, monthly_budget_usd__gte=0),
                name="assistant_settings_budgets_non_negative",
            ),
            models.CheckConstraint(condition=Q(max_images__lte=8), name="assistant_settings_max_images_8"),
        ]

    def __str__(self) -> str:
        return str(self._meta.verbose_name)


class ProviderConfig(TimeStampedModel):
    """A provider's model settings and its key's Vault metadata (no key column)."""

    provider = models.CharField(
        max_length=32, unique=True, choices=enums.AssistantProvider.choices, verbose_name="provayder"
    )
    enabled = models.BooleanField(default=True, verbose_name="yoqilgan")
    model_id = models.CharField(max_length=100, default=DEFAULT_MODEL_ID, verbose_name="model")
    temperature = models.DecimalField(
        max_digits=3, decimal_places=2, default=Decimal("0.40"), verbose_name="temperatura"
    )
    max_output_tokens = models.PositiveIntegerField(default=4096, verbose_name="javob tokenlari chegarasi")
    timeout_seconds = models.PositiveSmallIntegerField(default=20, verbose_name="kutish vaqti (soniya)")
    max_retries = models.PositiveSmallIntegerField(default=2, verbose_name="qayta urinishlar")
    price_input_per_mtok_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.10"), verbose_name="kirish narxi (1M token, USD)"
    )
    price_output_per_mtok_usd = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.40"), verbose_name="chiqish narxi (1M token, USD)"
    )
    # --- the API key's Vault KV v2 metadata (the key itself is only in Vault) ---
    # relative to settings.VAULT_AI_PREFIX: <VAULT_KV_MOUNT>/data/<VAULT_AI_PREFIX>/<vault_path>
    vault_path = models.CharField(max_length=100, verbose_name="Vault yo‘li")
    active_version = models.PositiveIntegerField(null=True, blank=True, verbose_name="faol kalit versiyasi")
    previous_version = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="oldingi kalit versiyasi"
    )
    # a new key written to Vault and being tested by a worker; becomes active only when the test succeeds
    pending_version = models.PositiveIntegerField(
        null=True, blank=True, verbose_name="tekshirilayotgan kalit versiyasi"
    )
    rotated_at = models.DateTimeField(null=True, blank=True, verbose_name="kalit almashtirilgan")
    rotated_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="kalitni almashtirgan",
    )
    test_status = models.CharField(
        max_length=16,
        choices=enums.CredentialTestStatus.choices,
        default=enums.CredentialTestStatus.UNKNOWN,
        verbose_name="tekshiruv holati",
    )
    tested_at = models.DateTimeField(null=True, blank=True, verbose_name="tekshirilgan")
    test_error_code = models.CharField(max_length=40, blank=True, default="", verbose_name="tekshiruv xatosi")
    updated_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yangilagan",
    )

    class Meta:
        verbose_name = "AI provayder"
        verbose_name_plural = "AI provayderlar"
        constraints = [
            models.CheckConstraint(
                condition=Q(temperature__gte=0, temperature__lte=2), name="assistant_provider_temperature"
            ),
            models.CheckConstraint(
                condition=Q(max_output_tokens__gte=256, max_output_tokens__lte=65536),
                name="assistant_provider_max_output_tokens",
            ),
            models.CheckConstraint(
                condition=Q(timeout_seconds__gte=5, timeout_seconds__lte=20),
                name="assistant_provider_timeout",
            ),
            models.CheckConstraint(condition=Q(max_retries__lte=2), name="assistant_provider_max_retries"),
            models.CheckConstraint(
                condition=Q(price_input_per_mtok_usd__gte=0, price_output_per_mtok_usd__gte=0),
                name="assistant_provider_prices_non_negative",
            ),
            models.CheckConstraint(
                condition=Q(active_version__isnull=True)
                | Q(previous_version__isnull=True)
                | ~Q(active_version=models.F("previous_version")),
                name="assistant_provider_previous_differs",
            ),
            models.CheckConstraint(condition=~Q(vault_path=""), name="assistant_provider_vault_path"),
        ]

    def __str__(self) -> str:
        return self.get_provider_display()

    @property
    def configured(self) -> bool:
        """Whether a tested key version is active."""
        return self.active_version is not None


class PromptTemplate(TimeStampedModel):
    """One editable style-guidance template per purpose (five rows, ensured)."""

    purpose = models.CharField(
        max_length=20, unique=True, choices=enums.PromptPurpose.choices, verbose_name="vazifa"
    )

    class Meta:
        verbose_name = "prompt shabloni"
        verbose_name_plural = "prompt shablonlari"
        ordering = ["id"]

    def __str__(self) -> str:
        return self.get_purpose_display()


class PromptVersion(models.Model):
    """A numbered version of a template's instructions; only a draft is edited, only a tested (or a retired,
    for a rollback) version is activated, and one version per template is active."""

    template = models.ForeignKey(
        "assistant.PromptTemplate", on_delete=models.PROTECT, related_name="versions", verbose_name="shablon"
    )
    number = models.PositiveIntegerField(verbose_name="versiya")
    instructions = models.TextField(verbose_name="ko‘rsatmalar")
    status = models.CharField(
        max_length=16,
        choices=enums.PromptVersionStatus.choices,
        default=enums.PromptVersionStatus.DRAFT,
        verbose_name="holat",
    )
    # the latest console test: {job_id, at, by, warnings, suggestions_count, ...} — sanitized output, no key
    last_test = models.JSONField(default=dict, blank=True, verbose_name="oxirgi sinov")
    tested_at = models.DateTimeField(null=True, blank=True, verbose_name="sinovdan o‘tgan")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="yaratilgan")
    created_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="yaratgan",
    )
    updated_at = models.DateTimeField(auto_now=True, verbose_name="yangilangan")
    activated_at = models.DateTimeField(null=True, blank=True, verbose_name="faollashtirilgan")
    activated_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="faollashtirgan",
    )

    class Meta:
        verbose_name = "prompt versiyasi"
        verbose_name_plural = "prompt versiyalari"
        ordering = ["template_id", "-number"]
        constraints = [
            models.UniqueConstraint(fields=["template", "number"], name="assistant_prompt_version_number"),
            models.UniqueConstraint(
                fields=["template"],
                condition=Q(status=enums.PromptVersionStatus.ACTIVE),
                name="assistant_prompt_one_active_version",
            ),
            models.CheckConstraint(
                condition=Q(number__gte=1), name="assistant_prompt_version_number_positive"
            ),
            models.CheckConstraint(condition=~Q(instructions=""), name="assistant_prompt_instructions"),
        ]

    def __str__(self) -> str:
        return f"{self.template} v{self.number}"


class GenerationJob(TimeStampedModel):
    """One generation; its suggestions stay here until a person applies or discards them."""

    product = models.ForeignKey(
        "catalog.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="mahsulot",
    )
    created_by = models.ForeignKey(
        "accounts.StaffUser", on_delete=models.PROTECT, related_name="+", verbose_name="so‘ragan"
    )
    mode = models.CharField(max_length=16, choices=enums.AssistantMode.choices, verbose_name="rejim")
    languages = models.JSONField(default=list, blank=True, verbose_name="tillar")  # ["uz", "ru", "en"]
    # requested target keys (snapshot key vocabulary: texts.<lang>.<field>, seo.<lang>.<leaf>,
    # images.<id>.alt.<lang>); [] = chosen automatically by the mode
    fields = models.JSONField(default=list, blank=True, verbose_name="maydonlar")
    goal = models.CharField(
        max_length=16,
        choices=enums.AssistantImproveGoal.choices,
        blank=True,
        default="",
        verbose_name="maqsad",
    )
    image_ids = models.JSONField(default=list, blank=True, verbose_name="rasmlar")
    # target key → the product value when the job was created (apply compares it under the row lock)
    input_snapshot = models.JSONField(default=dict, blank=True, verbose_name="boshlang‘ich qiymatlar")
    state = models.CharField(
        max_length=16,
        choices=enums.JobState.choices,
        default=enums.JobState.QUEUED,
        db_index=True,
        verbose_name="holat",
    )
    outcome = models.CharField(
        max_length=16,
        choices=enums.AssistantJobOutcome.choices,
        default=enums.AssistantJobOutcome.PENDING,
        verbose_name="natija",
    )
    provider = models.CharField(max_length=32, blank=True, default="", verbose_name="provayder")
    model_id = models.CharField(max_length=100, blank=True, default="", verbose_name="model")
    # {purpose: PromptVersion id} of the style guidance used; rules_version = the code rules' version
    prompt_versions = models.JSONField(default=dict, blank=True, verbose_name="prompt versiyalari")
    rules_version = models.CharField(max_length=20, blank=True, default="", verbose_name="qoidalar versiyasi")
    # [{key, current, suggested, warnings: [...], origin, checked, max_length}, ...] (assistant.validation)
    suggestions = models.JSONField(default=list, blank=True, verbose_name="takliflar")
    warnings = models.JSONField(default=list, blank=True, verbose_name="ogohlantirishlar")
    input_tokens = models.PositiveIntegerField(default=0, verbose_name="kirish tokenlari")
    output_tokens = models.PositiveIntegerField(default=0, verbose_name="chiqish tokenlari")
    cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, default=Decimal("0"), verbose_name="narx (USD)"
    )
    error_code = models.CharField(max_length=40, blank=True, default="", verbose_name="xato kodi")
    attempts = models.PositiveSmallIntegerField(default=0, verbose_name="urinishlar")
    celery_task_id = models.CharField(max_length=64, blank=True, default="", verbose_name="Celery vazifasi")
    started_at = models.DateTimeField(null=True, blank=True, verbose_name="boshlangan")
    heartbeat_at = models.DateTimeField(null=True, blank=True, verbose_name="oxirgi signal")
    finished_at = models.DateTimeField(null=True, blank=True, verbose_name="tugagan")
    applied_keys = models.JSONField(default=list, blank=True, verbose_name="qo‘llangan maydonlar")
    applied_at = models.DateTimeField(null=True, blank=True, verbose_name="qo‘llangan")
    applied_by = models.ForeignKey(
        "accounts.StaffUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        verbose_name="qo‘llagan",
    )

    class Meta:
        verbose_name = "AI so‘rovi"
        verbose_name_plural = "AI so‘rovlari"
        ordering = ["-created_at", "-id"]
        constraints = [
            # one queued/running admin job per product (console prompt tests excluded)
            models.UniqueConstraint(
                fields=["product"],
                condition=Q(state__in=ACTIVE_JOB_STATES) & ~Q(mode=enums.AssistantMode.TEST),
                name="assistant_one_active_job_per_product",
            ),
            models.CheckConstraint(
                condition=~Q(state=enums.JobState.FAILED) | ~Q(error_code=""),
                name="assistant_failed_job_has_error",
            ),
            models.CheckConstraint(condition=Q(cost_usd__gte=0), name="assistant_job_cost_non_negative"),
            models.CheckConstraint(
                condition=Q(finished_at__isnull=True)
                | Q(state__in=[enums.JobState.SUCCEEDED, enums.JobState.FAILED]),
                name="assistant_job_finished_is_final",
            ),
            models.CheckConstraint(
                condition=Q(outcome=enums.AssistantJobOutcome.PENDING) | Q(state=enums.JobState.SUCCEEDED),
                name="assistant_job_outcome_needs_success",
            ),
        ]
        indexes = [
            models.Index(fields=["created_by", "created_at"], name="assistant_job_user_created"),
            models.Index(fields=["finished_at"], name="assistant_job_finished"),
            models.Index(fields=["product", "-created_at"], name="assistant_job_product"),
        ]

    def __str__(self) -> str:
        return f"#{self.pk} {self.get_mode_display()}"

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_JOB_STATES
