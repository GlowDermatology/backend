"""The shapes of health/: the readiness of each provider and the health answer."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from glowderma.core.api.serializers import ProviderModesSerializer

# Renders a datetime as the API does (Tashkent, seconds, "+05:00").
AWARE_DATETIME = serializers.DateTimeField()


class ProviderReadinessSerializer(serializers.Serializer[dict[str, Any]]):
    configured = serializers.BooleanField(
        help_text="The live adapter can be built and its `is_configured()` (where the port has one) says "
        "yes; `false` makes readiness answer 503."
    )
    last_sync_at = serializers.DateTimeField(
        allow_null=True,
        help_text="Billz and BTS only: the last successful catalog sync / polling run; `null` otherwise.",
    )
    last_sync_age_seconds = serializers.IntegerField(
        allow_null=True, min_value=0, help_text="Seconds since `last_sync_at`; `null` with it."
    )


class HealthSerializer(serializers.Serializer[dict[str, Any]]):
    status = serializers.ChoiceField(choices=["ok", "error"])
    checks = serializers.DictField(child=serializers.ChoiceField(choices=["ok", "error"]), required=False)
    version = serializers.CharField(required=False)
    integrations = ProviderModesSerializer(
        required=False,
        help_text="`?ready=1` only, and only for a caller that passes the admin network gate (§1): the mode "
        "of every provider with a `*_MODE` setting.",
    )
    sandbox = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        help_text="With `integrations`: the live providers configured against a sandbox (BTS test host, "
        "Payme sandbox checkout / `PAYME_TEST_MODE`, Eskiz test sender); empty in a real deployment.",
    )
    providers = serializers.DictField(
        child=ProviderReadinessSerializer(),
        required=False,
        help_text="`?ready=1` only, and only for a caller that passes the admin network gate (§1): one entry "
        "per provider whose mode is `live`. A live provider that is not configured makes `status` `error` "
        "(503) for every caller.",
    )
    cashback_policy_missing = serializers.BooleanField(
        required=False,
        allow_null=True,
        help_text="With `integrations`: no cashback policy version is in force, so purchases earn nothing. "
        "Informational, never fails readiness; `null` while the database check fails.",
    )
