"""Each provider's ``IntegrationState``: the last sync, the provider data, and the success and failure
record."""

from __future__ import annotations

import json
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.db.models import F

from glowderma.core import timeutils
from glowderma.integrations.models import IntegrationState


def get_state(provider: str) -> IntegrationState:
    """The provider's state row (created lazily; the migration and ``post_migrate`` normally keep it)."""
    state, _ = IntegrationState.objects.get_or_create(provider=provider)
    return state


def record_sync_success(provider: str, *, synced_at: Any = None) -> IntegrationState:
    """A successful sync (Billz) or poll (BTS): sets ``last_sync_at`` and clears the failure run."""
    instant = synced_at or timeutils.now()
    state = get_state(provider)
    IntegrationState.objects.filter(pk=state.pk).update(
        last_sync_at=instant,
        last_success_at=instant,
        consecutive_failures=0,
        last_error="",
        updated_at=timeutils.now(),
    )
    state.refresh_from_db()
    return state


def update_provider_data(provider: str, changes: dict[str, Any]) -> IntegrationState:
    """Merge ``changes`` into the provider's cursor document (``IntegrationState.data``) under the row lock.

    The Billz adapter keeps its sync cursor here (``delta_since``, ``full_count``, ``full_synced_at``); it
    calls this only after the caller applied a whole run, so a failed run never moves the cursor.
    """
    get_state(provider)
    with transaction.atomic():
        state = IntegrationState.objects.select_for_update().get(provider=provider)
        data = dict(state.data) if isinstance(state.data, dict) else {}
        data.update(changes)
        state.data = data
        state.updated_at = timeutils.now()
        state.save(update_fields=["data", "updated_at"])
        return state


def record_success(provider: str) -> bool:
    """A successful call that is not a sync (a Yandex price check on the customer path): ends the failure
    run. One ``UPDATE`` that matches no row while the provider is healthy (no row lock, no write); ``True``
    when a run was ended."""
    instant = timeutils.now()
    ended = IntegrationState.objects.filter(provider=provider, consecutive_failures__gt=0).update(
        last_success_at=instant, consecutive_failures=0, last_error="", updated_at=instant
    )
    return ended > 0


def record_failure(provider: str, message: str) -> IntegrationState:
    state = get_state(provider)
    instant = timeutils.now()
    IntegrationState.objects.filter(pk=state.pk).update(
        last_error_at=instant,
        last_error=message[:2000],
        consecutive_failures=F("consecutive_failures") + 1,
        updated_at=instant,
    )
    state.refresh_from_db()
    return state


def set_state_data(provider: str, key: str, value: Any) -> None:
    """Set ``IntegrationState(provider).data[key] = value`` with one ``UPDATE … SET data = data ||
    jsonb_build_object(key, value)`` (no read-modify-write, so a concurrent :func:`record_failure` or another
    key's writer is never lost). The BTS poll cursor is its first user (customer contract §2.13.3)."""
    get_state(provider)
    table = IntegrationState._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table} SET data = data || jsonb_build_object(%s::text, %s::jsonb), "  # noqa: S608
            "updated_at = %s WHERE provider = %s",
            [key, json.dumps(value, cls=DjangoJSONEncoder), timeutils.now(), provider],
        )


__all__ = [
    "get_state",
    "record_failure",
    "record_success",
    "record_sync_success",
    "set_state_data",
    "update_provider_data",
]
