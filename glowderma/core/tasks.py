from celery import shared_task  # type: ignore[import-untyped]

from glowderma.core import timeutils
from glowderma.core.models import IdempotencyRecord


@shared_task(name="core.purge_idempotency_records")  # type: ignore[untyped-decorator]
def purge_idempotency_records() -> int:
    count, _ = IdempotencyRecord.objects.filter(expires_at__lte=timeutils.now()).delete()
    return count
