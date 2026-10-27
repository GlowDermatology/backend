"""Serializers of the shared contract shapes (contract §2.0)."""

from typing import Any

from rest_framework import serializers

from glowderma.core import enums
from glowderma.core.integration_modes import PROVIDER_MODE_SETTINGS


class ActorSerializer(serializers.Serializer[Any]):
    """``Actor = {"id", "display_name"}``: the staff full name, else the email (``null`` = system).

    Takes any staff user instance; declare it with ``allow_null=True`` where the actor may be the system.
    """

    id = serializers.IntegerField(source="pk")
    display_name = serializers.SerializerMethodField()

    def get_display_name(self, obj: Any) -> str:
        return str(getattr(obj, "full_name", "") or obj.get_username())


# Takes any ``accounts.Customer`` instance; the one ``CustomerRef`` component every module renders.
class CustomerRefSerializer(serializers.Serializer[Any]):
    """``CustomerRef = {"id", "name", "phone"}`` (contract §2.0); ``name`` is ``""`` once the customer is
    deleted (scrubbed).
    """

    id = serializers.IntegerField(source="pk")
    name = serializers.CharField(allow_blank=True)
    # Hybrid identity: null only for an account without a phone yet (never on orders and payments, which are
    # phone-gated; a staff booking may name one).
    phone = serializers.CharField(allow_null=True)


# One field per entry of ``core.integration_modes.PROVIDER_MODE_SETTINGS`` (health ``?ready=1`` and
# ``GET settings/``), so a provider added there is rendered everywhere.
class ProviderModesSerializer(serializers.Serializer[dict[str, str]]):
    """Per-provider adapter modes (``*_MODE`` settings, each defaulting to ``INTEGRATIONS_MODE``)."""

    def get_fields(self) -> dict[str, serializers.Field[Any, Any, Any, Any]]:
        return {
            provider: serializers.ChoiceField(choices=enums.IntegrationMode.values)
            for provider in PROVIDER_MODE_SETTINGS
        }
