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
