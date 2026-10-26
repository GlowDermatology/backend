"""Serializers of the shared contract shapes (contract §2.0)."""

from typing import Any

from rest_framework import serializers


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
    phone = serializers.CharField()
