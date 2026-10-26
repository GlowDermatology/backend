"""``manage.py requeue_outbox --topic <topic> --dead [--since ISO] [--dry-run]``.

Puts the dead outbox messages of one topic back in the queue once staff fixed what killed them — typically a
Billz configuration error (a revoked integration key, a cash box of another shop, a wrong base URL: decision
D3, docs/integrations/billz.md §7). Each requeued message becomes ``pending`` with ``attempts = 0``,
``available_at = now`` and no deferral window; ``last_error`` is kept until the next attempt overwrites it.
Every requeued message gets its own audit row ``outbox.requeued`` (actor ``system``, reason "manage.py
requeue_outbox") with the state it left. ``--since`` keeps only the messages that went dead at or after
that instant (their ``updated_at``); ``--dry-run`` lists them and changes nothing. The counts are printed.

Handlers are idempotent at the provider boundary, so a requeued message never repeats an effect that
succeeded. It can still repeat one done **by hand** meanwhile: before requeueing ``billz.push_sale``, check
that none of the listed orders was entered in Billz by hand (the list names each message's dedupe key).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from glowderma.core import audit, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.core.pii import clean_text

REASON = "manage.py requeue_outbox"


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"--since must be an ISO 8601 datetime, not {value!r}") from exc
    if parsed.tzinfo is None:
        raise CommandError("--since needs a UTC offset (e.g. 2026-10-26T09:00:00+05:00)")
    return parsed


class Command(BaseCommand):
    help = "Requeue the dead outbox messages of one topic (audited); prints what it requeued."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--topic", required=True, help="Outbox topic, e.g. billz.push_sale.")
        parser.add_argument(
            "--dead", action="store_true", help="Requeue dead messages (the only kind requeued; required)."
        )
        parser.add_argument("--since", help="Only messages that went dead at or after this ISO datetime.")
        parser.add_argument("--dry-run", action="store_true", help="List the messages; change nothing.")

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["dead"]:
            raise CommandError("Only dead messages are requeued: pass --dead")
        topic = str(options["topic"]).strip()
        if not topic:
            raise CommandError("--topic must name an outbox topic")
        messages = OutboxMessage.objects.filter(topic=topic, status="dead")
        if options["since"]:
            messages = messages.filter(updated_at__gte=_instant(str(options["since"])))
        dry_run = bool(options["dry_run"])
        requeued = 0
        matched = 0
        for message_id in list(messages.order_by("id").values_list("pk", flat=True)):
            with transaction.atomic():
                message = (
                    OutboxMessage.objects.select_for_update().filter(pk=message_id, status="dead").first()
                )
                if message is None:  # requeued or changed meanwhile
                    continue
                matched += 1
                self.stdout.write(
                    f"{'would requeue' if dry_run else 'requeue'} #{message.pk} {message.dedupe_key} "
                    f"(attempts {message.attempts}): {clean_text(message.last_error, 200)}"
                )
                if dry_run:
                    continue
                before = {
                    "status": message.status,
                    "attempts": message.attempts,
                    "last_error": message.last_error,
                }
                OutboxMessage.objects.filter(pk=message.pk).update(
                    status="pending",
                    attempts=0,
                    available_at=timeutils.now(),
                    lease_expires_at=None,
                    claim_token=None,
                    deferred_since=None,
                    updated_at=timeutils.now(),
                )
                audit.record(
                    action="outbox.requeued",
                    entity_type="outbox",
                    entity_id=str(message.pk),
                    before=before,
                    after={"topic": message.topic, "dedupe_key": message.dedupe_key, "status": "pending"},
                    reason=REASON,
                )
                requeued += 1
        suffix = " (dry run: nothing changed)" if dry_run else ""
        self.stdout.write(f"{topic}: {matched} dead message(s) matched, {requeued} requeued{suffix}.")
