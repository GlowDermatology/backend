"""``manage.py seed_ops_demo``: realistic operational noise for the ops console dashboards (local/dev only).

``seed_demo`` lays down the business (catalogue, customers, orders); this adds what a running shop
accumulates and the dashboards exist to surface: dead and overdue outbox messages, rejected webhooks, a BTS
polling streak of failures, a failed Billz import, a payment stuck in ``pending``, web-channel sales over
the last weeks, fresh sign-ups by phone / e-mail / Google with their codes and sessions, today's clinic day
with one appointment a newly blocked period no longer covers — and then runs the
real consistency report (``run_consistency_report``), so the money dashboard reads a genuine audit row.

Idempotent (rows are keyed ``ops-demo:*``; a second run adds nothing but a new report). Refused under the
production settings (``DEV_FEATURES_ALLOWED`` false).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils.crypto import get_random_string

from glowderma.accounts.models import AuthSession, Customer, CustomerSocialIdentity, Device, SmsCode
from glowderma.appointments.models import Appointment
from glowderma.catalog.models import Product
from glowderma.commerce.models import Order, OrderItem
from glowderma.core import audit, enums, integration_modes, timeutils
from glowderma.core.models import OutboxMessage
from glowderma.integrations.models import IntegrationState, Job, WebhookInbox
from glowderma.loyalty.models import Purchase
from glowderma.notifications.models import Campaign
from glowderma.payments.models import Payment
from glowderma.professionals.models import BlockedPeriod, ProfessionalService
from glowderma.reporting.tasks import run_consistency_report

KEY = "ops-demo"
SIGNUPS = 26  # customers over the last 30 days
WEB_ORDERS = 34  # paid web orders over the last 30 days


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Command(BaseCommand):
    help = "Add ops-console demo signals (outbox, webhooks, sync failures, sign-ups, a report). Dev only."

    def handle(self, *args: Any, **options: Any) -> None:
        if not integration_modes.dev_features_allowed():
            raise CommandError("seed_ops_demo is refused under the production settings")
        now = timeutils.now()
        with transaction.atomic():
            created = {
                "outbox": self._outbox(now),
                "webhooks": self._webhooks(now),
                "states": self._states(now),
                "jobs": self._jobs(now),
                "customers": self._customers(now),
                "orders": self._orders(now),
                "appointments": self._appointments(now),
            }
        report = run_consistency_report()
        created["report_findings"] = sum(check["count"] for check in report["checks"].values())
        for name, count in created.items():
            self.stdout.write(f"{name}: {count}")

    # --- integrations ------------------------------------------------------------------------------------

    def _push_sales(self, count: int) -> list[dict[str, Any]]:
        """``billz.push_sale`` payloads in the handler's shape (commerce.tasks: purchase, order, Billz order
        uuid) for paid online orders whose sale is not recorded yet (else the newest paid ones); a bare
        database without purchases gets ``None`` ids (the rows only feed the dashboards)."""
        purchases = list(
            Purchase.objects.filter(order__isnull=False, order__paid_at__isnull=False).order_by(
                F("billz_pushed_at").asc(nulls_first=True), "-order_id"
            )[:count]
        )
        payloads: list[dict[str, Any]] = [
            {
                "purchase_id": purchase.pk,
                "order_id": purchase.order_id,
                "billz_order_uuid": str(purchase.billz_order_uuid) if purchase.billz_order_uuid else None,
            }
            for purchase in purchases
        ]
        payloads += [{"purchase_id": None, "order_id": None, "billz_order_uuid": None}] * (
            count - len(payloads)
        )
        return payloads

    def _outbox(self, now: datetime) -> int:
        paid = Order.objects.filter(paid_at__isnull=False).order_by("-id").first()
        delivery = (
            Order.objects.filter(fulfilment=enums.FulfilmentMethod.DELIVERY, paid_at__isnull=False)
            .order_by("-id")
            .first()
        )
        code = SmsCode.objects.exclude(phone="").order_by("-id").first()
        campaign = Campaign.objects.order_by("-id").first()
        sales = self._push_sales(3)
        status_push = {
            "order_id": paid.pk if paid else None,
            "status": paid.status if paid else "processing",
            "event_id": None,
            "customer_id": paid.customer_id if paid else None,
        }
        shipment = {
            "order_id": delivery.pk if delivery else None,
            "manual": False,
            "request_no": 1,
            "actor_id": None,
        }
        # (topic, status, due, payload, last error) — the real topics and payload keys of their handlers
        rows = [
            ("billz.push_sale", "dead", now - timedelta(hours=5), sales[0], "HTTP 503 x10"),
            ("billz.push_sale", "dead", now - timedelta(hours=30), sales[1], "HTTP 422"),
            ("push.order_status", "dead", now - timedelta(hours=2), status_push, "FCM unavailable"),
            (
                "sms.send",
                "failed",
                now - timedelta(minutes=95),
                {"sms_code_id": code.pk if code else None},
                "Eskiz timeout",
            ),
            (
                "sms.send",
                "pending",
                now - timedelta(minutes=80),
                {"sms_code_id": code.pk if code else None},
                "",
            ),
            ("bts.create_shipment", "pending", now - timedelta(minutes=3), shipment, ""),
            (
                "campaign.send_batch",
                "pending",
                now - timedelta(minutes=1),
                {"campaign_id": campaign.pk if campaign else None, "batch_no": 0},
                "",
            ),
            ("billz.push_sale", "failed", now - timedelta(hours=2), sales[2], "deferred: shift"),
        ]
        made = 0
        for index, (topic, status, due, payload, error) in enumerate(rows):
            row, created = OutboxMessage.objects.get_or_create(
                dedupe_key=f"{KEY}:outbox:{index}",
                defaults={
                    "topic": topic,
                    "payload": payload,
                    "status": status,
                    "attempts": 10 if status == "dead" else 2,
                    "available_at": due,
                    "last_error": error,
                    "deferred_since": now - timedelta(hours=3) if "deferred" in error else None,
                },
            )
            if created:
                # Enqueued an hour before it was due; touched last when it died (``updated_at`` is "when it
                # died" for K6) or at its last attempt — never before it was created.
                last_touch = due if status == "dead" else min(due, now) - timedelta(minutes=5)
                OutboxMessage.objects.filter(pk=row.pk).update(
                    created_at=due - timedelta(hours=1), updated_at=last_touch
                )
            made += int(created)
        return made

    def _webhooks(self, now: datetime) -> int:
        rows = [
            ("click", "rejected", "-1", 2),
            ("click", "accepted", None, 40),
            ("payme", "error", "-32400", 1),
            ("payme", "accepted", None, 26),
            ("bts", "rejected", "unknown_parcel", 3),
            ("bts", "accepted", None, 18),
        ]
        # what the real views store as the callback's object (commerce.api.webhooks): a payment for Click /
        # Payme, an order for BTS — so an order page's «Diagnostika» finds its webhooks
        payments = list(Payment.objects.order_by("-id").values_list("pk", "order_id")[:50])
        made = 0
        for provider, outcome, code, count in rows:
            for n in range(count):
                entity = ""
                if payments:
                    payment_id, order_id = payments[n % len(payments)]
                    entity = f"order:{order_id}" if provider == "bts" else f"payment:{payment_id}"
                key = f"{KEY}:{provider}:{outcome}:{n}"
                if WebhookInbox.objects.filter(provider=provider, event_key=key).exists():
                    continue
                WebhookInbox.objects.create(
                    provider=provider,
                    event_key=key,
                    received_at=now - timedelta(minutes=17 * n + 5),
                    method="POST",
                    path=f"/api/v1/webhooks/{provider}/",
                    headers={"Content-Type": "application/json"},
                    payload={"demo": True},
                    raw_body="{}",
                    result={"status": 200, "body": {}, "outcome": outcome, "error_code": code},
                    entity_ref=entity if outcome == "accepted" else "",
                    processing_ms=40 + n,
                )
                made += 1
        return made

    def _states(self, now: datetime) -> int:
        IntegrationState.objects.filter(provider="bts").update(
            last_sync_at=now - timedelta(hours=6),
            last_success_at=now - timedelta(hours=6),
            last_error_at=now - timedelta(minutes=8),
            last_error="BTS API timeout (demo)",
            consecutive_failures=4,
            updated_at=now,
        )
        IntegrationState.objects.filter(provider="billz").update(
            last_sync_at=now - timedelta(minutes=4),
            last_success_at=now - timedelta(minutes=4),
            updated_at=now,
        )
        IntegrationState.objects.filter(provider="eskiz").update(
            last_success_at=now - timedelta(minutes=11),
            last_error_at=now - timedelta(minutes=95),
            consecutive_failures=1,
            updated_at=now,
        )
        return 3

    def _jobs(self, now: datetime) -> int:
        if Job.objects.filter(params__demo=KEY).exists():
            return 0
        Job.objects.create(
            kind=enums.JobKind.BILLZ_IMPORT,
            state=enums.JobState.FAILED,
            params={"mode": "full", "demo": KEY},
            error={"code": "billz_unavailable", "message": "Billz 503 after 3 retries"},
            started_at=now - timedelta(hours=9, minutes=37),
            finished_at=now - timedelta(hours=9, minutes=33),
            heartbeat_at=now - timedelta(hours=9, minutes=33),
            attempts=1,
            created_at=now - timedelta(hours=9, minutes=37),
        )
        return 1

    # --- customers ---------------------------------------------------------------------------------------

    def _customers(self, now: datetime) -> int:
        made = 0
        for n in range(SIGNUPS):
            method = ("phone", "phone", "email", "google", "phone", "facebook", "email")[n % 7]
            registered = now - timedelta(days=(n * 29) // SIGNUPS, hours=n % 9)
            phone = f"+99897{7300000 + n:07d}" if method == "phone" or n % 5 == 0 else None
            email = None if method == "phone" else f"{KEY}-{n}@example.uz"
            if (email and Customer.objects.filter(email=email).exists()) or (
                phone and Customer.objects.filter(phone=phone).exists()
            ):
                continue
            customer = Customer.objects.create(
                phone=phone,
                email=email,
                email_verified_at=registered if email else None,
                name=f"Demo mijoz {n + 1}",
                registered_at=registered,
                last_login_at=registered + timedelta(hours=1),
            )
            if method in ("google", "facebook"):
                CustomerSocialIdentity.objects.create(
                    customer=customer, provider=method, subject=f"{KEY}-{n}", created_at=registered
                )
            if phone and method != "phone":  # signed up without one, attached it later
                audit.record(
                    action="customer.phone_attached",
                    entity_type="customer",
                    entity_id=str(customer.pk),
                    actor=customer,
                    actor_type="customer",
                )
            platform = ("web", "ios", "android")[n % 3]
            device = Device.objects.create(
                customer=customer, fid=f"{KEY}-fid-{n}", platform=platform, last_active_at=registered
            )
            AuthSession.objects.create(
                audience="customer",
                customer=customer,
                device=device,
                device_label=f"{platform} demo",
                refresh_hash=_hash(get_random_string(32)),
                created_at=registered,
                last_used_at=now - timedelta(hours=n),
                absolute_expires_at=now + timedelta(days=30),
            )
            if method == "phone":
                purpose, code_phone, code_email = "signup", phone or "", ""
            elif method == "email":
                purpose, code_phone, code_email = "signup", "", email or ""
            else:
                purpose = "attach_phone" if phone else "social_link"
                code_phone, code_email = (phone, "") if phone else ("", email or "")
            created_at = now - timedelta(hours=n * 6)
            SmsCode.objects.create(
                phone=code_phone,
                email=code_email,
                purpose=purpose,
                code_hmac=_hash(f"code-{n}"),
                attempts=5 if n % 11 == 0 else 1,
                created_at=created_at,
                expires_at=created_at + timedelta(seconds=120),
                consumed_at=None if n % 11 == 0 else created_at + timedelta(seconds=40),
                exhausted_at=created_at + timedelta(seconds=90) if n % 11 == 0 else None,
            )
            made += 1
        return made

    # --- orders ------------------------------------------------------------------------------------------

    def _orders(self, now: datetime) -> int:
        if Order.objects.filter(metadata__seed=KEY).exists():
            return 0
        customers = list(Customer.objects.filter(phone__isnull=False).order_by("id")[:12])
        active = Product.objects.filter(status=enums.ProductStatus.ACTIVE, price__gt=0)
        products = list(active.order_by("id")[:15])
        if not customers or not products:
            return 0
        made = 0
        for n in range(WEB_ORDERS):
            customer = customers[n % len(customers)]
            product = products[(n * 7) % len(products)]
            quantity = 1 + n % 3
            subtotal = product.price * quantity
            created = now - timedelta(days=(n * 29) // WEB_ORDERS, hours=2 + n % 7)
            order = Order.objects.create(
                customer=customer,
                channel=enums.OrderChannel.WEB,
                fulfilment=enums.FulfilmentMethod.PICKUP,
                status=enums.OrderStatus.DELIVERED,
                recipient_name=customer.name,
                recipient_phone=customer.phone or "",
                items_subtotal=subtotal,
                total=subtotal,
                payment_provider=enums.PaymentProvider.CLICK if n % 2 else enums.PaymentProvider.PAYME,
                reservation_minutes=15,
                reservation_expires_at=created + timedelta(minutes=15),
                paid_at=created + timedelta(minutes=4),
                delivered_at=created + timedelta(hours=5),
                status_changed_at=created + timedelta(hours=5),
                metadata={"seed": KEY},
            )
            Order.objects.filter(pk=order.pk).update(created_at=created)
            OrderItem.objects.create(
                order=order,
                position=0,
                product=product,
                name=product.name_uz or product.billz_name or product.billz_id,
                billz_id=product.billz_id,
                quantity=quantity,
                unit_price=product.price,
                line_total=subtotal,
            )
            Payment.objects.create(
                order=order,
                provider=order.payment_provider,
                status=enums.PaymentStatus.PAID,
                amount=subtotal,
                provider_txn_id=f"{KEY}-{n}",
                confirmed_at=created + timedelta(minutes=4),
                provider_state={"click_paydoc_id": f"PD-{n}"} if n % 2 else {},
                created_at=created,
            )
            made += 1
        # one payment stuck in pending: the order's reservation ran out two hours ago, the transaction is open
        customer = customers[0]
        product = products[0]
        created = now - timedelta(hours=2, minutes=30)
        stuck = Order.objects.create(
            customer=customer,
            channel=enums.OrderChannel.ANDROID,
            fulfilment=enums.FulfilmentMethod.PICKUP,
            status=enums.OrderStatus.AWAITING_PAYMENT,
            recipient_name=customer.name,
            recipient_phone=customer.phone or "",
            items_subtotal=product.price,
            total=product.price,
            payment_provider=enums.PaymentProvider.PAYME,
            reservation_minutes=15,
            reservation_expires_at=created + timedelta(minutes=15),
            metadata={"seed": KEY, "stuck": True},
        )
        Order.objects.filter(pk=stuck.pk).update(created_at=created)
        Payment.objects.create(
            order=stuck,
            provider=enums.PaymentProvider.PAYME,
            status=enums.PaymentStatus.PENDING,
            amount=product.price,
            provider_txn_id=f"{KEY}-stuck",
            created_at=created + timedelta(minutes=2),
        )
        return made + 1

    # --- clinic ------------------------------------------------------------------------------------------

    def _appointments(self, now: datetime) -> int:
        if Appointment.objects.filter(staff_note__startswith=KEY).exists():
            return 0
        links = list(ProfessionalService.objects.select_related("professional").order_by("id")[:3])
        customers = list(Customer.objects.filter(phone__isnull=False).order_by("id")[:6])
        if not links or not customers:
            return 0
        day_start, _ = timeutils.day_bounds(timeutils.local_date(now))
        made = 0
        plan = [(10, "attended"), (11, "attended"), (14, "confirmed"), (15, "confirmed"), (17, "requested")]
        for index, (hour, status) in enumerate(plan):
            link = links[index % len(links)]
            start = day_start + timedelta(hours=hour)
            Appointment.objects.create(
                professional=link.professional,
                service=link.service,
                customer=customers[index % len(customers)],
                start=start,
                end=start + timedelta(minutes=link.duration_min),
                duration_min=link.duration_min,
                price=link.price,
                status=status,
                requested_via="app",
                confirmed_at=start - timedelta(days=1) if status in ("confirmed", "attended") else None,
                attended_at=start + timedelta(minutes=5) if status == "attended" else None,
                staff_note=f"{KEY}: demo",
            )
            made += 1
        # a confirmed visit in three days, then the specialist blocks that afternoon: a schedule conflict
        link = links[0]
        start = day_start + timedelta(days=3, hours=15)
        Appointment.objects.create(
            professional=link.professional,
            service=link.service,
            customer=customers[-1],
            start=start,
            end=start + timedelta(minutes=link.duration_min),
            duration_min=link.duration_min,
            price=link.price,
            status="confirmed",
            requested_via="app",
            confirmed_at=now,
            staff_note=f"{KEY}: conflict",
        )
        BlockedPeriod.objects.create(
            professional=link.professional,
            start=day_start + timedelta(days=3, hours=14),
            end=day_start + timedelta(days=3, hours=18),
            kind="off",
            label="Seminar (demo)",
        )
        return made + 1
