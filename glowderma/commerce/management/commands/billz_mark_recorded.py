"""``manage.py billz_mark_recorded --order <id> [--billz-sale-id <id>] --actor <staff email> [--dry-run]``.

The remedy of a dead ``billz.push_sale`` (docs/integrations/billz.md §7): once staff entered (or completed)
the order's sale in Billz by hand, this records it — the purchase gets its pushed marker through the push's
own link path, ``billz_sale_recorded`` is written once and the audit row ``billz.sale_recorded_by_hand``
names the staff member. The push and ``requeue_outbox`` then skip the order, and its units stop counting
against the online availability after the next Billz stock read (without this they are subtracted from
Billz's stock — which already counts the hand-entered sale — for ever).

Refused (exit status 1, nothing changed): an unknown or cancelled order, an order without an online purchase,
one already recorded in Billz, a push running right now, a ``--billz-sale-id`` other than the draft the push
created, no sale id at all (the push died before Billz created a draft: name the sale staff entered), an
unknown or inactive staff email. ``--dry-run`` prints what would be recorded.

The order then leaves the consistency report's ``k1_billz_sale_unrecorded`` (and ``_backlog``): its purchase
has ``billz_pushed_at`` and a Billz sale id. Its dead ``billz.push_sale`` message stays listed under
``k1_billz_sale_dead`` until staff close it with ``requeue_outbox --topic billz.push_sale --dead --resolved
--order <id>``.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from glowderma.accounts.models import StaffUser
from glowderma.commerce import billz_sales


class Command(BaseCommand):
    help = (
        "Record that staff entered an order's Billz sale by hand (audited); see docs/integrations/billz.md."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--order", type=int, required=True, help="The Glow Derma order id.")
        parser.add_argument(
            "--billz-sale-id",
            default="",
            help="The Billz sale id (UUID) staff entered; default: the draft the push created (required when "
            "the push created none).",
        )
        parser.add_argument("--actor", required=True, help="The email of the staff member who entered it.")
        parser.add_argument(
            "--dry-run", action="store_true", help="Print what would be recorded; change nothing."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        email = str(options["actor"]).strip().lower()
        actor = StaffUser.objects.filter(email__iexact=email, is_active=True).first()
        if actor is None:
            raise CommandError(f"--actor: no active staff member with the email {email!r}")
        dry_run = bool(options["dry_run"])
        try:
            record = billz_sales.record_sale_by_hand(
                int(options["order"]),
                actor=actor,
                billz_sale_id=str(options["billz_sale_id"]),
                dry_run=dry_run,
            )
        except billz_sales.RecordByHandRefused as exc:
            raise CommandError(str(exc)) from exc
        verb = "would record" if dry_run else "recorded"
        self.stdout.write(
            f"{verb} order {record.order_id} (purchase {record.purchase_id}) as entered in Billz by hand: "
            f"Billz sale {record.billz_sale_id or '-'} (draft {record.draft_id or '-'}), "
            f"push message {record.push_status or '-'}"
        )
        if record.payment_attempted_at is not None:
            self.stdout.write(
                f"note: a payment of draft {record.draft_id} was attempted at "
                f"{record.payment_attempted_at.isoformat()}; make sure Billz holds one paid sale for this "
                "order"
            )
        if dry_run:
            self.stdout.write("dry run: nothing changed.")
