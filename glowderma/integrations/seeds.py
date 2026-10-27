"""Demo seeder of the integrations module (design 03 §7.3).

``IntegrationState(billz).last_sync_at = anchor − 2 min`` and one succeeded delta ``Job`` at that time,
created by the seeded administrator (looked up through ``get_user_model()``: integrations sits below
accounts in the layer contract; the accounts seeder runs first, order 10). Other providers keep
``last_sync_at = null`` (rows from ``0003``).
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import transaction

from glowderma.core import enums
from glowderma.core.seeding import SeedContext, register_seeder
from glowderma.integrations.models import IntegrationState, Job

ADMIN_EMAIL = "admin@glowderma.uz"
SEED_JOB_RESULT = {"created": 0, "updated": 248, "unchanged": 0, "new_from_billz": 0}


@register_seeder("integrations", order=20)
@transaction.atomic
def seed_integrations(context: SeedContext) -> None:
    synced_at = context.anchor - timedelta(minutes=2)
    state, _ = IntegrationState.objects.get_or_create(provider=IntegrationState.Provider.BILLZ)
    if state.last_sync_at != synced_at:
        IntegrationState.objects.filter(pk=state.pk).update(
            last_sync_at=synced_at, last_success_at=synced_at, updated_at=synced_at
        )
    if Job.objects.filter(kind=enums.JobKind.BILLZ_IMPORT, created_at=synced_at).exists():
        return
    try:
        admin = get_user_model().objects.get(email=ADMIN_EMAIL)
    except get_user_model().DoesNotExist as exc:  # idempotency keys on (kind, created_at): never backfilled
        raise RuntimeError(
            "seed_integrations needs the seeded administrator: run the accounts seeder first"
        ) from exc
    job = Job.objects.create(
        kind=enums.JobKind.BILLZ_IMPORT,
        state=enums.JobState.SUCCEEDED,
        params={"mode": enums.BillzImportMode.DELTA},
        progress={"done": 248, "total": 248},
        result=dict(SEED_JOB_RESULT),
        created_at=synced_at,
        updated_at=synced_at,
        started_at=synced_at,
        finished_at=synced_at,
        heartbeat_at=synced_at,
        attempts=1,
        created_by=admin,
    )
    # TimeStampedModel.save() stamps updated_at with the real clock; pin it like every other seeded instant
    Job.objects.filter(pk=job.pk).update(updated_at=synced_at)
