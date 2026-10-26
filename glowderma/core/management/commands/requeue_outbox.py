"""``manage.py requeue_outbox --topic <topic> --dead [--since ISO] [--order ID] [--id N ...] [--resolved]
[--dry-run] [--forget-payment-attempt --order ID --actor EMAIL]``.

Puts the dead outbox messages of one topic back in the queue once staff fixed what killed them — typically a
Billz configuration error (a revoked integration key, a cash box of another shop, a wrong base URL: decision
D3, docs/integrations/billz.md §7). Each requeued message becomes ``pending`` with ``attempts = 0``,
``available_at = now`` and no deferral window; ``last_error`` is kept until the next attempt overwrites it.
Every requeued message gets its own audit row ``outbox.requeued`` (actor ``system``, reason "manage.py
requeue_outbox") with the state it left. ``--since`` keeps only the messages that went dead at or after
that instant (their ``updated_at``); ``--order`` only those of one order (``payload.order_id``); ``--id``
(repeatable) only those message ids; ``--dry-run`` lists them and changes nothing. The counts are printed.

The module owning a topic may register :class:`core.outbox.RequeueHooks` (``core`` knows no module above
it): each listing line then says what the message is about (``billz.push_sale``: the order and the Billz
draft), a topic warning is printed first, and a message whose effect was already done another way is listed
but **skipped** (``billz.push_sale``: a sale staff recorded by hand with ``manage.py billz_mark_recorded``).

``--resolved`` closes dead messages that staff handled by hand instead (a sale entered in Billz, a refund paid
out in a cabinet): each becomes ``done`` without running its handler, audited ``outbox.resolved`` (reason
"manage.py requeue_outbox --resolved"). A topic's hooks may refuse it for a message (listed as ``refuse``,
left dead): ``billz.push_sale`` resolves only a sale already recorded with ``billz_mark_recorded`` — that
command is what takes the order off K1 and stops its units counting against stock. Retention never purges a
dead message (core.retention); a resolved one is purged with the done rows after ``OUTBOX_RETENTION_DAYS``
and leaves the consistency report (K6).

``--forget-payment-attempt`` (a topic whose hooks offer it — ``billz.push_sale``; needs ``--order`` and
``--actor``; not with ``--resolved``): a sale that died "check by hand" after its payment answer was lost
keeps the stamp that stops any second payment, so a plain requeue dies again at once. Once staff confirmed
**in Billz** that the payment never arrived (the sale is still an unpaid draft), this flag clears the stamp
before requeueing (audited by the topic's module with the staff actor), so the next attempt pays the draft.

Handlers are idempotent at the provider boundary, so a requeued message never repeats an effect that
succeeded. It can still repeat one done **by hand** meanwhile: before requeueing ``billz.push_sale``, check
that none of the listed orders was entered in Billz by hand (record those with ``billz_mark_recorded``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from glowderma.core import audit, outbox, timeutils
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
        parser.add_argument("--order", type=int, help="Only the message(s) of this order (payload order_id).")
        parser.add_argument(
            "--id", action="append", type=int, dest="ids", help="Only this message id (repeatable)."
        )
        parser.add_argument(
            "--resolved",
            action="store_true",
            help="Mark the messages done (handled by hand) instead of requeueing them; audited.",
        )
        parser.add_argument("--dry-run", action="store_true", help="List the messages; change nothing.")
        parser.add_argument(
            "--forget-payment-attempt",
            action="store_true",
            help="billz.push_sale: clear the payment-attempt stamp first — only after staff confirmed in "
            "Billz that the payment never arrived. Needs --order and --actor.",
        )
        parser.add_argument("--actor", help="The staff email that confirmed it (--forget-payment-attempt).")

    def handle(self, *args: Any, **options: Any) -> None:
        if not options["dead"]:
            raise CommandError("Only dead messages are requeued: pass --dead")
        topic = str(options["topic"]).strip()
        if not topic:
            raise CommandError("--topic must name an outbox topic")
        hooks = outbox.requeue_hooks(topic)
        forget = bool(options["forget_payment_attempt"])
        resolve = bool(options["resolved"])
        if forget and resolve:
            raise CommandError(
                "--forget-payment-attempt requeues a sale: it cannot be combined with --resolved"
            )
        actor = self._forget_actor(topic, hooks, options) if forget else None
        messages = OutboxMessage.objects.filter(topic=topic, status="dead")
        if options["since"]:
            messages = messages.filter(updated_at__gte=_instant(str(options["since"])))
        if options["order"] is not None:
            messages = messages.filter(payload__order_id=int(options["order"]))
        if options["ids"]:
            messages = messages.filter(pk__in=list(options["ids"]))
        dry_run = bool(options["dry_run"])
        verb = "resolve" if resolve else "requeue"
        if hooks is not None and hooks.warning and not resolve:
            self.stdout.write(f"WARNING: {hooks.warning}")
        changed = matched = skipped = 0
        for message_id in list(messages.order_by("id").values_list("pk", flat=True)):
            with transaction.atomic():
                message = (
                    OutboxMessage.objects.select_for_update().filter(pk=message_id, status="dead").first()
                )
                if message is None:  # requeued or changed meanwhile
                    continue
                matched += 1
                about = hooks.describe(message) if hooks is not None else ""
                about = f" [{about}]" if about else ""
                if resolve:
                    refusal = hooks.resolve_refusal(message) if hooks and hooks.resolve_refusal else ""
                    if refusal:
                        skipped += 1
                        self.stdout.write(f"refuse #{message.pk} {message.dedupe_key}{about}: {refusal}")
                        continue
                else:
                    skip = hooks.skip_reason(message) if hooks is not None else ""
                    if skip:
                        skipped += 1
                        self.stdout.write(f"skip #{message.pk} {message.dedupe_key}{about}: {skip}")
                        continue
                self.stdout.write(
                    f"{'would ' + verb if dry_run else verb} #{message.pk} {message.dedupe_key}{about} "
                    f"(attempts {message.attempts}): {clean_text(message.last_error, 200)}"
                )
                if forget and hooks is not None and hooks.forget_payment_attempt is not None:
                    if dry_run:
                        self.stdout.write(f"would forget the payment attempt of #{message.pk}")
                    else:
                        try:
                            self.stdout.write(hooks.forget_payment_attempt(message, actor))
                        except outbox.RequeueRefused as exc:
                            raise CommandError(str(exc)) from exc
                if dry_run:
                    continue
                if resolve:
                    self._resolve(message)
                else:
                    self._requeue(message)
                changed += 1
        suffix = " (dry run: nothing changed)" if dry_run else ""
        done = "resolved" if resolve else "requeued"
        left = "refused" if resolve else "skipped"
        self.stdout.write(
            f"{topic}: {matched} dead message(s) matched, {changed} {done}, {skipped} {left}{suffix}."
        )

    @staticmethod
    def _forget_actor(topic: str, hooks: outbox.RequeueHooks | None, options: dict[str, Any]) -> Any:
        if hooks is None or hooks.forget_payment_attempt is None:
            raise CommandError(f"--forget-payment-attempt is not supported for topic {topic}")
        if options["order"] is None:
            raise CommandError("--forget-payment-attempt needs --order: it clears one sale's guard at a time")
        email = str(options["actor"] or "").strip()
        if not email:
            raise CommandError("--forget-payment-attempt needs --actor (the staff email that confirmed it)")
        actor = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
        if actor is None:
            raise CommandError(f"--actor: no active staff member with the email {email!r}")
        return actor

    @staticmethod
    def _before(message: OutboxMessage) -> dict[str, Any]:
        return {"status": message.status, "attempts": message.attempts, "last_error": message.last_error}

    @classmethod
    def _requeue(cls, message: OutboxMessage) -> None:
        before = cls._before(message)
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

    @classmethod
    def _resolve(cls, message: OutboxMessage) -> None:
        before = cls._before(message)
        OutboxMessage.objects.filter(pk=message.pk).update(
            status="done", lease_expires_at=None, claim_token=None, updated_at=timeutils.now()
        )
        audit.record(
            action="outbox.resolved",
            entity_type="outbox",
            entity_id=str(message.pk),
            before=before,
            after={"topic": message.topic, "dedupe_key": message.dedupe_key, "status": "done"},
            reason=REASON + " --resolved",
        )
