import logging
from typing import Any

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.db.models.signals import post_migrate

from glowderma.core import integration_modes

logger = logging.getLogger("glowderma.integrations")
_fake_mode_warned = False

PROVIDERS = ("billz", "bts", "click", "payme", "eskiz", "fcm")


def ensure_provider_rows(sender: AppConfig, *, using: str, apps: Any = global_apps, **kwargs: Any) -> None:
    """Keep one ``IntegrationState`` row per provider (design 01 §2.3: reads never write).

    ``0003_seed_integration_state`` inserts them; this ``post_migrate`` receiver re-inserts them after a
    ``flush`` (transactional tests, ``--reuse-db`` reruns), the way ``contenttypes`` restores its rows.
    """
    # ``migrate`` passes the post-migration state apps, ``flush`` passes nothing (the global registry)
    try:
        IntegrationState = apps.get_model("integrations", "IntegrationState")
    except LookupError:  # migrated to zero: no table to fill
        return
    for provider in PROVIDERS:
        IntegrationState.objects.using(using).get_or_create(provider=provider)


def warn_fake_providers() -> bool:
    """Log one WARNING per process listing the providers in ``fake`` mode outside the local/test settings.

    Fake providers are allowed in production (product owner decision 2026-10-26), so this is a log line,
    never a system check (CI runs ``check --deploy --fail-level WARNING``) and never a refusal to start. The
    condition is ``not DEV_FEATURES_ALLOWED``, the same as ``integration_modes.fake_in_production`` (customer
    contract §6.14), so the warning names exactly the providers that run in the restricted fake posture.
    Returns whether the line was emitted by this call.
    """
    global _fake_mode_warned
    if _fake_mode_warned or integration_modes.dev_features_allowed():
        return False
    fakes = integration_modes.fake_providers()
    if not fakes:
        return False
    _fake_mode_warned = True
    logger.warning("integrations running in fake mode in production: %s", ", ".join(fakes))
    return True


_sandbox_warned = False


def warn_sandbox_providers() -> bool:
    """Log one WARNING per process naming the live providers configured against a sandbox (BTS's test host,
    ``PAYME_TEST_MODE`` / Payme's sandbox checkout, Eskiz's test sender); never a refusal to start (staging
    runs the production settings against sandboxes). Returns whether the line was emitted by this call."""
    global _sandbox_warned
    sandboxes = integration_modes.sandbox_providers()
    if _sandbox_warned or not sandboxes:
        return False
    _sandbox_warned = True
    logger.warning("integrations running against a sandbox: %s", ", ".join(sandboxes))
    return True


class IntegrationsConfig(AppConfig):
    name = "glowderma.integrations"
    label = "integrations"
    verbose_name = "Integratsiyalar"

    def ready(self) -> None:
        from glowderma.core import readiness
        from glowderma.integrations.readiness import PROBE

        post_migrate.connect(
            ensure_provider_rows, sender=self, dispatch_uid="integrations.ensure_provider_rows"
        )
        warn_fake_providers()
        warn_sandbox_providers()
        # health/?ready=1: live providers' configured flags and sync ages (core cannot import integrations)
        readiness.register(PROBE)
