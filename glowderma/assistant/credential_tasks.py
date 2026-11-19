"""``assistant.test_credential``: the worker side of a key rotation / key test (``assistant.credentials``).

Arguments are ids only — the provider row, the Vault version to test (``None`` = the active one) and the
staff member who asked — never a key: the worker reads the version from Vault itself. Registered with Celery
by ``config/celery.py`` (``autodiscover_tasks(related_name="credential_tasks")``) and imported by the console
views that enqueue it.
"""

from __future__ import annotations

from celery import shared_task  # type: ignore[import-untyped]

from glowderma.assistant import credentials


@shared_task(name="assistant.test_credential", soft_time_limit=60, time_limit=90)  # type: ignore[untyped-decorator]
def test_credential(config_id: int, version: int | None = None, actor_id: int | None = None) -> str:
    """Test ``version`` (or the active key) with the provider; activate / reject / record the result."""
    return credentials.finish_test(config_id, version, actor_id=actor_id)
