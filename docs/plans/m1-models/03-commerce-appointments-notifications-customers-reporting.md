# M1 models — group g3-orchestration: `commerce`, `appointments`, `notifications` (+ `customers`/`reporting` selectors, seed, admin, migrations)

> Design document for slice B2 (`feat/m1-models`). It expands §5.3 and §4 of the approved plan into field-level
> Django models for the three orchestration modules and writes down the cross-cutting pieces the plan assigns to
> this group: the order state machine, `available_actions`, the outbox topic registry, the selectors that
> `customers` and `reporting` need, `seed_demo`, the Django-admin plan and the complete migration order. It is a
> **document**: no code, migrations or commits are produced by it. Sibling documents: `01-…` (accounts,
> integrations, site_config, catalog, professionals) and `02-…` (loyalty, payments, delivery). Where this file
> and `docs/api/admin-api-v1.md` disagree, the contract wins and this file is fixed.

Inputs read: `AGENTS.md`; `docs/api/admin-api-v1.md` (all 1168 lines); the design spec ("Financial integrity and
the settle-now foundations", "Provisional business rules", "M1 admin endpoint inventory", "M2 — Customer (mobile)
API"); `docs/decisions/open-questions.md` (O1–O8, P1–P4, A1, T1–T11); `glowderma/core/{models,db,outbox,tasks,
audit,timeutils}.py`; `glowderma/accounts/models.py`; `core/migrations/0002_auditentry_append_only.py`; the plan
§4.1–4.3 and §5.3; the sibling designs `01-…` and `02-…`; the frontend mock data (`frontend/src/data/mock/*.ts`)
and domain rules (`frontend/src/domain/{order,cashback,appointment,notification,customer}.ts`).

`customers` and `reporting` own **no tables**: they are selector + API modules above everything else (plan §4.1,
decision T2). Their selectors are specified in §6 because they read the tables designed here and in `02-…`.

---

## 0. Conventions and cross-group interface

### 0.1 Hard conventions (applied to every model below)
- Primary keys: `BigAutoField` (`DEFAULT_AUTO_FIELD`). Only `commerce.Order` gets a sequence restart (10001).
- Money: `BigIntegerField`, integer so'm. Percent: `DecimalField(max_digits=5, decimal_places=2)` (only as
  snapshots copied from loyalty; the engine lives in `02-…` §1.9).
- FKs by string reference `"app.Model"`. FKs **to or from** append-only rows: `on_delete=PROTECT`. Actor FKs to
  `accounts.StaffUser` — one rule for all three documents (review minor 22): `PROTECT` on append-only rows
  (required) and on business rows (`Order.cancelled_by`, `Appointment.created_by`, `Campaign.author`; in the other
  groups `Job.created_by`, `Refund.requested_by`, `PosDevice.created_by`); `SET_NULL` on configuration/content rows
  (`StatusTemplate.updated_by` as the skeleton says, `CustomerSegment.created_by`, `SegmentMember.added_by`; in
  `01-…` `Product.last_change_actor`, `ProductImage.uploaded_by`, `SiteSettings.updated_by`,
  `WorkingHoursOverride/BlockedPeriod.created_by`). A staff row is never deleted (`deactivate_staff` flips
  `is_active`), so the distinction is documentation only.
- **Lower-layer services take scalar ids and values**, never a `commerce.Order` instance (mypy would need a
  `TYPE_CHECKING` import of `commerce.models`, which import-linter still counts): `loyalty.create_hold(*,
  customer_id, order_id, amount, expires_at)`, `capture_hold(order_id)`, `release_hold(order_id, reason)`,
  `record_online_purchase(*, customer_id, order_id, items_subtotal, promo_subtotal, cashback_used, at, items,
  billz_order_uuid=None)`, `reverse_purchase(order_id)`, `payments.create_payment(*, order_id, provider, amount)`,
  `delivery.ensure_shipment(order_id)`, `request_bts_create(order_id, *, manual, actor_id)`,
  `mark_created(shipment, response, *, delivery_fee)`; the orchestrator passes the locked order's values
  (review minor 12; 02 §0.1).
- **Layer reads** (review minors 6 and 15, stated once here): a lower module's selector may traverse its *own* FK
  upward (`Shipment.order__status` in `delivery.selectors.bts_failed_count`, `Payment.order__customer__phone` in
  `payments.selectors.payments_list`) but never a reverse relation into an upper module's table; `available_stock`
  therefore lives in `commerce.selectors` (§1.6), never in `catalog`.
- Append-only (`AO`) models subclass `AppendOnlyModel` **and** get a `RunSQL(*core.db.append_only_trigger_sql(table))`
  migration. Event-like AO rows (`OrderEvent`, `AppointmentEvent`, and `LedgerEntry`/`ShipmentEvent` in `02-…`) have
  `at` (business time, `default=timestamp_default`, `editable=False`) and no `updated_at`; `CashbackPolicy`/
  `CashbackTier` keep `created_at` (the contract's `PolicyVersion.created_at`).
- Time-stamped (`TS`) models subclass `TimeStampedModel`. Every other "now" default is `timestamp_default`.
- `CharField` is never `null=True`; "absent" is `""`, rendered as `null` by the serializer where the contract says
  `str | null` (`address`, `cancellation.note`, `items[].image_url`, `blocks[].label`, `link.label`, …).
- Choices: `from glowderma.core import enums`; `choices=enums.<ContractEnumName>.choices` for every enum of
  contract §3.1. States that are **not** contract enums are nested `TextChoices` on the model (marked *internal*),
  as in `02-…`; the serializer maps them. §0.4 lists them so `ENUM_NAME_OVERRIDES` can be seeded.
- `__str__` on every model. Constraint/index names ≤ 63 chars, unique per app, prefixed by the table's short name.
- `CheckConstraint(condition=…)`, `UniqueConstraint(condition=…)` (partial). The exact SQL PostgreSQL ends up with is
  given next to every constraint block so the review does not depend on ORM rendering.
- JSON columns use `encoder=DjangoJSONEncoder` (datetimes, Decimals, UUIDs serialise); provider protocol state, where
  it exists at all in this group, is a JSON column named `provider_state` (only `CampaignRecipient.provider_state`).
- i18n text: three columns `*_uz/_ru/_en` (`StatusTemplate.text_*`). Everything else in this group is stored in one
  language (campaign title/body are authored in one language by design).
- Time: `timeutils.now()` only (module attribute). Tashkent calendar semantics via `timeutils.day_bounds`,
  `local_date`, `month_start`; `Asia/Tashkent` has no DST, so hour buckets are regular.

### 0.2 Append-only tables of this group and their trigger migrations

| Table | Migration |
|---|---|
| `commerce_orderevent` | `commerce/migrations/0003_orderevent_append_only.py` |
| `appointments_appointmentevent` | `appointments/migrations/0002_appointmentevent_append_only.py` (as built; the duration CHECK lives in `0001_initial`) |

Tests per table (B2): ORM `save()` of a loaded row, `QuerySet.update()`, `delete()`, `bulk_update()` raise
`AppendOnlyError`; raw `UPDATE`/`DELETE` raise `append-only table …` on PG 16 and 18; a purge with
`SET LOCAL glowderma.allow_purge = on` is **not** allowed on these two tables (T6 names only `WebhookInbox` and
`AuditEntry`; see §9 note on `glowderma_forbid_mutation()`).

### 0.3 Models of other groups this group relies on (assumptions to reconcile with `01-…` / `02-…`)

| Referenced | What this group relies on |
|---|---|
| `accounts.Customer` (01 §1.1) | `id`, `phone`, `name` (`""` when deleted), `status`, `language`, `registered_at`, `address_text`. Never physically deleted (A1) → `PROTECT` FKs never block. |
| `accounts.Device` (01 §1.2) | `is_active`, `push_enabled`, `fcm_token`, `model/os/app_version/last_active_at`; "pushable device" predicate = `is_active AND push_enabled AND customer.status = 'active'`; at most one active device per customer (M2 rule, backstopped by the partial unique on `fcm_token`). |
| `accounts.Address` (01 §1.3) | `text`, `region_code`, `city_code`, `comment` — snapshotted onto `Order` at checkout. |
| `accounts.StaffUser` | actor FKs; `Actor` shape `{id, display_name}` = `full_name or email`. |
| `catalog.Product` / `ProductStock` / `ProductImage` (01 §4) | `billz_id`, `name_uz`, `billz_name`, `price`, `promo`, `status`; `ProductStock.quantity`, `synced_at` (Billz-owned); first image by `position` → `image_url` snapshot via `core.media.absolute_media_url`. |
| `professionals.*` (01 §5) | `Professional(role, position, is_active)`, `Service(name_uz, short_name_uz)`, `ProfessionalService(price, duration_min)` (snapshot source), `WorkingHours`/`WorkingHoursOverride`/`BlockedPeriod` and the day-schedule derivation 01 §5.8 (the appointments of the day come from §2 here). |
| `site_config.SiteSettings` (01 §3.1) | `reservation_minutes` (snapshotted onto `Order.reservation_minutes` at checkout). |
| `loyalty.*` (02 §1) | `services.create_hold/capture_hold/release_hold/record_online_purchase/reverse_purchase/adjust/post_entry` (scalar signatures, §0.1); `selectors.customer_totals/account_summary/tier_for/ledger_for_customer/ledger_for_order/purchases_for_customer`; `LedgerEntry.order` FK → this group's `Order` (`PROTECT`); `LedgerEntry.at` is the business time (API `at`). |
| `payments.*` (02 §2) | `services.create_payment/confirm_payment/fail_payment/create_refund/mark_*`; `selectors.order_payment` (display payment), `revenue_buckets`, `refunds_needing_action`; `Payment.order` FK → `Order` (`PROTECT`). |
| `delivery.*` (02 §3) | `services.ensure_shipment/request_bts_create/mark_created/mark_failed/apply_status/cancel`; `selectors.shipment_for_order` (derived `pending`), `bts_failed_count`; `Shipment.order` 1:1 → `Order` (`PROTECT`); internal `cancelled` state rendered as `created` (D-1). |
| `integrations.*` (01 §2) | `WebhookInbox` for raw callbacks (the webhook views in `commerce/api/webhooks/` insert it after processing, T5); `IntegrationState(provider='billz').last_sync_at`. |
| `core.OutboxMessage` | `outbox.enqueue(topic, payload, dedupe_key=, delay=)` is `get_or_create(dedupe_key)`; dispatcher backoff `min(2**attempts, 3600) s`, `dead` after 10 attempts with audit `outbox.dead`; `delivery.Shipment.create_message` FK derives `pending`. |

Migration dependency note (G-1 in `02-…`): `loyalty`, `payments` and `delivery` reference `commerce.Order`, and
`commerce` references only `accounts` and `catalog`; `commerce.0001` therefore precedes them (§9). Import-linter
layering is unaffected (string FKs do not import).

### 0.4 Internal (non-contract) enums introduced by this group

| Where | Nested `TextChoices` | Literals | API mapping |
|---|---|---|---|
| `StockReservation.State` | internal | `active`, `converted`, `released` | never rendered |
| `StockReservation.ReleaseReason` | internal | `expired`, `cancelled`, `payment_failed` | never rendered |
| `Appointment.CancelledBy` | internal (M2) | `customer`, `admin` | M2 only |
| `AppointmentEvent.Type` | internal | `requested`, `confirmed`, `rejected`, `attended`, `cancelled`, `rescheduled` | not rendered in M1 |
| `AppointmentEvent.Source` | internal | `system`, `admin`, `customer` | not rendered in M1 |
| `Campaign.State` | internal | `scheduled`, `queued`, `sending`, `sent` | `sending` → `queued` (contract `NotificationState`) |
| `CampaignRecipient.State` | internal | `pending`, `sent`, `failed`, `skipped` | never rendered |
| `InboxNotification.Kind` | internal | `promo`, `general`, `order_status`, `appointment` | M2 `/me/notifications` |
| `InboxNotification.SourceType` | internal | `campaign`, `order`, `appointment` | M2 |
| `InboxNotification.PushState` | internal | `no_device`, `queued`, `sending`, `sent`, `failed` | never rendered |

Contract enums used here (names exactly as §3.1): `OrderStatus`, `PaymentProvider`, `FulfilmentMethod`,
`OrderChannel`, `EventSource`, `OrderEventType`, `CancellationReason`, `AppointmentStatus` (T3: carries the extra
`cancelled` literal), `RequestedVia`, `NotificationKind`, `AudienceType`, `LinkKind`, `TemplateStatus`,
`ContentLanguage`. `ENUM_NAME_OVERRIDES` (B0) must name each `status`/`state`/`kind`/`type` serializer field.

---

## 1. `commerce`

### 1.0 Module overview
```
commerce/
  models.py        Cart (TS, M2), CartItem (M2), Order (TS), OrderItem, OrderEvent (AO), StockReservation (TS)
  services.py      checkout (M2), confirm_payment_effects (after-payment orchestration), fail_payment_effects,
                   cancel_order, transition, retry_shipment, apply_bts_status, expire_reservation,
                   record_event (the only OrderEvent writer), available_actions
  selectors.py     orders_list (filters/search/period/export rows), status_counts, order_detail (items, history,
                   payment, refunds, shipment union, cashback, cashback_hold, available_actions),
                   available_stock, customer_last_order, processing_count (nav), orders_in_buckets (dashboard)
  tasks.py         outbox handlers: bts.create_shipment, billz.push_sale, refund.submit; beat:
                   commerce.expire_reservations (1 min), commerce.poll_bts (10 min)
  api/admin/       orders views (B5); api/webhooks/ click, payme, bts callback views (B7 / M2)
  seed.py          seeder (§7)
```
Rule reminders: the order row is locked first in every flow (Order → Payment → LoyaltyAccount); `OrderEvent` rows
are written only by `commerce.services.record_event()`; upward effects (`notifications`) are outbox topics.

### 1.1 `Cart` (TS, M2)
The customer's server-side shopping cart: one row per customer, emptied by a successful checkout.

```python
class Cart(TimeStampedModel):
    customer = models.OneToOneField("accounts.Customer", on_delete=models.CASCADE, related_name="cart")
    last_checkout_order = models.ForeignKey(
        "commerce.Order", null=True, on_delete=models.SET_NULL, related_name="+"
    )  # the order the last successful checkout produced (debug aid; the M2 checkout is @idempotent anyway)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"Cart of customer {self.customer_id}"
```
```sql
ALTER TABLE commerce_cart ADD CONSTRAINT commerce_cart_customer_id_key UNIQUE (customer_id);   -- OneToOne
```

**Invariants (services)**
- `get_or_create` on first use (`commerce.services.cart_for(customer)`); `updated_at` is bumped on every line change
  (`TimeStampedModel.save`) so the app can show "cart updated".
- Checkout (M2) reads the lines under `select_for_update()` of the cart row, validates stock (`available_stock`,
  §1.6) and limits, creates the `Order` and deletes the lines in the same transaction.
- Cart lines carry no price snapshot: prices are read live at checkout (the app re-renders totals from the
  `GET /cart` response, which quotes the current `Product.price`, `promo`, availability and `max_reached`).
- The `CASCADE` from `Customer` never fires in practice (A1); it is the semantically right choice for a cart.

**Contract fields served** — none in admin v1. M2: `GET/POST/PATCH/DELETE /api/v1/cart…` (`items[] {product_id,
quantity, unit_price, promo, available, max_reached}`, `items_subtotal`, `promo_subtotal`, `OUT_OF_STOCK` errors).

**Open points**
- C-1 Whether the cart should also persist the checkout draft (fulfilment, address id, cashback to use, provider)
  between app screens. Default: no — the checkout request carries them; a draft would duplicate validation.

### 1.2 `CartItem` (M2)
One product line of a cart.

```python
class CartItem(models.Model):
    cart = models.ForeignKey("commerce.Cart", on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="cart_items")
    quantity = models.PositiveSmallIntegerField()
    added_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        ordering = ["cart_id", "added_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["cart", "product"], name="cartitem_cart_product_unique"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, quantity__lte=99), name="cartitem_quantity_1_99"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity} × product {self.product_id} (cart {self.cart_id})"
```
```sql
ALTER TABLE commerce_cartitem ADD CONSTRAINT cartitem_cart_product_unique UNIQUE (cart_id, product_id);
ALTER TABLE commerce_cartitem ADD CONSTRAINT cartitem_quantity_1_99 CHECK (quantity >= 1 AND quantity <= 99);
```

**Invariants (services)** — `CART_MAX_LINES` (default 30) and `CART_MAX_QTY_PER_LINE` (default 10; the DB check is
the hard ceiling) live in settings; exceeding either is the M2 `max_reached` error. Lines pointing at a product that
became `hidden` or has `available_stock ≤ 0` stay in the cart but are flagged in the response and refused at
checkout (`OUT_OF_STOCK`). `CASCADE` on `product` is deliberate: carts are ephemeral and must never block a product
row (products are not deleted by any API; this only matters for manual clean-ups).

**Contract fields served** — none in admin v1; M2 cart endpoints.

**Open points** — C-2 per-line quantity ceiling (10 vs 99) is a business question; both are settings.

### 1.3 `Order` (TS)
A customer's online order: the immutable commercial snapshot (items, amounts, address) plus the mutable lifecycle
status; its `id` is the order number shown everywhere (`№10248`).

```python
class Order(TimeStampedModel):
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="orders")
    channel = models.CharField(max_length=8, choices=enums.OrderChannel.choices)  # ios|android
    fulfilment = models.CharField(max_length=16, choices=enums.FulfilmentMethod.choices)  # delivery|pickup
    status = models.CharField(
        max_length=24, choices=enums.OrderStatus.choices, default=enums.OrderStatus.AWAITING_PAYMENT
    )
    # delivery snapshot (delivery only; "" for pickup)
    address_text = models.CharField(max_length=300, blank=True)
    address_region_code = models.CharField(max_length=16, blank=True)  # BTS region id from accounts.Address
    address_city_code = models.CharField(max_length=16, blank=True)  # BTS city id
    address_comment = models.CharField(max_length=200, blank=True)
    recipient_name = models.CharField(max_length=150, blank=True)  # D-3: BTS needs a name after a scrub
    recipient_phone = models.CharField(max_length=13, blank=True)
    customer_note = models.CharField(max_length=300, blank=True)  # M2 "izoh"
    # money (so'm)
    items_subtotal = models.BigIntegerField()  # Σ OrderItem.line_total
    promo_subtotal = models.BigIntegerField(default=0)  # Σ line_total WHERE promo
    delivery_fee = models.BigIntegerField(default=0)  # quoted BTS tariff at checkout; 0 for pickup
    cashback_used = models.BigIntegerField(default=0)  # = CashbackHold.amount
    total = models.BigIntegerField()  # items_subtotal + delivery_fee − cashback_used
    payment_provider = models.CharField(
        max_length=16, choices=enums.PaymentProvider.choices
    )  # chosen at checkout
    # reservation
    reservation_minutes = models.PositiveSmallIntegerField()  # SiteSettings.reservation_minutes at checkout
    reservation_expires_at = models.DateTimeField(
        null=True
    )  # set while awaiting_payment; kept afterwards for history
    # lifecycle
    status_changed_at = models.DateTimeField(default=timestamp_default)
    paid_at = models.DateTimeField(null=True)
    handed_to_courier_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    cancellation_reason = models.CharField(
        max_length=24, blank=True, choices=enums.CancellationReason.choices
    )  # reservation_expired|admin|customer_request
    cancellation_note = models.CharField(max_length=500, blank=True)  # admin reason text (3..500)
    cancelled_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="cancelled_orders"
    )
    metadata = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder
    )  # internal; keys in the table below

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    total=models.F("items_subtotal") + models.F("delivery_fee") - models.F("cashback_used")
                ),
                name="order_total_formula",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    items_subtotal__gte=0,
                    promo_subtotal__gte=0,
                    delivery_fee__gte=0,
                    cashback_used__gte=0,
                    total__gte=0,
                    promo_subtotal__lte=models.F("items_subtotal"),
                    cashback_used__lte=models.F("items_subtotal"),
                ),
                name="order_amounts_range",
            ),
            models.CheckConstraint(
                condition=(models.Q(fulfilment="delivery") & ~models.Q(address_text=""))
                | models.Q(
                    fulfilment="pickup",
                    address_text="",
                    address_region_code="",
                    address_city_code="",
                    delivery_fee=0,
                ),
                name="order_address_by_fulfilment",
            ),
            models.CheckConstraint(
                condition=models.Q(recipient_phone="") | models.Q(recipient_phone__regex=r"^\+998\d{9}$"),
                name="order_recipient_phone_e164",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="cancelled", cancelled_at__isnull=False)
                    & ~models.Q(cancellation_reason="")
                )
                | models.Q(
                    status__in=["awaiting_payment", "processing", "handed_to_courier", "delivered"],
                    cancelled_at__isnull=True,
                    cancellation_reason="",
                    cancellation_note="",
                    cancelled_by__isnull=True,
                ),
                name="order_cancellation_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cancellation_reason="admin", cancelled_by__isnull=False)
                    & ~models.Q(cancellation_note="")
                )
                | models.Q(
                    cancellation_reason__in=["reservation_expired", "customer_request"],
                    cancelled_by__isnull=True,
                )
                | models.Q(cancellation_reason=""),
                name="order_cancellation_by_reason",
            ),
            models.CheckConstraint(
                condition=models.Q(status="awaiting_payment", paid_at__isnull=True)
                | models.Q(status__in=["processing", "handed_to_courier", "delivered"], paid_at__isnull=False)
                | models.Q(status="cancelled"),
                name="order_paid_at_by_status",
            ),
            models.CheckConstraint(
                condition=models.Q(status="delivered", delivered_at__isnull=False)
                | (~models.Q(status="delivered") & models.Q(delivered_at__isnull=True)),
                name="order_delivered_at_by_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="awaiting_payment")
                | models.Q(reservation_expires_at__isnull=False),
                name="order_awaiting_has_expiry",
            ),
            models.CheckConstraint(
                condition=models.Q(reservation_minutes__gte=5, reservation_minutes__lte=60),
                name="order_reservation_minutes_5_60",
            ),
        ]
        indexes = [
            models.Index(fields=["status"], name="order_status_idx"),
            models.Index(fields=["created_at", "id"], name="order_created_idx"),
            models.Index(fields=["customer", "-created_at", "-id"], name="order_customer_created_idx"),
            models.Index(
                fields=["reservation_expires_at"],
                condition=models.Q(status="awaiting_payment"),
                name="order_awaiting_expiry_idx",
            ),
            models.Index(fields=["payment_provider"], name="order_provider_idx"),
        ]

    def __str__(self) -> str:
        return f"Order №{self.pk} ({self.status}, {self.total} so'm, customer {self.customer_id})"
```
Exact SQL:
```sql
ALTER TABLE commerce_order ADD CONSTRAINT order_total_formula
  CHECK (total = items_subtotal + delivery_fee - cashback_used);
ALTER TABLE commerce_order ADD CONSTRAINT order_amounts_range CHECK (
  items_subtotal >= 0 AND promo_subtotal >= 0 AND delivery_fee >= 0 AND cashback_used >= 0 AND total >= 0
  AND promo_subtotal <= items_subtotal AND cashback_used <= items_subtotal);
ALTER TABLE commerce_order ADD CONSTRAINT order_address_by_fulfilment CHECK (
  (fulfilment = 'delivery' AND address_text <> '')
  OR (fulfilment = 'pickup' AND address_text = '' AND address_region_code = '' AND address_city_code = '' AND delivery_fee = 0));
ALTER TABLE commerce_order ADD CONSTRAINT order_recipient_phone_e164 CHECK (recipient_phone = '' OR recipient_phone ~ '^\+998\d{9}$');
ALTER TABLE commerce_order ADD CONSTRAINT order_cancellation_fields CHECK (
  (status = 'cancelled' AND cancelled_at IS NOT NULL AND cancellation_reason <> '')
  OR (status IN ('awaiting_payment','processing','handed_to_courier','delivered')
      AND cancelled_at IS NULL AND cancellation_reason = '' AND cancellation_note = '' AND cancelled_by_id IS NULL));
ALTER TABLE commerce_order ADD CONSTRAINT order_cancellation_by_reason CHECK (
  (cancellation_reason = 'admin' AND cancelled_by_id IS NOT NULL AND cancellation_note <> '')
  OR (cancellation_reason IN ('reservation_expired','customer_request') AND cancelled_by_id IS NULL)
  OR cancellation_reason = '');
ALTER TABLE commerce_order ADD CONSTRAINT order_paid_at_by_status CHECK (
  (status = 'awaiting_payment' AND paid_at IS NULL)
  OR (status IN ('processing','handed_to_courier','delivered') AND paid_at IS NOT NULL)
  OR status = 'cancelled');
ALTER TABLE commerce_order ADD CONSTRAINT order_delivered_at_by_status CHECK (
  (status = 'delivered' AND delivered_at IS NOT NULL) OR (status <> 'delivered' AND delivered_at IS NULL));
ALTER TABLE commerce_order ADD CONSTRAINT order_awaiting_has_expiry CHECK (status <> 'awaiting_payment' OR reservation_expires_at IS NOT NULL);
ALTER TABLE commerce_order ADD CONSTRAINT order_reservation_minutes_5_60 CHECK (reservation_minutes >= 5 AND reservation_minutes <= 60);
CREATE INDEX order_status_idx           ON commerce_order (status);
CREATE INDEX order_created_idx          ON commerce_order (created_at, id);
CREATE INDEX order_customer_created_idx ON commerce_order (customer_id, created_at DESC, id DESC);
CREATE INDEX order_awaiting_expiry_idx  ON commerce_order (reservation_expires_at) WHERE status = 'awaiting_payment';
CREATE INDEX order_provider_idx         ON commerce_order (payment_provider);
-- commerce/migrations/0002_order_sequence.py (RunSQL; reverse: START WITH 1 RESTART WITH 1)
ALTER SEQUENCE commerce_order_id_seq START WITH 10001 RESTART WITH 10001;
```
`START WITH` makes the intent visible in `pg_sequences.start_value` (asserted by a migration test); `RESTART`
moves the current value. The seeder's `setval` (§7.9) keeps the sequence above both the seeded maximum and 10000.

`metadata` keys (internal, never rendered; all optional):

| Key | Type | Meaning |
|---|---|---|
| `seed` | bool | row written by `seed_demo` |
| `fake.bts.fail_attempts` | int | fake BTS adapter fails the first *n* create attempts (reproduces "shipment failed → retry") |
| `fake.bts.tracking_number`, `fake.bts.tariff` | str, int | fake BTS returns these instead of derived values (design numbers) |
| `fake.bts.cancel` | `"refused"` \| `"unavailable"` | fake BTS cancel answer: status ≠ 100 (409 `not_cancellable`) / transport failure (503) |
| `fake.click.transaction_id`, `fake.payme.transaction_id` | str | fake provider transaction ids (design numbers, e.g. `CLK-7741920385`) |
| `fake.refund` | `"succeeded"` \| `"failed"` \| `"unknown"` | fake Click refund outcome |
| `checkout.app_version`, `checkout.device_id` | str, int | diagnostics from M2 checkout |

**Invariants (services)**
- `total == items_subtotal + delivery_fee − cashback_used` (DB check) and `items_subtotal == Σ OrderItem.line_total`,
  `promo_subtotal == Σ line_total WHERE promo` (service invariant, asserted by the checkout test and by the nightly
  consistency query in §6.7).
- `cashback_used > 0 ⇔` exactly one `CashbackHold` exists for the order (02 §1.5) and `cashback_used == hold.amount`.
- `payment_provider` is the provider chosen at checkout; the *display* payment is `payments.selectors.order_payment`
  (02 §2.1); a provider switch (M2) updates `payment_provider` together with the new `Payment` row.
- `status` changes only inside `commerce.services` under `select_for_update(of=("self",))` of the order row, always
  with `status_changed_at = now`, the matching timestamp column (`paid_at`, `handed_to_courier_at`, `delivered_at`,
  `cancelled_at`) and exactly one status-changing `OrderEvent` in the same transaction (§4).
- `address_*`, `recipient_*`, items and amounts never change after checkout (no service writes them; Django admin
  is read-only). `reservation_expires_at` is written once at checkout and never cleared (history).
- Lock order: Order → (ProductStock rows, by product id, checkout only) → Payment → LoyaltyAccount (02 §1.9.2).

**Contract fields served**
- `OrderSummary`: `id`, `customer` (`CustomerRef` from `customer`), `created_at`, `total`, `status`, `fulfilment`,
  `payment {status, provider}` (02 §2.1 display payment; fallback `{pending, payment_provider}` when no row),
  `shipment` (02 §3.1 union; `not_required` for pickup), `cancellation` (`{reason, note: NULLIF(note,'')}` or `null`).
- `Order` (detail): `channel`, `address = NULLIF(address_text, '')`, `items` (§1.4), `items_subtotal`,
  `promo_subtotal`, `delivery_fee`, `cashback_used`, `payment {…, amount, transaction_id, confirmed_at}`, `refunds[]`,
  `cashback_hold`, `cashback[]` (02 §4.4), `history[]` (§1.5), `available_actions` (§4.2).
- `GET orders/` filters: `status` (column), `search` (§6.1), `fulfilment` (column), `payment` (display payment status,
  02 §2.1 `EXISTS`), `period`/`date_from`/`date_to` on `created_at` with Tashkent semantics (§6.1), `customer`
  (column), `ordering ±created_at` ties by `id` same direction; page sizes 6/10/25/50; export `buyurtmalar`:
  `№ (id); Mijoz (customer.name); Telefon; Sana (fmt_dt created_at); Summa (so‘m) (total); To‘lov (payment status
  label); Usul (provider label); Olish usuli (fulfilment label); Holat (status label); BTS (tracking number or "")`.
- `GET orders/status-counts/` (§6.2), `nav-counts.orders_processing`, dashboard `orders` per bucket and
  `previous_period_orders` (§6.4), customer card `online_orders.last_order_id/last_order_at` (§6.3).
- Audit `order.cancelled`, `order.status_changed`, `order.shipment_retry_requested` carry `entity_type="order"`,
  `entity_id=str(id)`.

**Open points**
- C-3 `recipient_name/phone` snapshot (D-3 from `02-…`): taken from `Customer.name/phone` at checkout so a later
  scrub (A1) does not break BTS create. Confirm BTS requires a recipient name.
- C-4 `cashback_used ≤ items_subtotal` (not `≤ items_subtotal + delivery_fee`): cashback never pays the delivery
  fee. Business confirmation needed (affects the M2 checkout UI).
- C-5 `handed_to_courier_at` is kept although no contract field reads it (the events carry the time); cheap, useful
  for the BTS SLA report (M3).
- C-6 Customer-initiated cancel (`customer_request`) is modelled (reason literal exists in the contract) but no M2
  endpoint is in the plan; default: M2 allows it only from `awaiting_payment` (no refund path needed).

### 1.4 `OrderItem`
One product line of an order, fully snapshotted at checkout (name, Billz id, image, unit price, promo flag).

```python
class OrderItem(models.Model):
    order = models.ForeignKey("commerce.Order", on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="order_items")
    name = models.CharField(max_length=200)  # Product.name_uz, else billz_name, at checkout
    billz_id = models.CharField(max_length=14)
    image_url = models.CharField(
        max_length=500, blank=True
    )  # absolute URL of images[0] at checkout; "" → null
    quantity = models.PositiveIntegerField()
    unit_price = models.BigIntegerField()  # Product.price at checkout
    promo = models.BooleanField(default=False)
    line_total = models.BigIntegerField()  # quantity × unit_price

    class Meta:
        ordering = ["order_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["order", "position"], name="orderitem_position_unique"),
            models.UniqueConstraint(fields=["order", "product"], name="orderitem_product_unique"),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=1, unit_price__gte=0), name="orderitem_amounts"
            ),
            models.CheckConstraint(
                condition=models.Q(line_total=models.F("quantity") * models.F("unit_price")),
                name="orderitem_line_total",
            ),
            models.CheckConstraint(condition=~models.Q(name=""), name="orderitem_name_required"),
        ]

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity} @ {self.unit_price} (order {self.order_id})"
```
```sql
ALTER TABLE commerce_orderitem ADD CONSTRAINT orderitem_position_unique UNIQUE (order_id, position);
ALTER TABLE commerce_orderitem ADD CONSTRAINT orderitem_product_unique  UNIQUE (order_id, product_id);
ALTER TABLE commerce_orderitem ADD CONSTRAINT orderitem_amounts    CHECK (quantity >= 1 AND unit_price >= 0);
ALTER TABLE commerce_orderitem ADD CONSTRAINT orderitem_line_total CHECK (line_total = quantity * unit_price);
ALTER TABLE commerce_orderitem ADD CONSTRAINT orderitem_name_required CHECK (name <> '');
```

**Invariants (services)** — written once by checkout (and by the seeder) in cart order; `image_url` is computed with
`core.media.absolute_media_url(first_image.file.name)` at checkout and **not** refreshed when product photos change
(the detail screen shows what the customer saw). `PurchaseItem` (02 §1.7) is copied from these rows after payment.
`PROTECT` on `product` is the backstop for "products are never deleted"; the `CASCADE` from `order` never fires
(orders are never deleted) but is the right semantics.

**Contract fields served** — `Order.items[] {product_id, name, billz_id, image_url (""→null), quantity, unit_price,
promo}`; `order_created.data.item_count = COUNT(*)` (lines, not units — the FE text "3 ta mahsulot" for №10248 counts
lines: ceramide cream, 2 × foam, SPF 50); the dashboard never reads items.

**Open points** — C-7 `item_count` = lines (as the FE mock does) vs units; the contract example (`item_count: 3`
for 1 + 2 + 1 units) settles it as lines.

### 1.5 `OrderEvent` (AO)
One immutable, typed entry of an order's history (contract §4): what happened, from which source, which status it
produced, and the structured data the FE renders into Uzbek strings.

```python
class OrderEvent(AppendOnlyModel):
    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="events")
    type = models.CharField(max_length=32, choices=enums.OrderEventType.choices)
    source = models.CharField(max_length=8, choices=enums.EventSource.choices)  # system|bts|admin
    status_after = models.CharField(max_length=24, blank=True, choices=enums.OrderStatus.choices)  # "" → null
    actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="order_events"
    )
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["order", "at", "id"], name="orderevent_order_at_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source="admin", actor__isnull=False)
                | (~models.Q(source="admin") & models.Q(actor__isnull=True)),
                name="orderevent_actor_by_source",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(type="order_created", status_after="awaiting_payment")
                    | models.Q(type="payment_confirmed", status_after="processing")
                    | models.Q(type="handed_to_courier", status_after="handed_to_courier")
                    | models.Q(type="delivered", status_after="delivered")
                    | models.Q(type__in=["cancelled", "reservation_expired"], status_after="cancelled")
                    | models.Q(type="status_changed", status_after__in=["handed_to_courier", "delivered"])
                    | models.Q(
                        type__in=[
                            "reservation_created",
                            "billz_sale_recorded",
                            "cashback_credited",
                            "payment_failed",
                            "shipment_created",
                            "shipment_failed",
                            "refund_requested",
                            "refund_succeeded",
                            "refund_failed",
                        ],
                        status_after="",
                    )
                ),
                name="orderevent_status_by_type",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        type__in=[
                            "order_created",
                            "reservation_created",
                            "payment_confirmed",
                            "billz_sale_recorded",
                            "cashback_credited",
                            "payment_failed",
                            "reservation_expired",
                            "refund_succeeded",
                            "refund_failed",
                        ],
                        source="system",
                    )
                    | models.Q(type__in=["handed_to_courier", "delivered"], source="bts")
                    | models.Q(type="status_changed", source="admin")
                    | models.Q(
                        type__in=["shipment_created", "shipment_failed", "refund_requested", "cancelled"],
                        source__in=["system", "admin"],
                    )
                ),
                name="orderevent_source_by_type",
            ),
        ]

    def __str__(self) -> str:
        return f"Order {self.order_id}: {self.type} ({self.source}) at {self.at:%Y-%m-%d %H:%M}"
```
```sql
CREATE INDEX orderevent_order_at_idx ON commerce_orderevent (order_id, at, id);
ALTER TABLE commerce_orderevent ADD CONSTRAINT orderevent_actor_by_source CHECK (
  (source = 'admin' AND actor_id IS NOT NULL) OR (source <> 'admin' AND actor_id IS NULL));
ALTER TABLE commerce_orderevent ADD CONSTRAINT orderevent_status_by_type CHECK (
  (type = 'order_created' AND status_after = 'awaiting_payment')
  OR (type = 'payment_confirmed' AND status_after = 'processing')
  OR (type = 'handed_to_courier' AND status_after = 'handed_to_courier')
  OR (type = 'delivered' AND status_after = 'delivered')
  OR (type IN ('cancelled','reservation_expired') AND status_after = 'cancelled')
  OR (type = 'status_changed' AND status_after IN ('handed_to_courier','delivered'))
  OR (type IN ('reservation_created','billz_sale_recorded','cashback_credited','payment_failed','shipment_created',
               'shipment_failed','refund_requested','refund_succeeded','refund_failed') AND status_after = ''));
ALTER TABLE commerce_orderevent ADD CONSTRAINT orderevent_source_by_type CHECK (
  (type IN ('order_created','reservation_created','payment_confirmed','billz_sale_recorded','cashback_credited',
            'payment_failed','reservation_expired','refund_succeeded','refund_failed') AND source = 'system')
  OR (type IN ('handed_to_courier','delivered') AND source = 'bts')
  OR (type = 'status_changed' AND source = 'admin')
  OR (type IN ('shipment_created','shipment_failed','refund_requested','cancelled') AND source IN ('system','admin')));
-- + append-only trigger (0.2): commerce/migrations/0003_orderevent_append_only.py
```

**The 16 event types, their `data` keys (JSON Schema-like; all keys always present) and who writes them**

| `type` | `source` | `status_after` | `data` | Writer (service / handler) |
|---|---|---|---|---|
| `order_created` | system | `awaiting_payment` | `{"channel": OrderChannel, "item_count": int (lines), "total": int}` | `checkout` |
| `reservation_created` | system | — | `{"item_count": int, "minutes": int, "expires_at": iso}` | `checkout` (same `at` as `order_created`) |
| `payment_confirmed` | system | `processing` | `{"provider": PaymentProvider, "transaction_id": str, "amount": int, "reservation_converted": bool}` | `confirm_payment_effects` |
| `billz_sale_recorded` | system | — | `{"billz_order_id": str (uuid)}` | `tasks.billz.push_sale` handler |
| `cashback_credited` | system | — | `{"ledger_entry_id": int, "amount": int, "base": int, "percent": "1.00", "policy_version": int}` | `confirm_payment_effects` (same `at` as `payment_confirmed`; only when credit > 0) |
| `payment_failed` | system | — | `{"provider": PaymentProvider, "transaction_id": str\|null, "error_code": str\|null}` | `fail_payment_effects` — **provider callbacks only** (Click `complete` with `error < 0`, Payme `CancelTransaction` on a state-1 transaction, including one that arrives after the order was already cancelled/expired); the order stays `awaiting_payment` while payable. Expiry, admin cancel and provider switch fail the payment **without** this event (C-8, 02 §2.1) |
| `shipment_created` | system / admin | — | `{"tracking_number": str, "tariff": int, "manual": bool}` | `tasks.bts.create_shipment` handler (`admin` + `actor` from payload when `manual`) |
| `shipment_failed` | system / admin | — | `{"attempt": int, "manual": bool, "error_code": str\|null}` | same handler / dead-letter hook |
| `status_changed` | admin | `to` | `{"from": OrderStatus, "to": OrderStatus, "push_sent": bool}` | `transition` |
| `handed_to_courier` | bts | `handed_to_courier` | `{"via": "webhook"\|"polling", "bts_status": str, "push_sent": bool}` | `apply_bts_status` |
| `delivered` | bts | `delivered` | `{"via", "bts_status", "push_sent"}` | `apply_bts_status` |
| `cancelled` | admin / system (`customer_request`) | `cancelled` | `{"reason": CancellationReason, "note": str\|null, "refund_id": int\|null, "cashback_reversed": bool, "stock_released": bool, "bts_cancelled": bool, "push_sent": bool}` | `cancel_order` |
| `reservation_expired` | system | `cancelled` | `{"released_item_count": int, "payment_failed": bool}` | `expire_reservation` |
| `refund_requested` | admin / system | — | `{"refund_id": int, "amount": int, "provider": PaymentProvider, "manual": bool}` | `cancel_order` (admin) / late-capture auto refund (system, M2) |
| `refund_succeeded` | system | — | `{"refund_id": int, "amount": int, "provider": PaymentProvider}` | `tasks.refund.submit` handler / Payme `CancelTransaction` view (M2) |
| `refund_failed` | system | — | `{"refund_id": int, "amount": int, "provider": PaymentProvider, "state": "failed"\|"unknown", "error_code": str\|null}` | `tasks.refund.submit` handler; the dead-letter hook `on_outbox_dead` (§5) with `{state: "failed", error_code: "dead_letter"}` |

`push_sent` (plan §4.3) = "the customer had a pushable device at event time": evaluated inside the writing
transaction as `Device.objects.filter(customer=order.customer, is_active=True, push_enabled=True,
customer__status="active").exists()`; the push itself is asynchronous (`push.order_status`).

**Invariants (services)**
- Written only by `commerce.services.record_event(order, type, *, source, status_after="", actor=None, data, at=None)`
  while the caller holds the order lock; `at` defaults to the transaction's `timeutils.now()` so sibling events of one
  step share the same instant (`order_created` + `reservation_created`; `payment_confirmed` + `cashback_credited`),
  and the FE folding (§5.1 of the contract) groups them by type, not time.
- Exactly one status-changing event per status change (`status_after != ""`), in the same transaction as the
  `Order.status` update; `Order.status` always equals the `status_after` of the newest status-changing event
  (checked by the nightly consistency query, §6.7).
- Handlers that run at-least-once (`billz_sale_recorded`, `shipment_created`, `shipment_failed`, `refund_*`) check
  under the order lock whether an event with the same `type` and the same discriminating data key already exists
  (`billz_order_id`, `tracking_number`, `attempt`, `refund_id` + `type`) before inserting; the DB cannot express
  this uniqueness over JSON cheaply and the handlers' provider-boundary guards (§5) already make duplicates rare.
- `data` keys are validated by `record_event` against the table above (a `TypedDict` per type in
  `commerce/events.py`), so a missing key fails the writing transaction, not the reader.

**Contract fields served** — `Order.history[]` (`ORDER BY at DESC, id DESC`): `id`, `type`, `at`, `source`,
`status_after` (`""` → `null`), `actor` (`Actor`|null via `select_related("actor")`), `data` as stored. The §4
№10248 vector (seven rows) is produced by the seeder (§7.6) and asserted by B5. `cancelled.data.refund_id` folds the
matching `refund_requested` row in the FE mapper.

**Open points**
- C-8 No event is written for the Payment `pending → failed` transition that an *admin cancel* or the *expiry job*
  causes (`cancelled`/`reservation_expired` carry `payment_failed`/`refund_id` instead); `payment_failed` is reserved
  for provider-reported failures. **Decided** (review major 7): contract §4 defines `payment_failed` as the provider
  failure row ("To‘lov o‘tmadi" / "… to‘lov rad etildi") and §2.2 lists only `cancelled` (+ `refund_requested`) as
  the effects of an `awaiting_payment` cancel; `02-…` §2.1 now states the same rule.
- C-9 `shipment_created.source = admin` for a manual retry requires the staff id in the outbox payload (`actor_id`);
  if the staff row were deactivated meanwhile the event still references it (allowed).

### 1.6 `StockReservation` (TS)
Units of a product held for an unpaid order so two checkouts cannot sell the last unit; converted into the sale on
payment, released on expiry/cancel.

```python
class StockReservation(TimeStampedModel):
    class State(models.TextChoices):  # internal
        ACTIVE = "active"
        CONVERTED = "converted"
        RELEASED = "released"

    class ReleaseReason(models.TextChoices):  # internal
        EXPIRED = "expired"
        CANCELLED = "cancelled"
        PAYMENT_FAILED = "payment_failed"

    order = models.ForeignKey("commerce.Order", on_delete=models.CASCADE, related_name="reservations")
    product = models.ForeignKey("catalog.Product", on_delete=models.PROTECT, related_name="reservations")
    quantity = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.ACTIVE)
    expires_at = models.DateTimeField()  # = Order.reservation_expires_at
    converted_at = models.DateTimeField(null=True)
    released_at = models.DateTimeField(null=True)
    release_reason = models.CharField(max_length=16, blank=True, choices=ReleaseReason.choices)

    class Meta:
        ordering = ["order_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product"],
                condition=models.Q(state="active"),
                name="reservation_one_active_per_line",
            ),
            models.CheckConstraint(condition=models.Q(quantity__gte=1), name="reservation_quantity_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="active", converted_at__isnull=True, released_at__isnull=True, release_reason=""
                    )
                    | models.Q(
                        state="converted",
                        converted_at__isnull=False,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | models.Q(state="released", converted_at__isnull=True, released_at__isnull=False)
                    & ~models.Q(release_reason="")
                ),
                name="reservation_state_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product"], condition=models.Q(state="active"), name="reservation_active_product_idx"
            ),
            models.Index(
                fields=["product", "converted_at"],
                condition=models.Q(state="converted"),
                name="reservation_converted_prod_idx",  # ≤ 30 chars (E034)
            ),
            models.Index(
                fields=["expires_at"],
                condition=models.Q(state="active"),
                name="reservation_active_expires_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Reservation {self.quantity} × product {self.product_id} for order {self.order_id} ({self.state})"
```
```sql
CREATE UNIQUE INDEX reservation_one_active_per_line ON commerce_stockreservation (order_id, product_id) WHERE state = 'active';
ALTER TABLE commerce_stockreservation ADD CONSTRAINT reservation_quantity_positive CHECK (quantity >= 1);
ALTER TABLE commerce_stockreservation ADD CONSTRAINT reservation_state_fields CHECK (
  (state = 'active'    AND converted_at IS NULL     AND released_at IS NULL     AND release_reason = '')
  OR (state = 'converted' AND converted_at IS NOT NULL AND released_at IS NULL  AND release_reason = '')
  OR (state = 'released'  AND converted_at IS NULL  AND released_at IS NOT NULL AND release_reason <> ''));
CREATE INDEX reservation_active_product_idx    ON commerce_stockreservation (product_id) WHERE state = 'active';
CREATE INDEX reservation_converted_prod_idx ON commerce_stockreservation (product_id, converted_at) WHERE state = 'converted';
CREATE INDEX reservation_active_expires_idx    ON commerce_stockreservation (expires_at) WHERE state = 'active';
```

**Available stock (SQL, `commerce.selectors.available_stock(product_ids)`)** — Billz owns `ProductStock.quantity`
(refreshed by sync at `synced_at`). Units held by active reservations are unavailable; units of *converted*
reservations whose conversion happened after the last Billz sync are also unavailable (Billz has not yet reflected
the pushed sale):
```sql
SELECT ps.product_id,
       ps.quantity
       - COALESCE(SUM(r.quantity) FILTER (WHERE r.state = 'active'), 0)
       - COALESCE(SUM(r.quantity) FILTER (WHERE r.state = 'converted' AND r.converted_at > ps.synced_at), 0) AS available
FROM catalog_productstock ps
LEFT JOIN commerce_stockreservation r ON r.product_id = ps.product_id AND r.state IN ('active', 'converted')
WHERE ps.product_id = ANY(%(ids)s)
GROUP BY ps.product_id, ps.quantity, ps.synced_at;
```
Checkout locks the `ProductStock` rows of the cart (`select_for_update(of=("self",))`, ordered by `product_id`) before
running this query, so two concurrent checkouts for the last unit serialise and the second gets `OUT_OF_STOCK`
(spec test "two checkouts for the last unit → one reservation"). `synced_at` null → the converted term is ignored.

**Transition table**

| From | To | Trigger | Effects |
|---|---|---|---|
| — | `active` | checkout | one row per order line, `expires_at = order.reservation_expires_at`; event `reservation_created` |
| `active` | `converted` | payment confirmed | `converted_at = now`; `payment_confirmed.data.reservation_converted = true` |
| `active` | `released` (`expired`) | `commerce.expire_reservations` beat (1 min) | `released_at`; event `reservation_expired.released_item_count = COUNT(rows)` |
| `active` | `released` (`cancelled`) | admin/customer cancel from `awaiting_payment` | `cancelled.data.stock_released = true` |
| `active` | `released` (`payment_failed`) | reserved for a terminal provider failure that ends the order before expiry (M2 Payme `-31008`-style dead ends) | event `payment_failed`, order stays `awaiting_payment` until expiry — see open point |
| `converted` / `released` | — | terminal | a cancel after payment does **not** release stock (the sale was pushed to Billz; a return is a Billz operation, O2) → `cancelled.data.stock_released = false` |

**Invariants (services)** — `Σ quantity` of the order's reservations equals `Σ OrderItem.quantity` per product;
transitions run under the order lock; `release`/`convert` on a row already in the target state are no-ops
(idempotent for at-least-once callers), other mismatches raise `InvalidTransition`.

**Contract fields served** — `reservation_created`/`reservation_expired` event data; `payment_confirmed.data.
reservation_converted`; M2 cart/checkout availability and `OUT_OF_STOCK`. The admin `products/?out_of_stock` keeps
the contract's definition (`ProductStock.quantity ≤ 0`, Billz-owned) and ignores reservations.

**Open points**
- C-10 `release_reason = payment_failed` is modelled but no M1/M2 flow uses it (a failed provider attempt keeps the
  order payable until expiry). Keep for the M2 "customer abandons checkout" button, or drop.
- C-11 Whether the Billz import (`ProductStock` refresh) should subtract still-active reservations when computing
  the customer-visible stock (it does, through this selector) — and whether the *admin* product list should show
  "available" next to "stock" (contract change).

---

## 2. `appointments`

### 2.0 Module overview
```
appointments/
  models.py      Appointment (TS), AppointmentEvent (AO)
  services.py    request (M2 booking), create_by_admin, reschedule, confirm, reject, attend, cancel (M2),
                 record_event (the only AppointmentEvent writer), check_slot (hours − overrides − blocks − overlaps)
  selectors.py   day_appointments(date), pending_summary(now), upcoming_count(customer, now), my_appointments (M2),
                 free_slots (M2)
  tasks.py       none in M1 (push goes through notifications via the outbox topic push.appointment)
  seed.py        seeder (§7)
```

### 2.1 `Appointment` (TS)
A booked visit of one customer with one professional for one service in one time slot, with the price and
duration snapshotted at booking; pending requests hold the slot (P1), enforced by a `btree_gist` exclusion.

```python
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators


class TsTzRange(models.Func):  # as in the Django docs' ExclusionConstraint example
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Appointment(TimeStampedModel):
    class CancelledBy(models.TextChoices):  # internal, M2
        CUSTOMER = "customer"
        ADMIN = "admin"

    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.PROTECT, related_name="appointments"
    )
    service = models.ForeignKey(
        "professionals.Service", on_delete=models.PROTECT, related_name="appointments"
    )
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="appointments")
    start = models.DateTimeField()
    end = models.DateTimeField()
    duration_min = models.PositiveSmallIntegerField()  # snapshot of ProfessionalService.duration_min
    price = models.BigIntegerField()  # snapshot of ProfessionalService.price
    status = models.CharField(
        max_length=16, choices=enums.AppointmentStatus.choices, default=enums.AppointmentStatus.REQUESTED
    )  # requested|confirmed|rejected|attended|cancelled (T3)
    requested_via = models.CharField(max_length=8, choices=enums.RequestedVia.choices)  # app|admin
    requested_at = models.DateTimeField(default=timestamp_default)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="created_appointments"
    )  # set iff requested_via = admin
    customer_note = models.CharField(max_length=300, blank=True)  # M2 booking comment
    staff_note = models.CharField(max_length=300, blank=True)  # admin-only remark (reschedule reason, …)
    confirmed_at = models.DateTimeField(null=True)
    rejected_at = models.DateTimeField(null=True)
    attended_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    cancelled_by = models.CharField(max_length=8, blank=True, choices=CancelledBy.choices)
    reschedule_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["start", "id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end__gt=models.F("start")), name="appointment_end_after_start"
            ),
            models.CheckConstraint(
                condition=models.Q(duration_min__gte=5, duration_min__lte=480),
                name="appointment_duration_5_480",
            ),
            models.CheckConstraint(condition=models.Q(price__gte=0), name="appointment_price_non_negative"),
            models.CheckConstraint(
                condition=models.Q(requested_via="admin", created_by__isnull=False)
                | models.Q(requested_via="app", created_by__isnull=True),
                name="appointment_created_by_via",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="requested",
                        confirmed_at__isnull=True,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | models.Q(
                        status="confirmed",
                        confirmed_at__isnull=False,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | models.Q(
                        status="rejected",
                        rejected_at__isnull=False,
                        confirmed_at__isnull=True,
                        attended_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | models.Q(
                        status="attended",
                        confirmed_at__isnull=False,
                        attended_at__isnull=False,
                        rejected_at__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by="",
                    )
                    | models.Q(
                        status="cancelled",
                        cancelled_at__isnull=False,
                        rejected_at__isnull=True,
                        attended_at__isnull=True,
                    )
                    & ~models.Q(cancelled_by="")
                ),
                name="appointment_status_fields",
            ),
            ExclusionConstraint(
                name="appointment_no_overlap",
                expressions=[
                    (TsTzRange("start", "end", RangeBoundary()), RangeOperators.OVERLAPS),  # '[)'
                    ("professional", RangeOperators.EQUAL),
                ],
                condition=models.Q(status__in=["requested", "confirmed"]),
            ),
        ]
        indexes = [
            models.Index(fields=["professional", "start"], name="appointment_prof_start_idx"),
            models.Index(fields=["start"], name="appointment_start_idx"),
            models.Index(
                fields=["start"],
                condition=models.Q(status="requested"),
                name="appointment_req_start_idx",  # ≤ 30 chars (E034)
            ),
            models.Index(
                fields=["customer", "start"],
                condition=models.Q(status__in=["requested", "confirmed"]),
                name="appointment_customer_open_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Appointment #{self.pk} {self.status}: prof {self.professional_id} {self.start:%Y-%m-%d %H:%M} ({self.duration_min} min)"
```
Exact SQL (as built: `btree_gist` already exists from `core.0007_db_extensions`, so `appointments/0001_initial.py`
depends on it instead of running `BtreeGistExtension()`, and the raw duration check that the ORM cannot express is a
`RunSQL` inside the same `0001_initial`; the append-only trigger is `0002_appointmentevent_append_only`):
```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_end_after_start CHECK ("end" > start);
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_duration_5_480 CHECK (duration_min >= 5 AND duration_min <= 480);
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_price_non_negative CHECK (price >= 0);
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_created_by_via CHECK (
  (requested_via = 'admin' AND created_by_id IS NOT NULL) OR (requested_via = 'app' AND created_by_id IS NULL));
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_status_fields CHECK (
  (status = 'requested' AND confirmed_at IS NULL AND rejected_at IS NULL AND attended_at IS NULL AND cancelled_at IS NULL AND cancelled_by = '')
  OR (status = 'confirmed' AND confirmed_at IS NOT NULL AND rejected_at IS NULL AND attended_at IS NULL AND cancelled_at IS NULL AND cancelled_by = '')
  OR (status = 'rejected'  AND rejected_at IS NOT NULL AND confirmed_at IS NULL AND attended_at IS NULL AND cancelled_at IS NULL AND cancelled_by = '')
  OR (status = 'attended'  AND confirmed_at IS NOT NULL AND attended_at IS NOT NULL AND rejected_at IS NULL AND cancelled_at IS NULL AND cancelled_by = '')
  OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND rejected_at IS NULL AND attended_at IS NULL AND cancelled_by <> ''));
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_no_overlap
  EXCLUDE USING gist (tstzrange(start, "end", '[)') WITH &&, professional_id WITH =)
  WHERE (status IN ('requested', 'confirmed'));
-- RunSQL inside appointments/migrations/0001_initial.py (reverse: DROP CONSTRAINT)
ALTER TABLE appointments_appointment ADD CONSTRAINT appointment_duration_matches
  CHECK ("end" - start = make_interval(mins => duration_min));
CREATE INDEX appointment_prof_start_idx      ON appointments_appointment (professional_id, start);
CREATE INDEX appointment_start_idx           ON appointments_appointment (start);
CREATE INDEX appointment_req_start_idx ON appointments_appointment (start) WHERE status = 'requested';
CREATE INDEX appointment_customer_open_idx   ON appointments_appointment (customer_id, start) WHERE status IN ('requested','confirmed');
```
`end` is a reserved word: Django quotes identifiers, raw SQL must write `"end"`. `ExclusionConstraint` and
`DateTimeRangeField` both mix in `CheckPostgresInstalledMixin` (Django 6.1.2), whose system check fails with
`postgres.E005` unless **`django.contrib.postgres` is in `INSTALLED_APPS`** — it is (01 §6.1/§7, review blocker 1),
so the ORM constraint is emitted by `makemigrations` and no `RunSQL` fallback is needed (A-1 closed).

**State machine** (every transition under `select_for_update()` of the row; an `AppointmentEvent` per row; the
exclusion constraint is re-checked by PostgreSQL on every `UPDATE` that touches `start`/`end`/`status`/`professional`)

| From | To | Trigger | Guards | Effects (event · outbox · audit) |
|---|---|---|---|---|
| — | `requested` | M2 `POST /appointments` (`@idempotent`) | active customer; `check_slot`: inside the day's hours (override wins), not intersecting a `BlockedPeriod` or the recurring break, aligned to `slot_step_min`, start ≥ now; the exclusion constraint catches the overlap race (`IntegrityError` → 409 `conflict` "slot_taken") | event `requested {via: "app", start, end, service_id, price}` (source `customer`) |
| — | `requested` / `confirmed` | admin `POST appointments/` (T10, B6) | same slot checks; `requested_via = admin`, `created_by = staff`; admins may create directly as `confirmed` | event `requested {via: "admin", …}` (+ `confirmed` when created confirmed, source `admin`); audit `appointment.created` (new §3.2 action — open point A-2) |
| `requested` | `confirmed` | `POST appointments/{id}/confirm/` (I) | status `requested` else 409 `invalid_transition` | `confirmed_at`; event `confirmed {push_sent}`; outbox `push.appointment {appointment_id, action: "confirmed"}`; audit `appointment.confirmed` |
| `requested` | `rejected` | `…/reject/` (I) | status `requested` | `rejected_at`; event `rejected {push_sent}`; outbox `push.appointment … "rejected"`; audit `appointment.rejected`; the slot is freed (exclusion predicate no longer matches) |
| `confirmed` | `attended` | `…/attend/` (I) | status `confirmed` | `attended_at`; event `attended {}`; audit `appointment.attended`; no push |
| `requested` / `confirmed` | same | admin `POST appointments/{id}/reschedule/` (T10) | slot checks for the new range; status unchanged ("reschedule keeps status") | `start/end/duration_min` updated, `reschedule_count += 1`, `staff_note`; event `rescheduled {from_start, from_end, to_start, to_end, by: "admin"}`; outbox `push.appointment … "rescheduled"` (open point A-3); audit `appointment.rescheduled` |
| `requested` / `confirmed` | `cancelled` | M2 customer cancel / admin cancel (F3 dialog) | start ≥ now (customer); P3 terms not enforced | `cancelled_at`, `cancelled_by`; event `cancelled {by, reason, push_sent}`; outbox `push.appointment … "cancelled"` for admin cancels; audit `appointment.cancelled` (admin) |
| `rejected` / `attended` / `cancelled` | — | terminal | | |

**Invariants (services)**
- `end == start + duration_min` (DB check) and `duration_min`/`price` are copied from `ProfessionalService` at
  booking; later price edits never change a booked row.
- At most one open (`requested`|`confirmed`) appointment per professional per overlapping range (exclusion); past
  `attended`/`rejected`/`cancelled` rows never block.
- `requested_at` = the booking instant for app requests; for admin-created rows it is the creation instant too
  (the design shows "requested_at" for admin rows: a3 `2026-10-22T10:05`).
- `status` of appointments in the past is not auto-advanced (no "no-show" state in the contract; staff mark
  `attend` or leave `confirmed`).

**Contract fields served**
- `GET appointments/?date=`: `appointments[]` = rows with `start ∈ [d0, d1)` (`timeutils.day_bounds(date)`), all
  statuses, `ORDER BY start, id`: `id`, `specialist_id = professional_id`, `customer` (`CustomerRef`),
  `service_id`, `service_name = service.name_uz`, `service_short_name = service.short_name_uz`, `duration_min`,
  `start`, `end`, `price`, `status`, `requested_at`, `requested_via`. `specialists[]` from `01-…` §5.8.
- `GET appointments/pending-summary/` and dashboard `attention.pending_appointments/next_appointment_at`,
  `nav-counts.appointments_pending`:
  `SELECT COUNT(*), MIN(start) FROM appointments_appointment WHERE status = 'requested' AND start >= %(now)s`
  (partial index `appointment_req_start_idx`).
- Customer card `upcoming_appointments`:
  `SELECT COUNT(*) FROM appointments_appointment WHERE customer_id = %(id)s AND status IN ('requested','confirmed') AND start >= %(now)s`.
- Action endpoints return the day-item shape above.

**Open points**
- A-1 *Resolved*: `django.contrib.postgres` is installed (review blocker 1); `ExclusionConstraint` stays an ORM
  constraint in `Meta.constraints`.
- A-2 Audit actions `appointment.created`, `appointment.rescheduled`, `appointment.cancelled` are not in contract
  §3.2 (which predates T10); add them with the admin create/reschedule endpoints.
- A-3 Push on reschedule/cancel: the contract names pushes only for confirm/reject; the M2 app needs them. Default:
  enqueue `push.appointment` for `rescheduled` and admin `cancelled` too; texts in `notifications/texts.py`.
- A-4 Slot alignment: `slot_step_min` (01 §5.2) applies to M2 booking; admin creation may use any minute.

### 2.2 `AppointmentEvent` (AO)
Immutable trail of an appointment (who did what, when, from where), mirroring `OrderEvent`; rendered in M2
("history" of my appointment) and in the Django admin, not in the admin API v1.

```python
class AppointmentEvent(AppendOnlyModel):
    class Type(models.TextChoices):  # internal
        REQUESTED = "requested"
        CONFIRMED = "confirmed"
        REJECTED = "rejected"
        ATTENDED = "attended"
        CANCELLED = "cancelled"
        RESCHEDULED = "rescheduled"

    class Source(models.TextChoices):  # internal
        SYSTEM = "system"
        ADMIN = "admin"
        CUSTOMER = "customer"

    appointment = models.ForeignKey(
        "appointments.Appointment", on_delete=models.PROTECT, related_name="events"
    )
    type = models.CharField(max_length=16, choices=Type.choices)
    source = models.CharField(max_length=8, choices=Source.choices)
    actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="appointment_events"
    )
    data = models.JSONField(default=dict, encoder=DjangoJSONEncoder)
    at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [models.Index(fields=["appointment", "at", "id"], name="apptevent_appointment_at_idx")]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source="admin", actor__isnull=False)
                | (~models.Q(source="admin") & models.Q(actor__isnull=True)),
                name="apptevent_actor_by_source",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    type__in=["confirmed", "rejected", "attended", "rescheduled"], source="admin"
                )
                | models.Q(type__in=["requested", "cancelled"]),
                name="apptevent_source_by_type",
            ),
        ]

    def __str__(self) -> str:
        return f"Appointment {self.appointment_id}: {self.type} ({self.source}) at {self.at:%Y-%m-%d %H:%M}"
```
```sql
CREATE INDEX apptevent_appointment_at_idx ON appointments_appointmentevent (appointment_id, at, id);
ALTER TABLE appointments_appointmentevent ADD CONSTRAINT apptevent_actor_by_source CHECK (
  (source = 'admin' AND actor_id IS NOT NULL) OR (source <> 'admin' AND actor_id IS NULL));
ALTER TABLE appointments_appointmentevent ADD CONSTRAINT apptevent_source_by_type CHECK (
  (type IN ('confirmed','rejected','attended','rescheduled') AND source = 'admin') OR type IN ('requested','cancelled'));
-- + append-only trigger (0.2): appointments/migrations/0002_appointmentevent_append_only.py
```

`data` per type: `requested {via, start, end, service_id, professional_id, price, duration_min}` ·
`confirmed {push_sent}` · `rejected {push_sent}` · `attended {}` · `cancelled {by: "customer"|"admin", reason: str|null,
push_sent}` · `rescheduled {from_start, from_end, to_start, to_end, by: "admin", push_sent}`.

**Invariants** — written only by `appointments.services.record_event` under the appointment lock, one per
transition (plus one `requested` at creation); `push_sent` as in §1.5. **Contract fields served** — none in admin
v1; audit entries (`appointment.*`) carry `entity_type="appointment"`. **Open points** — A-5 whether to expose
`history` on the admin day item later (FE has no slot for it).

---

## 3. `notifications`

### 3.0 Module overview
```
notifications/
  models.py      StatusTemplate, CustomerSegment (TS), SegmentMember, Campaign (TS), CampaignRecipient,
                 InboxNotification (TS)
  services.py    create_campaign (materialise + queue or schedule), start_campaign, update_template,
                 segment CRUD + members, deliver_order_status (handler body), deliver_appointment (handler body)
  selectors.py   campaigns_list, meta (total_devices, segments with size, templates, link_options),
                 segment_members_q(segment) (SQL predicate), segment_sizes, customer_groups(customer),
                 pushable_devices(audience), inbox_for_customer (M2), unread_count (M2)
  texts.py       fixed uz/ru/en texts for appointment pushes (no admin templates in v1)
  tasks.py       outbox handlers push.order_status, push.appointment, campaign.send_batch;
                 beat notifications.start_due_campaigns (1 min)
  seed.py        seeder (§7)
```

### 3.1 `StatusTemplate`
The customer-facing push text for one of the four order statuses that trigger a push, in three languages, with
`{raqam}` as the only placeholder.

```python
class StatusTemplate(models.Model):
    status = models.CharField(max_length=24, choices=enums.TemplateStatus.choices, unique=True)
    text_uz = models.CharField(max_length=180)
    text_ru = models.CharField(max_length=180, blank=True)
    text_en = models.CharField(max_length=180, blank=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)
    updated_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(text_uz__regex=r"^.{5,180}$"), name="template_text_uz_5_180"
            ),
            models.CheckConstraint(
                condition=~models.Q(text_uz__regex=r"\{(?!raqam\})")
                & ~models.Q(text_ru__regex=r"\{(?!raqam\})")
                & ~models.Q(text_en__regex=r"\{(?!raqam\})"),
                name="template_only_raqam_placeholder",
            ),
        ]

    def __str__(self) -> str:
        return f"Template {self.status}: {self.text_uz[:40]}"
```
```sql
ALTER TABLE notifications_statustemplate ADD CONSTRAINT notifications_statustemplate_status_key UNIQUE (status);
ALTER TABLE notifications_statustemplate ADD CONSTRAINT template_text_uz_5_180 CHECK (text_uz ~ '^.{5,180}$');
ALTER TABLE notifications_statustemplate ADD CONSTRAINT template_only_raqam_placeholder CHECK (
  NOT (text_uz ~ '\{(?!raqam\})') AND NOT (text_ru ~ '\{(?!raqam\})') AND NOT (text_en ~ '\{(?!raqam\})'));
```
(PostgreSQL ARE supports the negative lookahead; the serializer gives the friendly `unknown_placeholder` error
before the DB ever sees it. `{raqam}` may appear more than once.)

The four rows are created by a data migration (`notifications/0002_seed_status_templates.py`, reverse: delete the
four rows) with the design texts, so `GET notification-templates/{status}/` never 404s for a valid status:

| `status` | `text_uz` | `text_ru` | `text_en` |
|---|---|---|---|
| `processing` | To‘lov qabul qilindi. Buyurtma №{raqam} tayyorlanmoqda. | Оплата принята. Заказ №{raqam} готовится. | Payment received. Order №{raqam} is being prepared. |
| `handed_to_courier` | Buyurtma №{raqam} kuryerga topshirildi. Holatni ilovada kuzating. | Заказ №{raqam} передан курьеру. Следите за статусом в приложении. | Order №{raqam} has been handed to the courier. Track it in the app. |
| `delivered` | Buyurtma №{raqam} yetkazildi. Xaridingiz uchun rahmat. | Заказ №{raqam} доставлен. Спасибо за покупку. | *(empty — the design shows `en: false` for this row)* |
| `cancelled` | Buyurtma №{raqam} bekor qilindi. To‘lov qaytariladi. | Заказ №{raqam} отменён. Оплата будет возвращена. | Order №{raqam} has been cancelled. The payment will be refunded. |

**Invariants (services)** — `update_template(status, texts, actor)` validates `uz` 5..180, `ru/en` ≤180, the
placeholder rule, writes `updated_at/updated_by`, audits `notification_template.updated` (diff). Rendering for a
customer: `text = texts[customer.language] or texts["uz"]`, `text.replace("{raqam}", str(order_id))`. The
`awaiting_payment` status has no template by design (no push on creation).

**Contract fields served** — `GET/PUT notification-templates/{status}/` `{status, texts {uz,ru,en}, updated_at,
updated_by (Actor|null)}`; `meta.templates[]` (all four, by `status` in `TemplateStatus` order);
`push.order_status` handler input.

**Open points** — N-1 A `cancelled` push after `reservation_expired` would read "To‘lov qaytariladi" although nothing
was paid; default: **no push on expiry** (§4.1 row 3). N-2 No templates for appointment pushes in v1 (fixed texts).

### 3.2 `CustomerSegment` (TS)
A named audience: rule-based (`min_online_orders`, `min_lifetime_amount`, ANDed when set) plus manual members.

```python
class CustomerSegment(TimeStampedModel):
    name = models.CharField(max_length=60)
    min_online_orders = models.PositiveIntegerField(null=True)  # null = rule off
    min_lifetime_amount = models.BigIntegerField(null=True)  # so'm; null = rule off
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="segment_name_ci_unique"),
            models.CheckConstraint(condition=~models.Q(name=""), name="segment_name_required"),
            models.CheckConstraint(
                condition=models.Q(min_online_orders__isnull=True) | models.Q(min_online_orders__gte=1),
                name="segment_min_orders_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(min_lifetime_amount__isnull=True) | models.Q(min_lifetime_amount__gte=1),
                name="segment_min_amount_positive",
            ),
        ]

    def __str__(self) -> str:
        return self.name
```
```sql
CREATE UNIQUE INDEX segment_name_ci_unique ON notifications_customersegment (LOWER(name));
ALTER TABLE notifications_customersegment ADD CONSTRAINT segment_name_required CHECK (name <> '');
ALTER TABLE notifications_customersegment ADD CONSTRAINT segment_min_orders_positive CHECK (min_online_orders IS NULL OR min_online_orders >= 1);
ALTER TABLE notifications_customersegment ADD CONSTRAINT segment_min_amount_positive CHECK (min_lifetime_amount IS NULL OR min_lifetime_amount >= 1);
```

**Membership (the single rule, implemented once in `selectors.segment_members_q(segment)` and reused by the
customers list filter, the card `groups`, the campaign audience and the meta sizes)** — contract §2.8:
`active customer ∧ (all set rules match ∨ manual member)`; with no rule set, manual only:
```sql
-- predicate over accounts_customer c for segment s
c.status = 'active' AND (
  EXISTS (SELECT 1 FROM notifications_segmentmember m WHERE m.segment_id = s.id AND m.customer_id = c.id)
  OR (
    (s.min_online_orders IS NOT NULL OR s.min_lifetime_amount IS NOT NULL)
    AND (s.min_online_orders IS NULL OR
         (SELECT COUNT(*) FROM loyalty_purchase p
           WHERE p.customer_id = c.id AND p.source = 'online' AND p.reversed = false) >= s.min_online_orders)
    AND (s.min_lifetime_amount IS NULL OR
         (SELECT COALESCE(SUM(p.amount), 0) FROM loyalty_purchase p
           WHERE p.customer_id = c.id AND p.reversed = false) >= s.min_lifetime_amount)))
```
ORM: `Customer.objects.filter(status="active").annotate(online_orders_count=…, lifetime_total=…).filter(Q(manual) |
(Q(rules_set) & Q(online_orders_count__gte=…) & Q(lifetime_total__gte=…)))` built from the segment row; the two
annotations are the same `Subquery`s the customers list uses (§6.3), over `purchase_live_cust_src_idx` (02 §1.6).
"Paid, non-reversed online orders" = non-reversed `online` purchases (a purchase row exists only after payment).

**Invariants (services)** — name unique case-insensitively (`unique` error); `DELETE segments/{id}/` → 409
`conflict` while `Campaign.objects.filter(audience_segment=s, state="scheduled").exists()`; members endpoints
only touch `SegmentMember`; `DELETE members/{customer_id}` → 409 `conflict` when the customer matches the rules
(rule-only member); audit `segment.created|updated|deleted|members_added|member_removed`.

**Contract fields served** — `Segment {id, name, rules {min_online_orders, min_lifetime_amount},
manual_member_count = COUNT(SegmentMember), member_count = COUNT(predicate), size = COUNT(pushable devices of
members), created_at, updated_at}`; `meta.segments[] {id, name, size}`; `customers/?segment=`; card `groups[]`
(§6.3); export `guruhlar`: `ID; Nomi; A’zolar (member_count); Push qurilmalar (size)`; `guruh-azolari` =
customer columns of `mijozlar` for the members list.

**Open points** — N-3 `member_count`/`size` on the paginated segments list are N+1-ish counts (one grouped query for
all segments is written in §6.5); acceptable for a handful of segments. N-4 Case-insensitive uniqueness uses
`LOWER()` (ASCII + Cyrillic basic folding), not the ICU collation.

### 3.3 `SegmentMember`
A manual membership of one customer in one segment.

```python
class SegmentMember(models.Model):
    segment = models.ForeignKey(
        "notifications.CustomerSegment", on_delete=models.CASCADE, related_name="manual_members"
    )
    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="segment_memberships"
    )
    added_by = models.ForeignKey("accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["segment_id", "added_at", "id"]
        constraints = [models.UniqueConstraint(fields=["segment", "customer"], name="segmentmember_unique")]
        indexes = [models.Index(fields=["customer"], name="segmentmember_customer_idx")]

    def __str__(self) -> str:
        return f"Customer {self.customer_id} in segment {self.segment_id}"
```
```sql
ALTER TABLE notifications_segmentmember ADD CONSTRAINT segmentmember_unique UNIQUE (segment_id, customer_id);
CREATE INDEX segmentmember_customer_idx ON notifications_segmentmember (customer_id);
```
**Invariants** — `POST segments/{id}/members/ {customer_ids ≤ 500}` is `bulk_create(ignore_conflicts=True)` (adding an
existing member is a no-op); deleted customers may be listed as manual members but never count (predicate requires
`active`). **Contract fields served** — `manual_member_count`, membership predicate, `segments/{id}/members/` list
(ordered like `customers/`). **Open points** — none.

### 3.4 `Campaign` (TS)
One admin-authored push notification (promo or general) to an audience, with its schedule, send progress and
result counters; the contract's `NotificationRecord`.

```python
class Campaign(TimeStampedModel):
    class State(models.TextChoices):  # internal; API renders sending as "queued"
        SCHEDULED = "scheduled"
        QUEUED = "queued"
        SENDING = "sending"
        SENT = "sent"

    kind = models.CharField(max_length=8, choices=enums.NotificationKind.choices)  # promo|general
    title = models.CharField(max_length=60)
    body = models.CharField(max_length=180)
    image = models.ImageField(
        upload_to=media.campaign_image_path, blank=True, max_length=255
    )  # MEDIA_ROOT/campaigns/<id>/<hash>.<ext>
    link_kind = models.CharField(
        max_length=8, choices=enums.LinkKind.choices, default=enums.LinkKind.NONE
    )  # none|product
    link_product = models.ForeignKey(
        "catalog.Product", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    link_product_label = models.CharField(
        max_length=120, blank=True
    )  # uz display name at creation (snapshot)
    audience_type = models.CharField(max_length=8, choices=enums.AudienceType.choices)  # all|segment|customer
    audience_segment = models.ForeignKey(
        "notifications.CustomerSegment", null=True, on_delete=models.SET_NULL, related_name="campaigns"
    )
    audience_segment_name = models.CharField(max_length=60, blank=True)  # snapshot
    audience_customer = models.ForeignKey(
        "accounts.Customer", null=True, on_delete=models.PROTECT, related_name="+"
    )
    audience_phone = models.CharField(max_length=13, blank=True)  # snapshot
    recipient_count = models.PositiveIntegerField(
        default=0
    )  # pushable devices at creation; recomputed at send start
    state = models.CharField(max_length=16, choices=State.choices, default=State.QUEUED)
    scheduled_at = models.DateTimeField(null=True)
    started_at = models.DateTimeField(null=True)
    sent_at = models.DateTimeField(null=True)
    sent = models.PositiveIntegerField(null=True)  # FCM accepted
    failed = models.PositiveIntegerField(null=True)  # FCM rejected / no token
    delivered = models.PositiveIntegerField(null=True)  # M2 acknowledgements; null in M1
    send_cursor = models.BigIntegerField(default=0)  # last CampaignRecipient.id handed to the adapter
    batches_dispatched = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    author = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="campaigns"
    )

    class Meta:
        ordering = [Coalesce("scheduled_at", "created_at").desc(), "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(title__regex=r"^.{3,60}$") & models.Q(body__regex=r"^.{5,180}$"),
                name="campaign_text_lengths",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(link_kind="none", link_product__isnull=True, link_product_label="")
                    | models.Q(link_kind="product") & ~models.Q(link_product_label="")
                ),
                name="campaign_link_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        audience_type="all",
                        audience_segment__isnull=True,
                        audience_segment_name="",
                        audience_customer__isnull=True,
                        audience_phone="",
                    )
                    | models.Q(audience_type="segment", audience_customer__isnull=True, audience_phone="")
                    & ~models.Q(audience_segment_name="")
                    | models.Q(
                        audience_type="customer",
                        audience_customer__isnull=False,
                        audience_segment__isnull=True,
                        audience_segment_name="",
                    )
                    & models.Q(audience_phone__regex=r"^\+998\d{9}$")
                ),
                name="campaign_audience_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="scheduled",
                        scheduled_at__isnull=False,
                        started_at__isnull=True,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | models.Q(
                        state="queued",
                        started_at__isnull=True,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | models.Q(
                        state="sending",
                        started_at__isnull=False,
                        sent_at__isnull=True,
                        sent__isnull=True,
                        failed__isnull=True,
                    )
                    | models.Q(
                        state="sent",
                        started_at__isnull=False,
                        sent_at__isnull=False,
                        sent__isnull=False,
                        failed__isnull=False,
                    )
                ),
                name="campaign_state_fields",
            ),
        ]
        indexes = [
            models.Index(
                Coalesce("scheduled_at", "created_at").desc(), models.F("id").desc(), name="campaign_at_idx"
            ),
            models.Index(
                fields=["scheduled_at"], condition=models.Q(state="scheduled"), name="campaign_due_idx"
            ),
            models.Index(fields=["state", "kind"], name="campaign_state_kind_idx"),
            models.Index(
                fields=["audience_segment"],
                condition=models.Q(state="scheduled"),
                name="campaign_sched_segment_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Campaign #{self.pk} [{self.kind}] {self.title} ({self.state})"
```
```sql
ALTER TABLE notifications_campaign ADD CONSTRAINT campaign_text_lengths CHECK (title ~ '^.{3,60}$' AND body ~ '^.{5,180}$');
ALTER TABLE notifications_campaign ADD CONSTRAINT campaign_link_fields CHECK (
  (link_kind = 'none' AND link_product_id IS NULL AND link_product_label = '')
  OR (link_kind = 'product' AND link_product_label <> ''));
ALTER TABLE notifications_campaign ADD CONSTRAINT campaign_audience_fields CHECK (
  (audience_type = 'all' AND audience_segment_id IS NULL AND audience_segment_name = '' AND audience_customer_id IS NULL AND audience_phone = '')
  OR (audience_type = 'segment' AND audience_segment_name <> '' AND audience_customer_id IS NULL AND audience_phone = '')
  OR (audience_type = 'customer' AND audience_customer_id IS NOT NULL AND audience_phone ~ '^\+998\d{9}$'
      AND audience_segment_id IS NULL AND audience_segment_name = ''));
ALTER TABLE notifications_campaign ADD CONSTRAINT campaign_state_fields CHECK (
  (state = 'scheduled' AND scheduled_at IS NOT NULL AND started_at IS NULL AND sent_at IS NULL AND sent IS NULL AND failed IS NULL)
  OR (state = 'queued'  AND started_at IS NULL AND sent_at IS NULL AND sent IS NULL AND failed IS NULL)
  OR (state = 'sending' AND started_at IS NOT NULL AND sent_at IS NULL AND sent IS NULL AND failed IS NULL)
  OR (state = 'sent'    AND started_at IS NOT NULL AND sent_at IS NOT NULL AND sent IS NOT NULL AND failed IS NOT NULL));
CREATE INDEX campaign_at_idx            ON notifications_campaign (COALESCE(scheduled_at, created_at) DESC, id DESC);
CREATE INDEX campaign_due_idx           ON notifications_campaign (scheduled_at) WHERE state = 'scheduled';
CREATE INDEX campaign_state_kind_idx    ON notifications_campaign (state, kind);
CREATE INDEX campaign_sched_segment_idx ON notifications_campaign (audience_segment_id) WHERE state = 'scheduled';
```

**State machine and batching**

| From | To | Trigger | Effects |
|---|---|---|---|
| — | `scheduled` | `POST notifications/` with `scheduled_at > now` | row (`recipient_count` = pushable devices of the audience now, for display); audit `notification.created` |
| — | `queued` | `POST notifications/` without `scheduled_at` | row; `materialise(campaign)` (below); outbox `campaign.send_batch {campaign_id, batch_no: 0}`; audit |
| `scheduled` | `queued` | beat `notifications.start_due_campaigns` (1 min): `state='scheduled' AND scheduled_at <= now`, `select_for_update(skip_locked=True)` | `materialise` (recomputes `recipient_count`); outbox batch 0 |
| `queued` | `sending` | first `campaign.send_batch` handler run | `started_at` |
| `sending` | `sending` | each batch | 500 `CampaignRecipient` rows with `id > send_cursor` ordered by `id`: inbox row per recipient (`dedupe_key = campaign:{cid}:{customer_id}`, `get_or_create`), FCM batch send through `integrations.fcm` (fake in M1), per-recipient `state`/`error`/`sent_at`; `send_cursor = max(id)`, `batches_dispatched += 1`; if more rows remain → enqueue `batch_no + 1` |
| `sending` | `sent` | last batch | `sent_at`, `sent = COUNT(state='sent')`, `failed = COUNT(state IN ('failed','skipped'))` |
| `sent` | — | terminal (`delivered` is updated by M2 acknowledgements) | |

`materialise(campaign)`: inserts `CampaignRecipient(customer, device)` for every pushable device of the audience
(`audience_type=all` → all; `segment` → `segment_members_q`; `customer` → that customer), `ignore_conflicts=True`
(unique `(campaign, customer)`), then `recipient_count = COUNT(rows)`. A campaign with zero recipients goes straight
to `sent` with `sent = failed = 0`.

**Invariants (services)**
- `title`/`body` trimmed 3..60 / 5..180 (serializer messages from the contract); `image` through `core.images`
  (≤5 MiB, JPEG/PNG/WebP, re-encoded); `link.product_id` must be an *active* product (`does_not_exist`), its uz
  display name snapshotted; `audience.phone` must belong to an *active* customer (`customer_not_found`).
- Snapshots (`audience_segment_name`, `audience_phone`, `link_product_label`) are what the history renders even if
  the segment is renamed/deleted or the product hidden.
- `audience_segment` is `SET_NULL` (refinement of the skeleton's `PROTECT`): the contract allows deleting a segment
  once no *scheduled* campaign targets it, so sent campaigns must survive the deletion with their snapshot.
- The outbox `dedupe_key` per batch makes a redelivered batch idempotent together with the inbox `dedupe_key` and
  the recipient `state` (a recipient already `sent` is skipped). `send_cursor` advances only in the handler's own
  transaction after the adapter returned.

**Contract fields served** — `NotificationRecord`: `id`, `kind`, `title`, `body`, `image_url`
(`absolute_media_url(image)` or `null`), `link {kind, product_id, label (""→null)}`, `audience {type, segment_id,
segment_name (""→null), phone (""→null)}`, `recipient_count`, `state` (`sending` → `queued`), `scheduled_at`,
`created_at`, `sent_at`, `sent`, `failed`, `delivered`, `author`. `GET notifications/` filters `state` (API value;
`queued` matches `queued` and `sending`), `kind`; ordering `COALESCE(scheduled_at, created_at) DESC, id DESC`
(the index above); export `bildirishnomalar`: `ID; Sana (fmt_dt COALESCE); Turi (kind label); Sarlavha; Auditoriya
(the §5.5 label: "Barcha · N" / "<segment> · N" / "Bitta mijoz · <phone>"); Holat (state label); Yuborildi (sent or
""); Yetkazildi (delivered or "")`. `segments/{id}` delete guard (`campaign_sched_segment_idx`).

**Open points**
- N-5 `audience_segment` `SET_NULL` instead of the skeleton's `PROTECT` (reason above).
- N-6 Batch size 500 and the FCM fake's failure ratio (`FAKE_FCM_FAIL_RATE`, default 0) are settings.
- N-7 `delivered` is stored for seeded history rows (design numbers) although M1 never fills it; the serializer
  renders whatever is stored.

### 3.5 `CampaignRecipient`
One target device of a campaign with its send outcome; the unit of idempotency for batched sends.

```python
class CampaignRecipient(models.Model):
    class State(models.TextChoices):  # internal
        PENDING = "pending"
        SENT = "sent"
        FAILED = "failed"
        SKIPPED = "skipped"  # device gone / push disabled between materialise and send

    campaign = models.ForeignKey(
        "notifications.Campaign", on_delete=models.CASCADE, related_name="recipients"
    )
    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="+")
    device = models.ForeignKey("accounts.Device", null=True, on_delete=models.SET_NULL, related_name="+")
    state = models.CharField(max_length=8, choices=State.choices, default=State.PENDING)
    error = models.CharField(max_length=254, blank=True)
    provider_message_id = models.CharField(max_length=128, blank=True)  # FCM message id
    provider_state = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder
    )  # FCM error code / batch id
    sent_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)  # M2 acknowledgement
    inbox = models.OneToOneField(
        "notifications.InboxNotification",
        null=True,
        on_delete=models.SET_NULL,
        related_name="campaign_recipient",
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["campaign_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["campaign", "customer"], name="recipient_campaign_customer_unique"
            ),
            models.UniqueConstraint(
                fields=["campaign", "device"],
                condition=models.Q(device__isnull=False),
                name="recipient_campaign_device_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(state="sent", sent_at__isnull=False)
                | (~models.Q(state="sent") & models.Q(sent_at__isnull=True)),
                name="recipient_sent_at",
            ),
        ]
        indexes = [
            models.Index(fields=["campaign", "state"], name="recipient_campaign_state_idx"),
            models.Index(fields=["customer", "-created_at"], name="recipient_customer_idx"),
        ]

    def __str__(self) -> str:
        return f"Campaign {self.campaign_id} → customer {self.customer_id} ({self.state})"
```
```sql
ALTER TABLE notifications_campaignrecipient ADD CONSTRAINT recipient_campaign_customer_unique UNIQUE (campaign_id, customer_id);
CREATE UNIQUE INDEX recipient_campaign_device_unique ON notifications_campaignrecipient (campaign_id, device_id) WHERE device_id IS NOT NULL;
ALTER TABLE notifications_campaignrecipient ADD CONSTRAINT recipient_sent_at CHECK (
  (state = 'sent' AND sent_at IS NOT NULL) OR (state <> 'sent' AND sent_at IS NULL));
CREATE INDEX recipient_campaign_state_idx ON notifications_campaignrecipient (campaign_id, state);
CREATE INDEX recipient_customer_idx       ON notifications_campaignrecipient (customer_id, created_at DESC);
```
**Invariants** — one row per customer per campaign (the M2 single-active-device rule makes `(campaign, device)` the
same cardinality; both uniques are kept as plan §4.2 states); the batch handler updates rows only from `pending`;
an FCM "unregistered token" answer marks the row `failed` and asks `accounts.services.invalidate_fcm_token(device)`
(01 §1.2). **Contract fields served** — `Campaign.sent/failed` aggregates; M2 `delivered` acknowledgements
(`delivered_at` → `Campaign.delivered = COUNT`). **Open points** — N-8 retention: recipients of campaigns older than
180 days can be deleted (plain table; counters stay on the campaign).

### 3.6 `InboxNotification` (TS)
The customer's in-app notification feed: one row per message actually addressed to the customer (campaign, order
status, appointment), de-duplicated by `dedupe_key` so at-least-once handlers never double-notify.

```python
class InboxNotification(TimeStampedModel):
    class Kind(models.TextChoices):  # internal
        PROMO = "promo"
        GENERAL = "general"
        ORDER_STATUS = "order_status"
        APPOINTMENT = "appointment"

    class SourceType(models.TextChoices):  # internal
        CAMPAIGN = "campaign"
        ORDER = "order"
        APPOINTMENT = "appointment"

    class PushState(models.TextChoices):  # internal
        NO_DEVICE = "no_device"
        QUEUED = "queued"
        SENDING = "sending"  # claimed before the FCM call (crash → never resent)
        SENT = "sent"
        FAILED = "failed"

    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="inbox")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=80)
    body = models.CharField(max_length=300)
    link = models.JSONField(
        default=dict, encoder=DjangoJSONEncoder
    )  # {"kind":"none"} | {"kind":"product","product_id":4} | {"kind":"order","order_id":10248} | {"kind":"appointment","appointment_id":1}
    image_url = models.CharField(max_length=500, blank=True)
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    source_id = models.BigIntegerField()  # campaign / order / appointment id
    template_status = models.CharField(
        max_length=24, blank=True, choices=enums.TemplateStatus.choices
    )  # order_status only
    language = models.CharField(
        max_length=2, choices=enums.ContentLanguage.choices
    )  # language the text was rendered in
    dedupe_key = models.CharField(max_length=120, unique=True)
    push_state = models.CharField(max_length=16, choices=PushState.choices, default=PushState.NO_DEVICE)
    pushed_at = models.DateTimeField(null=True)
    push_error = models.CharField(max_length=254, blank=True)
    read_at = models.DateTimeField(null=True)
    delivered_at = models.DateTimeField(null=True)  # M2 acknowledgement

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="order_status", source_type="order") & ~models.Q(template_status="")
                    | models.Q(kind="appointment", source_type="appointment", template_status="")
                    | models.Q(kind__in=["promo", "general"], source_type="campaign", template_status="")
                ),
                name="inbox_kind_source_fields",
            ),
            models.CheckConstraint(
                condition=~models.Q(title="") & ~models.Q(body=""), name="inbox_text_required"
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "-created_at", "-id"], name="inbox_customer_created_idx"),
            models.Index(
                fields=["customer"],
                condition=models.Q(read_at__isnull=True),
                name="inbox_customer_unread_idx",
            ),
            models.Index(fields=["source_type", "source_id"], name="inbox_source_idx"),
        ]

    def __str__(self) -> str:
        return f"Inbox {self.kind} → customer {self.customer_id}: {self.title[:40]}"
```
```sql
ALTER TABLE notifications_inboxnotification ADD CONSTRAINT notifications_inboxnotification_dedupe_key_key UNIQUE (dedupe_key);
ALTER TABLE notifications_inboxnotification ADD CONSTRAINT inbox_kind_source_fields CHECK (
  (kind = 'order_status' AND source_type = 'order' AND template_status <> '')
  OR (kind = 'appointment' AND source_type = 'appointment' AND template_status = '')
  OR (kind IN ('promo','general') AND source_type = 'campaign' AND template_status = ''));
ALTER TABLE notifications_inboxnotification ADD CONSTRAINT inbox_text_required CHECK (title <> '' AND body <> '');
CREATE INDEX inbox_customer_created_idx ON notifications_inboxnotification (customer_id, created_at DESC, id DESC);
CREATE INDEX inbox_customer_unread_idx  ON notifications_inboxnotification (customer_id) WHERE read_at IS NULL;
CREATE INDEX inbox_source_idx           ON notifications_inboxnotification (source_type, source_id);
```

**`dedupe_key` formats (the plan's "unique `(customer, source_type, source_id, template)`" made explicit)**

| Source | Format | Consequence |
|---|---|---|
| order status push | `order_status:{order_id}:{status}` | one push per order per status — a manual `status_changed` to `handed_to_courier` followed by the BTS `handed_to_courier` webhook notifies once |
| appointment push | `appointment:{appointment_id}:{action}` (`confirmed`, `rejected`, `rescheduled:{n}`, `cancelled`) | one per action; reschedules are numbered by `reschedule_count` |
| campaign | `campaign:{campaign_id}:{customer_id}` | one per customer per campaign |

**Handler protocol (push.order_status / push.appointment / campaign.send_batch)**: in one short transaction
`get_or_create(dedupe_key, defaults={…, push_state: queued|no_device})`; if the row existed and `push_state ∉
{queued}` → nothing to do; else set `push_state = sending` and commit; call the FCM adapter outside the transaction;
then `sent`/`failed` + `pushed_at`/`push_error`. A crash after the claim leaves `sending`, which is never retried
automatically (same posture as `Refund.unknown`); the nightly consistency query lists them (§6.7).

**Invariants** — `title`/`body` are the rendered texts (order status: title = §5.8 status label, body = template in
`language`; campaign: title/body as authored; appointment: `texts.py`); `link` follows the four shapes above;
`image_url` is copied from the campaign at send time. **Contract fields served** — none in admin v1 (M2
`/me/notifications`, unread count = `inbox_customer_unread_idx`); `push_sent` on events is *not* read from here (it
is evaluated at event time). **Open points** — N-9 retention of inbox rows (default: keep 365 days, M3 purge task);
N-10 whether `order_status` pushes should also be created when the customer has no device (`push_state=no_device`)
so the in-app feed is complete once a device registers — default **yes** (that is why the row exists before the
push).

---

## 4. Order state machine and `available_actions`

### 4.1 Transition table
Owner of every row: `commerce.services` (the upper module owns the transaction). Lock order Order → Payment →
LoyaltyAccount (02 §1.9.2); `rec(...)` = `record_event`; `enq(...)` = `outbox.enqueue` (dedupe keys in §5);
`aud(...)` = `audit.record`. "Provider calls" happen inside the transaction only when they are the synchronous BTS
cancel (kept right after the Order lock, before the Payment lock, so the transaction stays short).

| # | From | To | Trigger (endpoint / job / callback) | Guards (in order; error if violated) | Effects |
|---|---|---|---|---|---|
| 1 | — | `awaiting_payment` | M2 `POST /checkout` (`@idempotent`) → `services.checkout` | active customer; cart non-empty; delivery ⇒ address + BTS tariff quote; `available_stock ≥ qty` per line under `ProductStock` locks (else `OUT_OF_STOCK`); `cashback_used ≤ loyalty available` (`loyalty.create_hold(customer_id=, order_id=, amount=cashback_used, expires_at=reservation_expires_at)`, else `insufficient_balance`); `reservation_minutes = SiteSettings.reservation_minutes` | `Order` (+ items snapshots incl. `image_url`); `StockReservation` active ×lines; `CashbackHold` (if `cashback_used > 0`); `payments.create_payment(order_id=, provider=, amount=total)` (pending); `delivery.ensure_shipment(order_id)` (delivery only, `not_created`); `rec(order_created {channel, item_count, total}, status_after=awaiting_payment)`, `rec(reservation_created {item_count, minutes, expires_at})`; cart emptied; no outbox, no audit |
| 2 | `awaiting_payment` | `processing` | provider capture: Click `complete` (error 0) / Payme `PerformTransaction` (M2 webhook views in `commerce/api/webhooks/`) → `services.confirm_payment_effects(order, payment, txn)` | `payment.status = pending` and `amount == payable` (protocol errors `-2`/`-31001` otherwise); order `awaiting_payment` (otherwise refused at protocol level; a capture that still lands → late-capture auto-refund, 02 §2.1) | under Order → Payment → Account: `payments.confirm_payment` (`paid`, `confirmed_at`); reservations `active → converted`; `loyalty.capture_hold(order_id)` (debit entry); `loyalty.record_online_purchase(customer_id=, order_id=, items_subtotal=, promo_subtotal=, cashback_used=, at=now, items=<OrderItem snapshots>)` (purchase inserted with `cashback_amount 0`, then credit entry, then one `UPDATE` setting `cashback_amount` + `credited_entry` together — 02 §1.6); `status = processing`, `paid_at`; `rec(payment_confirmed {provider, transaction_id, amount, reservation_converted: true}, status_after=processing)`; `rec(cashback_credited {ledger_entry_id, amount, base, percent, policy_version})` iff credit > 0; `enq(billz.push_sale)`, `enq(bts.create_shipment)` via `delivery.request_bts_create(order_id, manual=False, actor_id=None)` (delivery only), `enq(push.order_status processing)`; no audit (system) |
| 3 | `awaiting_payment` | `cancelled` (`reservation_expired`) | beat `commerce.expire_reservations` (1 min): `status = awaiting_payment AND reservation_expires_at <= now` (`order_awaiting_expiry_idx`), one transaction per order, `select_for_update(skip_locked=True)` | re-check under lock (a capture may have won the race) | Order → Payment → Account: **`payments.fail_payment(error_code="expired")` for a `pending` payment whose `provider_state.state != 1`** (Click, or Payme before `CreateTransaction`); a Payme state-1 transaction stays `pending` and is failed by Payme's own inbound `CancelTransaction` (our `PerformTransaction` answer is `-31008`, 02 P-2); `reservation_expired.data.payment_failed` is `true` iff a payment was failed here; **no `payment_failed` event** in either case (C-8); reservations `→ released (expired)`; `loyalty.release_hold(order_id, reservation_expired)`; `status = cancelled`, `cancellation_reason = reservation_expired`, `cancelled_at`; `rec(reservation_expired {released_item_count, payment_failed}, status_after=cancelled)`; **no push** (N-1), no audit |
| 4 | `awaiting_payment` | `cancelled` (`admin`) | `POST orders/{id}/cancel/ {reason}` (I) → `services.cancel_order(order, note, actor)` | body `reason` 3..500 (400); status allowed (else 409 `not_cancellable`) | Order → Payment → Account: `payments.fail_payment("cancelled")` for a `pending` payment, **same Payme state-1 rule and no `payment_failed` event as row 3** (contract §2.2's "a `pending` payment becomes `failed`" is read as "every pending payment that can be failed unilaterally"; decided under 02 P-2, contract §7 delta note); reservations `→ released (cancelled)`; `loyalty.release_hold(order_id, order_cancelled)`; `status = cancelled`, `cancellation_reason = admin`, `cancellation_note = note`, `cancelled_by`, `cancelled_at`; `rec(cancelled {reason: admin, note, refund_id: null, cashback_reversed: false, stock_released: true, bts_cancelled: false, push_sent}, source=admin, actor)`; `enq(push.order_status cancelled)`; `aud(order.cancelled {reason: "admin", note, refund_id: null})` |
| 5 | `awaiting_payment` | `cancelled` (`customer_request`) | M2 `POST /orders/{id}/cancel` (customer) — optional (C-6) | status `awaiting_payment` | as row 4 with `source = system`, `actor = null`, `cancellation_reason = customer_request`, `cancelled_by = null`; no audit |
| 6 | `processing` | `cancelled` (`admin`) | `POST orders/{id}/cancel/` | `reason` valid; **if `Shipment.state = created`** → synchronous `delivery.services.cancel(shipment)`: BTS status must be `100` else 409 `not_cancellable` "BTS buyurtmani bekor qilmadi: posilka kuryerda."; transport failure → 503 `provider_unavailable` (nothing written, idempotency record not stored, safe to retry with the same key) | Order lock → BTS cancel (`Shipment → cancelled`, `ShipmentEvent`) → Payment lock: `payments.create_refund(payment, requested_by=actor)` → Click `requested` + `enq(refund.submit)`, Payme `manual_required`; `aud(refund.requested)` → Account lock: `loyalty.reverse_purchase(order_id)` (purchase `reversed`, one reversal per unreversed debit/credit); stock **not** released (`stock_released: false`); `status = cancelled` + cancellation columns; `rec(cancelled {reason: admin, note, refund_id, cashback_reversed: n > 0, stock_released: false, bts_cancelled, push_sent}, source=admin)`; `rec(refund_requested {refund_id, amount, provider, manual: provider == payme}, source=admin)`; `enq(push.order_status cancelled)`; `aud(order.cancelled {reason, note, refund_id, bts_cancelled})`. If the shipment is `not_created`/`failed` nothing is cancelled at BTS (`bts_cancelled: false`); a `pending` create message is harmless: its handler runs under the Order lock, sees `status = cancelled` and marks itself done without creating |
| 7 | `handed_to_courier` | `cancelled` (`admin`) | `POST orders/{id}/cancel/` | as row 6; the shipment is always `created` here, so the BTS cancel is mandatory and succeeds only while BTS still reports `100` (O1) | as row 6 |
| 8 | `processing` | `handed_to_courier` | `POST orders/{id}/transition/ {status}` (I) → `services.transition` | `fulfilment = delivery`; target ∈ `available_actions.transitions` else 409 `invalid_transition`; non-enum value → 400 `invalid_choice` | `status`, `handed_to_courier_at`; `rec(status_changed {from, to, push_sent}, source=admin, actor, status_after=to)`; `enq(push.order_status handed_to_courier)`; `aud(order.status_changed {from, to})` |
| 9 | `processing` | `delivered` | `POST …/transition/` | `fulfilment = pickup` | `status`, `delivered_at`; `rec(status_changed …)`; `enq(push.order_status delivered)`; `aud(order.status_changed)` |
| 10 | `handed_to_courier` | `delivered` | `POST …/transition/` | always offered | as row 9 |
| 11 | `processing` | `handed_to_courier` | BTS webhook `/api/v1/webhooks/bts/` or `commerce.poll_bts` → `services.apply_bts_status(order, code, via, payload)` | shipment `created`; `delivery.bts.map_status(code) = handed_to_courier`; order `processing` (if already `handed_to_courier` → only `delivery.apply_status` + `ShipmentEvent`, no order event; if `delivered`/`cancelled` → record and ignore) | `delivery.apply_status` (`bts_status`, `via`, `last_status_at`, `ShipmentEvent`); `status`, `handed_to_courier_at`; `rec(handed_to_courier {via, bts_status, push_sent}, source=bts)`; `enq(push.order_status handed_to_courier)` (deduped if the manual transition already pushed); `WebhookInbox` row (T5) for webhooks |
| 12 | `processing` / `handed_to_courier` | `delivered` | same | mapping = `delivered` | as row 11 with `delivered_at`, `rec(delivered …)` |
| 13 | `processing` | `processing` | `POST orders/{id}/retry-shipment/` (I) → `services.retry_shipment` | `fulfilment = delivery ∧ status = processing ∧ shipment.state = failed` else 409 `invalid_transition` "BTS ga qayta yuborib bo‘lmadi."; `pending` else 409 `conflict` "BTS so‘rovi allaqachon yuborilmoqda." (both under the Order lock) | `delivery.request_bts_create(order_id, manual=True, actor_id=actor.id)` (`create_requests += 1`, `enq(bts.create_shipment {…, manual: true, actor_id})`, `create_message`); `aud(order.shipment_retry_requested)`; **202** with `shipment.pending = true` |
| 14 | `processing` | `processing` | handler `bts.create_shipment` (§5) | skip if `order.status != processing` or shipment already `created` (mark done, no event) | lookup by `clientId` first; `delivery.mark_created` → `rec(shipment_created {tracking_number, tariff, manual}, source = admin if manual else system, actor from payload)`; or `delivery.mark_failed` → `rec(shipment_failed {attempt, manual, error_code})` (also from the outbox dead-letter hook: when a `bts.create_shipment` message goes `dead`, `commerce.tasks.on_outbox_dead` marks the shipment failed with `error_code = "dead_letter"`) |
| 15 | any paid | same | handler `refund.submit` (§5) / Payme `CancelTransaction` on a performed transaction | 02 §2.2 | `rec(refund_succeeded {refund_id, amount, provider})` or `rec(refund_failed {…, state, error_code})`; `payments.mark_*`; `Payment → refunded` when covered; audit `refund.state_changed` (payments) |
| 16 | `awaiting_payment` | `awaiting_payment` | provider failure callback: Click `complete` with `error < 0`, Payme `CancelTransaction` on state 1 → `services.fail_payment_effects` | payment `pending` | `payments.fail_payment(error_code)`; `rec(payment_failed {provider, transaction_id, error_code})`; the order stays payable with a new `Payment` row until expiry (P-2 in `02-…`) |
| 17 | `delivered` / `cancelled` | — | terminal | `cancel` → 409 `not_cancellable`; `transition` → 409 `invalid_transition`; `retry-shipment` → 409 `invalid_transition` | — |

Manual transitions never go backwards and there is no `delivered → anything`. `payment.status` stays `paid` after a
cancel (O5). Every row writes its status-changing event in the same transaction as the status update; outbox rows
are written in the same transaction too (at-least-once delivery after commit).

### 4.2 `available_actions` derivation
Computed in `commerce.services.available_actions(order, shipment)` from the locked/annotated order row and the
shipment annotated with `pending` (02 §3.1); the detail serializer calls it once:

```
transitions =
    ["handed_to_courier"]  if status == processing and fulfilment == delivery
    ["delivered"]          if status == processing and fulfilment == pickup
    ["delivered"]          if status == handed_to_courier
    []                     otherwise
cancel = status in {awaiting_payment, processing, handed_to_courier}        # the BTS rule is checked at POST time, not here
retry_shipment = fulfilment == delivery and status == processing
                 and shipment is not None and shipment.state == failed and not shipment.pending
```
SQL form of `retry_shipment` for a list annotation (not needed by the contract, useful for the F3 orders table):
```sql
SELECT o.id,
       (o.fulfilment = 'delivery' AND o.status = 'processing' AND s.state = 'failed'
        AND COALESCE(m.status IN ('pending','failed','in_flight'), false) = false) AS retry_shipment
FROM commerce_order o
LEFT JOIN delivery_shipment s ON s.order_id = o.id
LEFT JOIN core_outboxmessage m ON m.id = s.create_message_id;
```
`POST transition/` re-derives the list under the Order lock and refuses with 409 `invalid_transition` when the
target is not in it; `POST cancel/` re-checks `cancel` (409 `not_cancellable`) and then the BTS rule.

---

## 5. Outbox topic registry
All handlers are registered with `@outbox.register(topic)` in the orchestrating module's `tasks.py` (imported at
startup through `AppConfig.ready()`), run at-least-once and are idempotent at the provider boundary. Payloads are
JSON objects with the keys below; `dedupe_key` is the `OutboxMessage` unique key (`enqueue` is `get_or_create`).

| Topic | Producer (module.service) | Handler module | Payload keys | `dedupe_key` | Provider-boundary idempotency guard |
|---|---|---|---|---|---|
| `bts.create_shipment` | `commerce.confirm_payment_effects` (automatic) / `commerce.retry_shipment` (manual), both through `delivery.services.request_bts_create` | `commerce/tasks.py` → `delivery.services.mark_created/mark_failed` + `integrations.bts` | `order_id`, `manual` (bool), `request_no` (int, = `Shipment.create_requests`), `actor_id` (int\|null) | `bts.create_shipment:{order_id}:{request_no}` | under the Order lock: skip if `order.status != processing` or `shipment.state == created`; **BTS lookup by `clientId = str(order_id)`** before create — if BTS already has the parcel, adopt it (tracking number) instead of creating a second one; `Shipment` is 1:1 per order |
| `bts.cancel_shipment` | — (synchronous call inside `cancel_order`; **not an outbox topic**) | — | — | — | the BTS cancel answer decides the whole cancel transaction (409/503 before any write) |
| `billz.push_sale` | `commerce.confirm_payment_effects` | `commerce/tasks.py` → `loyalty.services.link_billz_sale` + `integrations.billz.create_sale` | `purchase_id`, `order_id`, `billz_order_uuid` | `billz.push_sale:{billz_order_uuid}` | `Purchase.billz_order_uuid` is persisted **before** the message is committed (02 §1.6); the handler skips when `billz_pushed_at` is set; Billz de-duplicates on the order UUID; on success `rec(billz_sale_recorded {billz_order_id})` only if no such event exists for the order |
| `refund.submit` | `payments.services.create_refund` (called by `commerce.cancel_order`; Click only) | `commerce/tasks.py` → `payments.services.mark_submitting/mark_succeeded/mark_failed/mark_unknown` + `integrations.click` | `refund_id`, `payment_id`, `order_id`, `provider_txn_id`, `amount` | `refund.submit:{refund_id}` | `mark_submitting` committed before the provider call; **status check by `provider_txn_id` before any reversal**; a redelivery that finds `submitting` re-checks the status first; `unknown` is never retried automatically (02 §2.2); events `refund_succeeded`/`refund_failed` written once per `refund_id` + type |
| `push.order_status` | `commerce.services` (rows 2, 4–12 of §4.1) | `notifications/tasks.py` → `notifications.services.deliver_order_status` + `integrations.fcm` | `order_id`, `status` (TemplateStatus), `event_id`, `customer_id` | `push.order_status:{order_id}:{status}` | `InboxNotification.dedupe_key = order_status:{order_id}:{status}` unique + the claim protocol (§3.6): one push per order per status whatever the number of deliveries or producers |
| `push.appointment` | `appointments.services.confirm/reject` (+ `reschedule`/admin `cancel`, A-3) | `notifications/tasks.py` → `deliver_appointment` + `integrations.fcm` | `appointment_id`, `action` (`confirmed`\|`rejected`\|`rescheduled`\|`cancelled`), `event_id`, `customer_id`, `reschedule_no` (int, for `rescheduled`) | `push.appointment:{appointment_id}:{action}` (`…:rescheduled:{reschedule_no}`) | `InboxNotification.dedupe_key = appointment:{id}:{action}` + claim protocol |
| `campaign.send_batch` | `notifications.services.create_campaign` (immediate) / beat `notifications.start_due_campaigns` (scheduled) / the handler itself (next batch) | `notifications/tasks.py` → `notifications.services.send_batch` + `integrations.fcm` | `campaign_id`, `batch_no` (int) | `campaign.send_batch:{campaign_id}:{batch_no}` | `CampaignRecipient` rows unique `(campaign, customer)`/`(campaign, device)`, processed from `pending` only in `id` order after `send_cursor`; inbox rows `campaign:{campaign_id}:{customer_id}`; the campaign row is locked (`select_for_update`) for the cursor update, and batch *n+1* is enqueued only after batch *n* committed |
| `sms.send` (M2) | `accounts.services.send_code` | `accounts/tasks.py` → `integrations.eskiz` | `sms_code_id`, `phone`, `text`, `purpose` | `sms.send:{sms_code_id}` | `SmsCode.sent_at` set in the handler's own transaction before the Eskiz call (claim); a redelivery with `sent_at` set and no `provider_message_id` is not resent (the code expires in 120 s anyway; the user can request a resend after 60 s) |

Dead letters: after 10 failed attempts the dispatcher sets `dead` and audits `outbox.dead`. `commerce.tasks` adds an
`on_outbox_dead(message)` hook (called by the dispatcher through a small registry in `core.outbox`, open point
X-1) that, under the Order lock, for `bts.create_shipment` runs `delivery.mark_failed(error_code="dead_letter")` +
`rec(shipment_failed {attempt, manual, error_code: "dead_letter"})` so the order surfaces in `attention.bts_failed`
and becomes retryable; for `refund.submit` it calls `payments.services.mark_failed(refund, error_code="dead_letter",
last_error=message.last_error)` and writes `rec(refund_failed {refund_id, amount, provider, state: "failed",
error_code: "dead_letter"})` — **`failed`, never `unknown`** (review major 5): the handler raises only on transport
errors *before* submission (02 §2.2), so a dead message always finds the refund still `requested` with
`submitted_at IS NULL`, where `unknown` would violate `refund_submitted_at` and misstate that the money reached the
provider; `failed` is counted by `refunds_needing_action`, and a retry is a new `Refund(origin=manual)`. Other topics
only audit.

Beat schedule entries owned by this group: `commerce.expire_reservations` (1 min), `commerce.poll_bts` (10 min; M2/M3
— re-tracks `created` shipments of open orders older than 30 min without a status update),
`notifications.start_due_campaigns` (1 min), plus `core.dispatch_outbox` (10 s) from M0.

---

## 6. Selectors the `customers` and `reporting` modules need (SQL-level)
`customers/selectors.py` and `reporting/selectors.py` compose the module selectors below; they never write.

### 6.1 Orders list, search and period (`commerce.selectors.orders_list(params)`)
```sql
-- base: one row per order with the display payment and the shipment annotation
SELECT o.*, c.name AS customer_name, c.phone AS customer_phone,
       dp.status AS payment_status, dp.provider AS payment_provider_display,
       s.state AS shipment_state, s.tracking_number, s.tariff, COALESCE(s.last_status_at, s.bts_created_at) AS shipment_updated_at,
       s.via, s.attempts, s.last_attempt_at,
       COALESCE(m.status IN ('pending','failed','in_flight'), false) AS shipment_pending
FROM commerce_order o
JOIN accounts_customer c ON c.id = o.customer_id
LEFT JOIN LATERAL (SELECT p.status, p.provider FROM payments_payment p WHERE p.order_id = o.id
                   ORDER BY CASE p.status WHEN 'refunded' THEN 2 WHEN 'paid' THEN 2 WHEN 'pending' THEN 1 ELSE 0 END DESC, p.id DESC
                   LIMIT 1) dp ON true
LEFT JOIN delivery_shipment s ON s.order_id = o.id
LEFT JOIN core_outboxmessage m ON m.id = s.create_message_id
WHERE 1 = 1
  AND (%(status)s IS NULL OR o.status = %(status)s)
  AND (%(fulfilment)s IS NULL OR o.fulfilment = %(fulfilment)s)
  AND (%(payment)s IS NULL OR COALESCE(dp.status, 'pending') = %(payment)s)
  AND (%(customer)s IS NULL OR o.customer_id = %(customer)s)
  AND o.created_at >= %(from)s AND o.created_at < %(to)s                 -- period ∩ date_from/date_to (below)
  AND (%(term)s IS NULL OR (
        CAST(o.id AS text) LIKE '%%' || %(term_no_num_sign)s || '%%'          -- "№10248" → "10248"
     OR (%(digits_ge3)s AND c.phone LIKE '%%' || %(digits)s || '%%')
     OR LOWER(c.name) LIKE '%%' || LOWER(%(term)s) || '%%'))
ORDER BY o.created_at DESC, o.id DESC;                                        -- or ASC, ASC
```
Period semantics (`timeutils`, Tashkent): `today` → `day_bounds(local_date(now))`; `week` → `(now − 7×24h, now]`
(rolling; implemented as `created_at > now − 7d AND created_at <= now`); `month` → `[day_bounds(month_start(local
_date(now)))[0], now]`; `all` → unbounded; `date_from`/`date_to` → `[day_bounds(date_from)[0], day_bounds(date_to)[1])`
ANDed with the period; `date_from > date_to` → 400. ORM: `Q` objects on `created_at` with aware bounds;
`search` as `Q(id_text__contains=…) | Q(customer__phone__contains=digits) | Q(customer__name__icontains=term)` with
`id_text = Cast("id", TextField())`. The payment filter uses the ranked subquery as `Exists` (02 §2.1). Export rows
reuse the queryset without pagination (≤ 50 000 rows, else `export_too_large`).

### 6.2 Status counts (`commerce.selectors.status_counts(params)`)
Same predicates as 6.1 minus `status`/`ordering`/pagination:
```sql
SELECT o.status, COUNT(*) FROM commerce_order o JOIN accounts_customer c ON c.id = o.customer_id
WHERE <6.1 predicates without status> GROUP BY o.status;
-- Python: {"all": Σ, "awaiting_payment": …, "processing": …, "handed_to_courier": …, "delivered": …, "cancelled": …} with 0 defaults
```
`nav-counts.orders_processing` = `SELECT COUNT(*) FROM commerce_order WHERE status = 'processing'` (all time).

### 6.3 Customers list and card (`customers.selectors`)
```sql
-- customers_list: annotations used by filters, orderings and the export
SELECT c.id, c.name, c.phone, c.status, c.registered_at,
       COALESCE(t.lifetime, 0)            AS lifetime_total,
       COALESCE(t.online_orders_count, 0) AS online_orders_count,
       COALESCE(a.balance, 0)             AS balance
FROM accounts_customer c
LEFT JOIN loyalty_loyaltyaccount a ON a.customer_id = c.id
LEFT JOIN LATERAL (
    SELECT COALESCE(SUM(p.amount), 0) AS lifetime,
           COUNT(*) FILTER (WHERE p.source = 'online') AS online_orders_count
    FROM loyalty_purchase p WHERE p.customer_id = c.id AND p.reversed = false) t ON true
WHERE (%(status)s IS NULL OR c.status = %(status)s)
  AND (%(term)s IS NULL OR LOWER(c.name) LIKE '%%' || LOWER(%(term)s) || '%%'
       OR (%(digits_ge3)s AND c.phone LIKE '%%' || %(digits)s || '%%'))
  AND (%(segment)s IS NULL OR <segment_members_q predicate, §3.2, for segment %(segment)s>)
ORDER BY <ordering>;
-- orderings (ties by id in the same direction):
--   -registered_at (default): registered_at DESC, id DESC   · registered_at ASC, id ASC
--   name / -name:             name ASC, id ASC              · name DESC, id DESC        (default collation; 01 open point 10)
--   lifetime_total / -:       lifetime_total ASC, id ASC    · lifetime_total DESC, id DESC
--   balance / -:              balance ASC, id ASC           · balance DESC, id DESC
```
ORM: `Customer.objects.annotate(lifetime_total=Coalesce(Subquery(Purchase.objects.filter(customer=OuterRef("pk"),
reversed=False).values("customer").annotate(s=Sum("amount")).values("s")), Value(0)), online_orders_count=…,
balance=Coalesce(F("loyalty_account__balance"), Value(0)))`. `percent` (`CustomerListItem.percent`) is computed in
Python per row with `loyalty.selectors.tier_for(lifetime_total, active_policy)` (tiers fetched once per request;
`null` when no policy/tier). Export `mijozlar`: `ID; Ism; Telefon; Ro‘yxatdan o‘tgan (fmt_dt); Jami xaridlar (so‘m)
(lifetime_total); Cashback foizi (percent string or ""); Balans (so‘m); Holat (label)`.

Customer card (`customers.selectors.customer_card(id)`), each item one query (≈ 10 queries, no N+1):
`totals`/`online_orders.count` = `loyalty.customer_totals` (02 §4.1); `online_orders.last_order_id/last_order_at`:
```sql
SELECT id, created_at FROM commerce_order WHERE customer_id = %(id)s ORDER BY created_at DESC, id DESC LIMIT 1;  -- any status
```
Intended semantics (review minor 19, to be added to contract §2.6 as a clarification): `count` = paid, non-reversed
online purchases (what the loyalty programme counts); `last_order_*` = the customer's **newest order of any status**
— the card's "last activity" — so for a customer whose newest order is `awaiting_payment` or `cancelled` the
`last_order_id` may name an order that `count` excludes. Malika's vector is unaffected (№10248 is paid).
`tier` = `loyalty.tier_for(lifetime)`; `balance/held/available` = `loyalty.account_summary`; `groups` =
`customer_groups(customer)`: evaluate every segment's rule in Python against the customer's `online_orders_count`
and `lifetime` (one `customer_totals` query already done) plus one query `SELECT segment_id FROM
notifications_segmentmember WHERE customer_id = %(id)s`; order by segment `id`; only `active` customers have groups
(a deleted customer gets `[]`). `upcoming_appointments` (§2.1); `device` = `accounts.selectors.primary_device`
(01 §1.2); `ledger`/`purchases` (02 §4.4).

### 6.4 Dashboard (`reporting.selectors.dashboard(range, from, to, now)`)
Window and buckets (contract §2.10): `today` → `[d0, d1)` of the Tashkent day, 24 hour buckets, previous = yesterday;
`week` → 7 Tashkent days ending today (`[day_bounds(today − 6d)[0], d1)`), previous = the 7 days before; `month` →
30 days ending today, previous = the 30 before; `custom` → `[from, to]` inclusive days (`from ≤ to ≤ today`, span ≤
366), `bucket = hour` iff span ≤ 2 days, previous = same length just before `from`.
```sql
-- orders per bucket (unit = 'hour' | 'day'); zero-filled in Python over the bucket list
SELECT date_trunc(%(unit)s, o.created_at AT TIME ZONE 'Asia/Tashkent') AS bucket_local, COUNT(*) AS orders
FROM commerce_order o
WHERE o.created_at >= %(from)s AND o.created_at < %(to)s
GROUP BY 1;

-- revenue per bucket: payments by confirmation time, net of succeeded refunds attributed to the payment's bucket
SELECT date_trunc(%(unit)s, p.confirmed_at AT TIME ZONE 'Asia/Tashkent') AS bucket_local,
       SUM(p.amount) - COALESCE(SUM(r.refunded), 0) AS revenue
FROM payments_payment p
LEFT JOIN (SELECT payment_id, SUM(amount) AS refunded FROM payments_refund WHERE state = 'succeeded' GROUP BY payment_id) r
       ON r.payment_id = p.id
WHERE p.status IN ('paid', 'refunded') AND p.confirmed_at >= %(from)s AND p.confirmed_at < %(to)s
GROUP BY 1;

-- previous_period_orders
SELECT COUNT(*) FROM commerce_order WHERE created_at >= %(prev_from)s AND created_at < %(prev_to)s;

-- stats
SELECT COUNT(*) FROM accounts_customer WHERE registered_at >= %(from)s AND registered_at < %(to)s;        -- new_customers (incl. deleted)
SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'deleted') FROM accounts_customer;                      -- total_customers, deleted_customers
SELECT COUNT(*) FROM catalog_productstock WHERE quantity <= 0;                                           -- out_of_stock
-- attention
SELECT COUNT(*) FROM delivery_shipment s JOIN commerce_order o ON o.id = s.order_id
 WHERE s.state = 'failed' AND o.status = 'processing';                                                   -- bts_failed
SELECT COUNT(*), MIN(start) FROM appointments_appointment WHERE status = 'requested' AND start >= %(now)s; -- pending_appointments, next_appointment_at
SELECT COUNT(*) FROM catalog_product WHERE is_new_from_billz = true;                                     -- new_from_billz
SELECT COUNT(*) FROM payments_refund WHERE state IN ('failed', 'unknown', 'manual_required');            -- refunds_needing_action
```
`buckets[].start` is the bucket's Tashkent start rendered `+05:00`; `stats.orders`/`stats.revenue` = Σ buckets;
`revenue` is never negative because refunds are attributed to the payment's own bucket and `Σ refunds ≤ amount`.
ORM: `TruncHour/TruncDay(…, tzinfo=TASHKENT)` annotations produce exactly the two grouped queries above.

### 6.5 Notifications meta and segment sizes (`notifications.selectors`)
```sql
-- total_devices
SELECT COUNT(*) FROM accounts_device d JOIN accounts_customer c ON c.id = d.customer_id
WHERE d.is_active AND d.push_enabled AND c.status = 'active';
-- sizes of all segments in one pass (member_count and size)
SELECT s.id,
       COUNT(DISTINCT c.id) AS member_count,
       COUNT(DISTINCT d.id) FILTER (WHERE d.is_active AND d.push_enabled) AS size
FROM notifications_customersegment s
JOIN accounts_customer c ON <segment_members_q predicate for s>
LEFT JOIN accounts_device d ON d.customer_id = c.id
GROUP BY s.id;
-- link_options: active products, uz display name, sorted by label
SELECT p.id, COALESCE(NULLIF(p.name_uz, ''), p.billz_name) AS label FROM catalog_product p
WHERE p.status = 'active' ORDER BY label COLLATE "glowderma_ci", p.id;
```
(`templates` = the four `StatusTemplate` rows in `TemplateStatus` order.) `recipient_count` at creation and
`materialise` use the `total_devices` query narrowed by audience.

### 6.6 Payments list — owned by `payments.selectors.payments_list` (02 §2.1); `reporting` only wires the route.

### 6.7 Nightly consistency checks added by this group (`reporting.tasks.consistency_report`, 03:40 Tashkent, audit `reporting.consistency_mismatch` per finding)
```sql
-- K1 order amounts vs items
SELECT o.id FROM commerce_order o JOIN (SELECT order_id, SUM(line_total) s, SUM(line_total) FILTER (WHERE promo) ps
                                       FROM commerce_orderitem GROUP BY order_id) i ON i.order_id = o.id
WHERE o.items_subtotal <> i.s OR o.promo_subtotal <> COALESCE(i.ps, 0);
-- K2 order status equals the newest status-changing event
SELECT o.id FROM commerce_order o JOIN LATERAL (SELECT status_after FROM commerce_orderevent e WHERE e.order_id = o.id AND e.status_after <> ''
                                               ORDER BY e.at DESC, e.id DESC LIMIT 1) l ON true
WHERE l.status_after <> o.status;
-- K3 paid orders have exactly one converted reservation set and no active one
SELECT o.id FROM commerce_order o WHERE o.status IN ('processing','handed_to_courier','delivered')
  AND EXISTS (SELECT 1 FROM commerce_stockreservation r WHERE r.order_id = o.id AND r.state = 'active');
-- K4 cashback_used equals the hold amount
SELECT o.id FROM commerce_order o LEFT JOIN loyalty_cashbackhold h ON h.order_id = o.id
WHERE (o.cashback_used > 0 AND (h.id IS NULL OR h.amount <> o.cashback_used)) OR (o.cashback_used = 0 AND h.id IS NOT NULL);
-- K5 inbox pushes stuck in 'sending' for more than 1 h
SELECT id FROM notifications_inboxnotification WHERE push_state = 'sending' AND updated_at < now() - interval '1 hour';
-- K6 campaigns 'sending' with no batch for 1 h
SELECT id FROM notifications_campaign WHERE state = 'sending' AND updated_at < now() - interval '1 hour';
```

---

## 7. `seed_demo`

### 7.1 Framework
- `manage.py seed_demo [--anchor 2026-10-24T12:05:00+05:00] [--no-keep-open] [--only <module>[,…]]`. Refuses to run
  unless every provider is in `fake` mode and `DEBUG` or `ALLOW_SEED_DEMO=1` (never on a production database by
  accident). `manage.py seed_policy` (02 §1.1) stays separate and is called by `seed_demo` first.
- Registry (as built in `core.seeding`): every module may ship `glowderma/<module>/seeds.py` with one function
  decorated `@register_seeder("<module>", order=…)`; `seed_demo` imports them (`discover_seeders()`) and runs them in
  `order` = layer order `accounts (10) → integrations (20) → site_config (30) → catalog (40) → professionals (50) →
  loyalty (60) → commerce (70) → appointments (80) → notifications (90)` (`payments`, `delivery`, `customers`,
  `reporting` have nothing to seed: their rows are produced by the replayed flows). Actual flags: `--anchor`,
  `--no-keep-open`, `--sequences-only`, and `--force` (required when `DEBUG` is False; there is no `--only` and no
  `ALLOW_SEED_DEMO`). The whole run happens inside one transaction; `reset_sequences()` runs once at the end.
- **Replay through services.** Business rows are created by calling the real services (`commerce.services.checkout`,
  `confirm_payment_effects`, `apply_bts_status`, `cancel_order`, `transition`, `loyalty.services.record_store_purchase`,
  `adjust`, `appointments.services.*`, `notifications.services.*`) under a controllable clock, so events, ledger
  entries, purchases, holds, payments, shipments and inbox rows come out of production code paths and the vectors
  are asserted against the same code the API runs. Only configuration tables (categories, products, specialists,
  services, hours, templates, segments, settings, customers, devices, policy versions) are inserted directly.
- `SeedContext` helpers (`glowderma/core/seeding.py`, as built): `ctx.at(instant)` (= `pinned_clock(instant)`) — context
  manager that sets `timeutils.now` to a fixed aware datetime (module attribute, as the tests do) and restores it;
  `ctx.random(seed)` (= `MockRandom(seed)`) — the Python port of the frontend's `createRandom` (mulberry32, 32-bit
  arithmetic with `& 0xFFFFFFFF`, `Math.imul` semantics; `next/int/chance/pick/shuffle`; vectors pinned in
  `core/tests/test_seeding.py`) so generated people/products/appointments match the mock byte for byte;
  `ctx.media_placeholder(kind, tone)` (= `placeholder_art`) — a deterministic 96 px WebP per art kind/tone.
  **Not provided:** `ctx.sequence` — design rows are inserted with explicit ids and `reset_sequences()` re-syncs every
  serial sequence at the end of the run (where later sections say `ctx.sequence(table, n)`, read "insert the following
  rows with explicit ids from n"); `ctx.deliver` — a seeder that must replay an outbox effect calls the handler
  directly (`core.outbox` registry) under `ctx.at(...)`, since the outbox dispatcher's on-commit `.delay` is inert
  without a worker anyway. Seeders that write media files rely on the root `conftest.py` `media_root` fixture
  (session-scoped temporary `MEDIA_ROOT`) in tests.
- Idempotent by natural keys: staff `email`, customer `phone`, product `billz_id`, category `slug`, professional
  `name`, service `name_uz`, order `id` (a scenario whose order id exists is skipped entirely), appointment
  `(professional, start)`, campaign `(title, created_at)`, segment `name`, policy `version`, Billz purchase
  `billz_sale_id`. Re-running the command on a seeded database makes no changes and prints what it skipped.
- Explicit-id policy: design rows get the ids the contract or the frontend mock cites (inserted with explicit ids);
  generated rows take ids from 1000 upwards for events/ledger/appointments/inbox (the sequences are parked at 999
  before the first generated row), and their natural ranges elsewhere. After every module seeder, §7.9 resets the
  sequences.
- `--keep-open` (default on): seeded `awaiting_payment` orders get `reservation_expires_at = max(created_at +
  reservation_minutes, real_now + 7 days)` so a running expiry beat does not cancel them a minute after seeding;
  **the same value is written to the order's active `StockReservation.expires_at` rows and its active
  `CashbackHold.expires_at`** (02 §1.5/§1.6 define both as "= `Order.reservation_expires_at`"; otherwise the K-style
  consistency checks and the `hold_active_expires_idx`/`reservation_active_expires_idx` scans would disagree with
  the order — review minor 17c). With `--no-keep-open` the truthful values are kept everywhere (used by tests that
  exercise the expiry job on seeded data).
- The anchor (`2026-10-24T12:05:00+05:00`) is the design's "now": Saturday 24 October 2026, after the 12:00 campaign
  was sent and while order №10252 (11:42) is still within its reservation. Every seeded timestamp is absolute
  (design dates), not relative to the real clock; dashboard `range=today` therefore reproduces the seeded day only
  when `timeutils.now` is pinned to the anchor (tests) — a live demo uses `range=custom&from=2026-10-24&to=2026-10-24`.

### 7.2 `accounts.seed`
- Staff: `admin@glowderma.uz`, `full_name="Administrator"`, `is_staff=True`, password from `SEED_ADMIN_PASSWORD`
  (default `glowderma-demo-2026`, printed once). Every "Administrator" actor below is this row (id 1).
- Customers 1–12 exactly as `people.ts NAMED_CUSTOMERS` (ids, names, phones). Customers 13–432: the 420 generated
  rows from `createRandom(4812)` with the mock's call order per row (`chance(0.78)` female, `pick(SURNAMES)`,
  `pick(FEMALE|MALE)`, duplicate-name retry, `int(0, 9_999_999)` digits, `pick(OPERATORS)`), so names and phones equal
  the frontend's. Per-customer facts from `createRandom(id × 97)` in the mock's call order: `store = int(0,60) × 50 000`
  (used in §7.7), `registered_at = 2025-{int(1,12)}-{int(1,28)} 12:00`, `language = uz if chance(0.75) else ru`,
  device `iPhone 13 / iOS 18.1` if `chance(0.55)` else `Samsung Galaxy A54 / Android 14`, `push_enabled =
  chance(0.8)`. Malika (5): `registered_at 2025-03-12 10:20`, `language uz`, `address_text "Toshkent, Chilonzor,
  9-kvartal"`, device `iPhone 14 / iOS 18.1 / 1.0.0`, `last_active_at 2026-10-24 09:12`, push on. Other named
  customers: `address_text` = the address of their first delivery order — `commerce.seed` calls
  `accounts.services.add_address(customer_id, text=…, is_default=True)` (01 §1.3), which recomputes `address_text`;
  the seeder never writes an `accounts` column directly (AGENTS.md cross-module rule, review minor 21). Malika's
  address is created the same way by `accounts.seed`.
- Devices: one active device per customer (`fid = f"seed-fid-{id}"`, `fcm_token = f"seed-fcm-{id}"`, `platform` from
  the model, `app_version 1.0.0`, `last_active_at 2026-10-23 20:15`).
- Deleted customers: ids 427–432 are soft-deleted through `accounts.services.delete_customer` at `2026-10-20 10:00`
  (names scrubbed, devices deactivated) → `deleted_customers = 6`, and the list renders "O‘chirilgan mijoz".

### 7.3 `integrations.seed`
`IntegrationState(billz).last_sync_at = anchor − 2 min` (= `2026-10-24T12:03:00+05:00`; the products screen shows
"Sinxronlangan: 2 daqiqa oldin" at the anchor) and one `Job(kind=billz_import, state=succeeded, params {mode: delta},
result {created: 0, updated: 248, unchanged: 0, new_from_billz: 0}, created_by=admin)` at that time. Other providers:
`last_sync_at = null`.

### 7.4 `site_config.seed`
`reservation_minutes 15`, `support_phone +998712000000`, `pickup_address_uz "Toshkent, Chilonzor tumani, Bunyodkor
shoh ko‘chasi, 12"` (ru/en empty), `updated_by null`. (Address text is invented — not in the design.)

### 7.5 `catalog.seed`
- Categories (slug / uz / ru / en, `position` in this order): `serums` Serumlar / Сыворотки / Serums; `cleansing`
  Tozalash / Очищение / Cleansing; `moisturizing` Namlantirish / Увлажнение / Moisturising; `sun` Quyoshdan himoya /
  Защита от солнца / Sun care; `toners` Tonerlar / Тонеры / Toners (uz labels = `CATEGORY_LABEL` in the frontend;
  ru/en invented).
- Products 1–248 exactly as `catalog.ts buildCatalog()`: ids 1–8 the design rows (1 Gialuron kislotali serum
  `4607012301` 185 000, 3 images; 2 SPF 50 `4607012401` 240 000 **promo**; 3 Yumshoq tozalovchi ko‘pik `4607012388`
  128 000; 4 Seramidli namlantiruvchi krem `4607012345` 312 000; 5 Niatsinamidli toner `4607012422` 156 000 stock 0;
  6 Retinolli tungi serum `4607012399` 276 000; 7 `4607012455` VIT C SERUM hidden + `is_new_from_billz`, no texts,
  no category; 8 Azelain kislotali krem `4607012366` hidden), ids 9–18 the supporting products (`4607012410` …
  `4607012419`), ids 19–248 generated with `createRandom(248)` in the mock's call order (shuffle of the 20 × 13
  ingredient/form combos, `pick(volumes)`, `chance(0.55)` translated, `int(price range) × 1000`, `int(1,40)` stock,
  `chance(0.08)` promo, `chance(0.05)` hidden, `chance(0.15)` dark cap), then the mock's overrides: generated
  indexes `[11, 37, 64, 102, 151, 199]` → stock 0 (seven sold-out products in total with №5), `[5, 88, 160]` →
  hidden + new from Billz + no category/texts (four new in total with №7). `billz_price = price`,
  `billz_synced_at = ProductStock.synced_at = IntegrationState(billz).last_sync_at`.
- Images: one placeholder WebP per product with an `art` kind (product 1 gets three: dropper/box/texture), re-encoded
  through `core.images` and stored under `MEDIA_ROOT/products/<id>/…`; products with `art = null` get none (they are
  hidden, so the active rule holds). `image_url` snapshots on order items therefore resolve to real files.
- Product 1 `last_change`: `price_changed 175 000 → 185 000` by Administrator at `2026-10-22 18:04`.

### 7.6 `professionals.seed`
`Location 1` "Glow Derma klinikasi" (address = pickup address). Professionals (ids 1–3, `position` 0–2):
1 Nodira Rahimova dermatologist, 2 Jasur Aliyev dermatologist, 3 Dilnoza Yusupova cosmetologist. Services (ids):
1 Dermatolog konsultatsiyasi / Konsultatsiya 30 min; 2 Qayta ko‘rik / Qayta ko‘rik 20 min; 3 Yuzni chuqur
tozalash 75 min; 4 Kimyoviy pilling 60 min; 5 Namlantiruvchi parvarish 60 min. `ProfessionalService`: Nodira
{1: 150 000, 2: 100 000}; Jasur {1: 120 000, 2: 90 000}; Dilnoza {3: 320 000, 4: 380 000, 5: 260 000}.
`WorkingHours` Mon–Sat 09:00–18:00 for all three (no Sunday rows — the mock skips Sundays); Jasur `break 13:00–14:00`
on every weekday (renders as the `off` block with `label null` → FE "Ish vaqti emas" per contract §5.6
`label ?? (kind === "busy" ? "Band qilingan" : "Ish vaqti emas")`; the contract example for Jasur is exactly
`{"start": "13:00", "end": "14:00", "kind": "off", "label": null}`). `BlockedPeriod`: Nodira
`2026-10-26 16:00–16:30 busy "Band qilingan"`.

### 7.7 `loyalty.seed`
- Policy versions (02 §1.1; `effective_from > created_at` holds): v1 `created 2026-05-20 10:00, effective 2026-06-01
  00:00, author null ("Tizim"), basis before_purchase, earn_on_cashback_paid false, tiers [0, 20M) 1.00`;
  v2 `created 2026-08-01 10:00, effective 2026-08-15, author admin, earn false, tiers + [20M, 30M) 2.00`;
  v3 `created 2026-09-20 10:00, effective 2026-10-01, author admin, earn **true** (G-2), tiers + [30M, 50M) 3.00`.
  Derived: v1 "1-iyun – 14-avg 2026" expired, v2 "15-avg – 30-sen 2026" expired, v3 "1-okt 2026 dan" active;
  summaries "Boshlang‘ich: 20 mln gacha 1%", "20–30 mln: 2% qo‘shildi", "30–50 mln: 3% qo‘shildi".
- Store purchases of the generated customers (ids 13–426): `store = int(0,60) × 50 000` from §7.2's PRNG; when
  `> 0`, `record_store_purchase(billz_sale_id=f"S-{80000 + id*7}", billz_sale_ref=same, amount=store, sold_at
  2026-09-05 12:00)` → credit `store // 100` under v2 (1 % below 20 M), exactly the mock's "Chek … so‘m · 1%" rows.
  Named customers 1–12 except Malika get the same treatment; Javohir (7) gets `max(store, 2 000 000)` so that his
  order №10246 can use 20 000 so'm of cashback.
- Malika's timeline is replayed in chronological order by `loyalty.seed` and `commerce.seed` together (one ordered
  script, so `balance_after` chains are right). Ledger ids are pinned with `ctx.sequence` (contract example `id 112`
  for S-88213; `120/121` for №10248):

| When (Tashkent) | What | Ledger id · amount · balance_after |
|---|---|---|
| 2025-04 → 2026-05 | 10 pre-policy online orders (ids 10012, 10026, 10040, 10054, 10068, 10082, 10096, 10110, 10124, 10138 — all dated before 2026-06-01 by the interpolation of §7.8, which puts id 10155 at about 1 June 2026; delivered; items: [ceramide 312 000], [hyaluron serum 185 000], [retinol 276 000], [toner 156 000 + mask 45 000], [centella 214 000], [peptide serum 298 000], [balm 96 000], [salicylic gel 142 000], [enzyme 174 000 + lip balm 39 000], [hyaluron serum 185 000 + toner 156 000] = **2 278 000**) → purchases with `policy null`, `cashback_amount 0`, no entries | — |
| 2025-04-18, 2025-08-03, 2025-12-12, 2026-03-07 | pre-policy store sales S-60112 1 240 000, S-61350 985 000, S-62877 864 000, S-63905 600 000 = **3 689 000** (no credits) | — |
| 2026-06-01 09:30 | S-64998 361 000 (v1) | 100 · +3 610 · 3 610 |
| 2026-06-03 10:55 | S-65230 1 245 000 | 101 · +12 450 · 16 060 |
| 2026-06-20 15:18 | S-68011 987 000 | 102 · +9 870 · 25 930 |
| 2026-07-11 11:42 | S-71840 1 530 000 | 103 · +15 300 · 41 230 |
| 2026-08-02 17:05 | S-76102 621 000 | 104 · +6 210 · 47 440 |
| 2026-09-14 12:20 | S-81377 864 000 (v2) | 105 · +8 640 · 56 080 |
| 2026-10-02 14:31 / 14:34 / 16:10 | order №10198 created / paid (credit, v3) / cancelled by admin → reversal | 106 · +1 560 · 57 640; 107 · −1 560 · 56 080 |
| 2026-10-09 13:40 | S-86540 412 000 | 108 · +4 120 · 60 200 |
| 2026-10-12 10:03 | adjustment +2 950 by Administrator "Kassada QR o‘qilmadi, chek S-87011 qo‘lda bog‘landi" (`customers`→`loyalty.adjust`; audit `cashback.adjusted`) | 109 · +2 950 · 63 150 |
| 2026-10-18 15:20 / 15:25 / 20:25 | order №10231 created / paid (debit then credit) / delivered (admin, pickup) | 110 · −20 000 · 43 150; 111 · +3 140 · 46 290 |
| 2026-10-21 11:47 | S-88213 231 000 | 112 · +2 310 · **48 600** |
| 2026-10-23 14:02 / 14:05 | order №10248 created / paid (debit, credit) | 120 · −48 600 · 0; 121 · +5 680 · **5 680** |

  Resulting card: `totals {store 9 940 000, online 3 640 000, lifetime 13 580 000}`, `online_orders {12, 10248,
  2026-10-23T14:02:00+05:00}`, `tier {3, "1.00", next {"2.00", 20 000 000, 6 420 000, 0.679}}`, `balance 5 680, held 0,
  available 5 680`, `groups [1, 2]`, `upcoming_appointments 1`, `device iPhone 14`, 15 ledger rows newest first
  (l-12 … l-00 plus 120/121), 8 + 2 + 2 + 10 purchases.
- The deliberate negative balance (02 §4.5 item 4): customer 13 — order A №10210 (pickup, paid 2026-10-10, credit
  posted, stays `processing`), order B №10215 (2026-10-14, `cashback_used = balance after A`), then A is cancelled by
  the admin on 2026-10-16 → the reversal of A's credit drives the balance below zero (flagged, L5).

### 7.8 `commerce.seed`
Fixed orders (ids, statuses, items, fees and cashback exactly as `orders.ts DESIGN_ORDERS` + `MALIKA_HISTORY`;
times follow the mock's offsets: paid = created + 3 min, Billz echo + 3 min 20 s, BTS create + 4 min, handed = the
mock's shipment `updatedAt` when drawn else + 158 min, delivered = + 26 h (delivery) / + 5 h (pickup, by the admin's
`transition`), expiry cancel + 15 min, admin cancel + 60 min; fake ids follow the mock: Click `CLK-` /
Payme `PME-` + `str(7_741_000_000 + id × 37)[:10]`, №10248 `CLK-7741920385`):

| № | Customer | Created | Status · payment | Fulfilment · fee · cashback | Items (billz ids × qty) | Shipment / notes |
|---|---|---|---|---|---|---|
| 10252 | 1 Sherzod | 24 Oct 11:42 | awaiting_payment · Payme pending | delivery 28 000 · 0 | 345, 399, 412, 415 | `not_created`, `pending false`; reservation kept open (`--keep-open`); total 1 128 000 |
| 10251 | 2 Aziza | 24 Oct 11:20 | processing · Click paid 11:23 | pickup · 0 · 0 | 399, 410, 416, 417 | `not_required`; total 456 000 |
| 10250 | 3 Gulnora | 24 Oct 10:05 | processing · Payme paid 10:08 | delivery 27 000 · 0 | 401, 416 | automatic BTS create **failed** at 10:09 (`metadata.fake.bts.fail_attempts = 1`, `error_code "address_not_found"`) → `failed, attempts 1, pending false` → `retry_shipment = true`; total 312 000 |
| 10249 | 4 Bekzod | 23 Oct 18:30 | cancelled (`reservation_expired`) · Click failed | delivery 26 000 · 0 | 412 | expiry job at 18:45: `reservation_expired {released_item_count 1, payment_failed true}`; total 240 000 |
| 10248 | 5 Malika | 23 Oct 14:02:00 | handed_to_courier · Click paid 14:05:00 | delivery 25 000 · 48 600 | 345 ×1, 388 ×2, 401 ×1 | channel `ios`; address "Toshkent, Chilonzor tumani, 9-kvartal, 12-uy, 45-xonadon"; events 1–7 (vector below); Billz uuid `0d6f6c1e-5a7b-4c1e-9a43-2f1d8e3b7a10` (`record_online_purchase(…, billz_order_uuid=…)` keyword, 02 §1.6; seed-only use, S-1); tracking `48213907`, tariff 25 000, handed 16:40 via webhook, `bts_status "200"`, `push_sent true`; total 784 400 |
| 10247 | 6 Nilufar | 23 Oct 12:15 | delivered · Payme paid 12:18 | pickup · 0 · 0 | 301, 345, 422, 416 | delivered 17:15 by Administrator (`status_changed`, "mijozga topshirildi"); total 698 000 |
| 10246 | 7 Javohir | 23 Oct 09:48 | delivered · Payme paid 09:51 | delivery 25 000 · 20 000 | 415 ×2, 345 ×2, 399, 417 | tracking `48211544`; handed 12:26 (+158) webhook "200"; delivered 24 Oct 10:12 (mock `updatedAt`) webhook "300"; total 1 540 000 |
| 10245 | 8 Kamola | 22 Oct 19:02 | handed_to_courier · Click paid 19:05 | delivery 25 000 · 0 | 399, 416, 417 | tracking `48209981`; handed 23 Oct 11:30 webhook; total 385 000 |
| 10244 | 9 Dilshod | 22 Oct 16:37 | cancelled (`admin`, "Mijoz so‘rovi bilan") · Click **refunded** | delivery 25 000 · 0 | 411, 416 | paid 16:40; the BTS create message is **not** delivered before the cancel at 17:37 (worker backlog story) → the handler runs after, sees `cancelled` and marks done → shipment stays `not_created` as drawn; refund `requested` → `succeeded` 17:39 (fake) → payment `refunded`; reversal of the credit; total 212 000 |
| 10243 | 10 Sevara | 22 Oct 13:10 | delivered · Payme paid 13:13 | delivery 25 000 · 0 | 401, 345, 411, 418 | tracking `48207320`; handed 15:48; delivered 23 Oct 15:05 (mock `updatedAt`); total 924 000 |
| 10231 | 5 Malika | 18 Oct 15:20 | delivered · Payme paid 15:25 | pickup · 0 · 20 000 | 301 ×1, 416 ×2, 417 ×1, **401 ×1 (promo)** | items 554 000, promo 240 000, total 534 000; credit 3 140 on base 314 000 `promo_excluded`; delivered 20:25 by Administrator |
| 10198 | 5 Malika | 2 Oct 14:31 | cancelled (`admin`, "Mijoz so‘rovi bilan") · Click refunded | delivery 25 000 · 0 | 422 | paid 14:34, credit 1 560; BTS create failed 14:35 (`fail_attempts 1`); cancelled 16:10 → reversal; refund succeeded 16:12; total 181 000 |

**№10248 history vector produced by the replay** (newest first; `id`s pinned 1–7: the event sequence is parked at
999 before the first replayed order, so every order replayed before №10248 — including №10198 and №10231 — writes
events with ids ≥ 1000; right before №10248 the seeder calls `ctx.sequence("commerce_orderevent", 1)`, its seven
events take 1–7, and the sequence is moved back above the maximum afterwards; ledger ids stay pinned as in §7.7):
`7 handed_to_courier 16:40:00 bts handed_to_courier {via webhook, bts_status "200", push_sent true}` ·
`6 shipment_created 14:06:00 system {tracking_number "48213907", tariff 25000, manual false}` ·
`5 billz_sale_recorded 14:05:20 system {billz_order_id "0d6f6c1e-…"}` ·
`4 cashback_credited 14:05:00 system {ledger_entry_id 121, amount 5680, base 568000, percent "1.00", policy_version 3}` ·
`3 payment_confirmed 14:05:00 system processing {provider click, transaction_id "CLK-7741920385", amount 784400,
reservation_converted true}` · `2 reservation_created 14:02:00 system {item_count 3, minutes 15, expires_at
14:17:00}` · `1 order_created 14:02:00 system awaiting_payment {channel ios, item_count 3, total 784400}` — equal to
contract §4 key for key. `Order.cashback` = `[{120 debit −48600 …}, {121 credit 5680 base 568000 percent "1.00"}]`,
`available_actions {transitions ["delivered"], cancel true, retry_shipment false}` (the contract example shows
`transitions: []` for an order in `handed_to_courier`, which contradicts its own rule "handed_to_courier →
delivered"; the rule wins — open point S-2).

Generated orders: **230** rows (242 ids `10001–10242` minus `10198`, `10231` and Malika's ten pre-policy ids;
review minor 17a), `created_at`
by linear interpolation between the anchors `(10000 → 2025-03-20 10:00)`, `(10198 → 2026-10-02 14:31)`, `(10231 →
2026-10-18 15:20)`, `(10243 → 2026-10-22 13:10)` with a deterministic jitter that keeps ids monotonic in time and
every order inside 09:00–22:00 Tashkent. Facts from `ctx.random(1284)` with the mock's distributions (75 % delivery,
1–4 lines of active products, qty 2 with 15 %, 50/50 provider, 55 % iOS, fee `int(18,35) × 1000`, cashback on 15 % of
paid orders = `min(int(50,500) × 100, available)` and only after 2026-06-01, districts list for addresses, BTS tracking
numbers counting down from 48207000). Status allocation (so the tab counts are exact for the open tabs): the five
newest generated ids `10238–10242` → `awaiting_payment`; `10227–10237` minus `10231` → `processing` (10); `10220–10226`
→ `handed_to_courier` (7); `10025, 10050, …, 10200` (multiples of 25) and `10210` → `cancelled` (9; 60 % of them
`reservation_expired`, the rest `admin` with "Mijoz so‘rovi bilan", `10210` is customer 13's order A); everything
else → `delivered` (199 generated; 5 + 10 + 7 + 9 + 199 = 230). Customers: `ctx.random` picks among ids 1–426
excluding Malika; the first delivery order of a named customer creates their default `Address` through
`accounts.services.add_address` (§7.2), which sets `address_text`. Pre-policy orders earn nothing; later ones credit
under the policy active at their confirmation.

Resulting totals the seed **does** reproduce: `status-counts {all 252, awaiting_payment 6, processing 12,
handed_to_courier 9, delivered 213, cancelled 12}` (fixed rows 10243–10252 → 1/2/2/3/2; 10231 delivered; 10198
cancelled; Malika's ten pre-policy orders delivered; generated 5/10/7/199/9) for the open tabs as drawn (6 / 12 / 9);
`nav-counts {12, 3}`;
`attention {bts_failed 1, pending_appointments 3, next_appointment_at 2026-10-26T09:00:00+05:00, new_from_billz 4,
refunds_needing_action 0}`; `stats.out_of_stock 7`; products `total 248`.

### 7.9 Sequence reset
After each module seeder and at the end:
```sql
SELECT setval(pg_get_serial_sequence(%(table)s, 'id'),
              GREATEST(COALESCE((SELECT MAX(id) FROM <table>), 0), %(floor)s), true);
-- floor = 10000 for commerce_order (so the next real order is ≥ 10253 after a seed, 10001 on an empty table via
--         setval(seq, 10001, false) when MAX(id) IS NULL); floor = 0 elsewhere, with the empty-table case using
--         setval(seq, 1, false).
```
for every table the seeder touched (customers, devices, products, images, categories, professionals, services,
links, hours, blocks, policies, tiers, accounts, ledger entries, holds, purchases, purchase items, orders, items,
events, reservations, payments, refunds, shipments, shipment events, appointments, appointment events, segments,
members, campaigns, recipients, inbox, jobs, audit entries, outbox messages).

### 7.10 Mock numbers that are **not** reproduced (internally inconsistent) and what the seed uses instead

| # | Frontend mock value | Why it cannot be reproduced | Seed value |
|---|---|---|---|
| 1 | Malika's balance **48 600** on the card (= Σ of the 13 drawn ledger rows) while order №10248 (two days later) debits 48 600 and credits 5 680 | both cannot hold at once | balance **5 680** after №10248 (G-3); the 13 drawn rows end at 48 600 before it |
| 2 | Order №10231 items (serum + salicylic gel = 327 000, no promo) vs its ledger (credit on base 314 000 "aksiyasiz qism", debit 20 000) and purchases row (amount 314 000, promo 0) | the ledger implies promo lines and a 314 000 non-promo base | items 185 000 + 2 × 45 000 + 39 000 (= 314 000 non-promo) + SPF 50 240 000 (promo); purchase `amount 554 000, promo 240 000, cashback 3 140`, total 534 000 |
| 3 | №10231 debit at 15:22, credit at 15:25 | the debit is posted at hold capture = payment confirmation, same instant as the credit | both at 15:25 |
| 4 | №10248 credit "1% × 568 000" with `earnOnCashbackPaid: false` | 808 000 − 240 000 − 48 600 = 519 400 under `false` | policy v3 `earn_on_cashback_paid = true` (G-2); base 568 000, credit 5 680 as drawn |
| 5 | Order tabs `all 1284` (`delivered 1198`, `cancelled 59`) with newest id 10252 | 1 284 orders cannot fit between the sequence start 10001 and 10252 (the mock's generated ids run below 10001) | 252 orders, ids 10001–10252: `6 / 12 / 9 / 213 / 12` |
| 6 | Generated orders all dated 1–22 October 2026 | 240 orders in 22 days contradicts the week chart (≈ 30/day) and Malika's 2025 history | spread 2025-03-20 → 2026-10-22, monotonic with ids |
| 7 | Dashboard series: today 38 orders / 12 480 000; week 230 / 74 760 000; month 906 / 278 890 000; "Kecha: 41", "O‘tgan hafta: 212", "O‘tgan oy: 905"; new customers 14 / 96 / 402 | the orders list has 3 orders on 24 Oct and 22 orders in 18–24 Oct; ids are monotonic | all dashboard numbers are **derived** from the seeded orders/payments/customers (today: 3 orders, revenue 768 000 = №10250 312 000 + №10251 456 000; `previous_period_orders` = count of 23 Oct = 4) |
| 8 | `total_customers 4812`, `deleted 61` | the mock has 432 customer identities | 432 / 6 |
| 9 | `total_devices 4751`; segment sizes 612 / 318; "Barcha · 4 751" | ≤ 432 devices exist | derived live (pushable devices among active customers; segment sizes from the rule evaluation); the **history rows** keep the drawn snapshots `recipient_count/sent/delivered` (4751/4751/4388, 4740/4740/4205, 318/318/301, 612/—/—) because they are stored snapshots |
| 10 | Notification history `delivered` numbers | M1 never fills `delivered` | stored as drawn on the four seeded campaigns only; no `CampaignRecipient` rows are materialised for them |
| 11 | 56 generated customers with negative balances (mock ledgers debit cashback that was never credited) | balances come from real ledger replay | none negative except customer 13 (deliberate, §7.7) |
| 12 | Contract example ids (payment 301, refund 7, audit 9001, inbox/campaign ids) | illustrative | not pinned; only ledger 100–112/120/121, order events 1–7, appointments 1–8, campaigns 1–4, segments 1–2, products 1–248, customers 1–432 are pinned |
| 13 | №10246 shipment `updatedAt 2026-10-24 10:12` vs "delivered = created + 26 h" (11:48) | one or the other | last BTS status (`delivered`) at 10:12 |
| 14 | №10244 / №10198 shipment `not_created` on cancelled paid delivery orders | a paid delivery order normally gets a BTS create within seconds | №10244: create message delivered after the cancel (skip path) → `not_created`; №10198: create failed before the cancel → `failed` (not drawn anywhere) |
| 15 | Products stats example in the contract (`active 201, hidden 47, new 6, out_of_stock 9`) | illustrative; the mock yields other counts | derived from the 248 seeded products (`new_from_billz 4`, `out_of_stock 7`) |
| 16 | Audit example `cashback.adjusted` at 2026-10-24 12:03 | the drawn adjustment row is dated 2026-10-12 10:03 | 2026-10-12 10:03 |
| 17 | Audit example `cashback.adjusted … balance_after 51550` (= 48 600 + 2 950) | the +2 950 adjustment is the drawn l-09 row of 2026-10-12, where Malika's chain stands at 60 200 → 63 150; 48 600 is her balance only after S-88213 on 2026-10-21 | audit `after.balance_after = 63150` (ledger id 109); the contract example is illustrative (G-2 neighbourhood, review minor 18) |

### 7.11 `appointments.seed`
Ids 1–8 = `appointments.ts DESIGN_APPOINTMENTS` on **2026-10-26** (Monday): 1 Kamola(8)·Nodira 09:00 Konsultatsiya
150 000 `requested` (req 24 Oct 08:41, app); 2 Aziza(2)·Nodira 11:00 Qayta ko‘rik 100 000 `confirmed` (21 Oct 19:12);
3 Sardor(11)·Nodira 14:00 Konsultatsiya 150 000 `confirmed` (22 Oct 10:05, **admin**, `created_by` Administrator);
4 Bekzod(4)·Jasur 09:30 Konsultatsiya 120 000 `requested` (24 Oct 07:58); 5 Nilufar(6)·Jasur 15:00 Qayta ko‘rik
90 000 `confirmed` (20 Oct 12:40); 6 Gulnora(3)·Dilnoza 10:00 Yuzni chuqur tozalash 320 000 `confirmed` (19 Oct 16:22);
7 Sevara(10)·Dilnoza 12:00 Kimyoviy pilling 380 000 `requested` (23 Oct 21:03); 8 Nozima(12)·Dilnoza 15:30
Namlantiruvchi parvarish 260 000 `confirmed` (22 Oct 09:47). Confirmed rows are confirmed by the Administrator 30 min
after the request (event + inbox row via the replayed `confirm`). Id 9: Malika·Dilnoza **2026-10-27 10:00**
Namlantiruvchi parvarish 260 000 `confirmed` (22 Oct 14:10) → `upcoming_appointments = 1`. Surrounding days (ids ≥
1000, the sequence parked at 999 as §7.1 rules; the earlier "≥ 100" was superseded): `createRandom(26)` in the mock's call order for offsets −3, −2, −1 (25 Oct is a Sunday → skipped), +1 … +4,
customers from ids 13+, past days `attended`, future `confirmed`; a generated slot that collides with a pinned row
(exclusion `IntegrityError`) is dropped. Pending summary at the anchor: `{count 3, next_at 2026-10-26T09:00:00+05:00}`.

### 7.12 `notifications.seed`
Templates come from the data migration (§3.1). Segments: 1 "Doimiy mijozlar" `{min_online_orders 3, null}`,
2 "10 mln+ xarid" `{null, min_lifetime_amount 10 000 000}` (Malika matches both). Campaigns (author Administrator;
bodies for n-1/n-2/n-4 are invented, n-3 uses the mock `DRAFT` body):

| id | kind | title | audience | created / scheduled / started / sent | `recipient_count` / `sent` / `failed` / `delivered` |
|---|---|---|---|---|---|
| 1 | promo | Quyoshdan himoya: 15% chegirma | segment 2 "10 mln+ xarid" | 2026-10-18 16:30 / — / 16:29 / 16:31 | 318 / 318 / **0** / 301 |
| 2 | general | Yangi mahsulotlar keldi | all | 2026-10-20 15:00 / 2026-10-21 10:00 / 10:00 / 10:02 | 4740 / 4740 / **0** / 4205 |
| 3 | promo | Kuzgi namlik to‘plami (link product 4 "Seramidli namlantiruvchi krem") | all | 2026-10-24 09:40 / 12:00 / 12:00 / 12:02 | 4751 / 4751 / **0** / 4388 |
| 4 | general | Yangi dermatolog qabuli ochildi | segment 1 "Doimiy mijozlar" | 2026-10-24 11:30 / **2026-10-26 10:00** / — / — | 612 / null / null / null, `scheduled` |

Rows 1–3 are inserted directly in state `sent` with the drawn counters (no recipients, §7.10 #9–10); because
`campaign_state_fields` requires `started_at`, `sent_at`, `sent` **and `failed`** to be NOT NULL in `sent`, the
seeder sets `failed = 0` and `started_at = sent_at − 2 min` (row 1: the immediate send started at 16:29, i.e. the
`created_at` minute — review minor 17b). Row 4 is `scheduled` and will be sent by beat when its time comes
(recomputing `recipient_count`). Order-status and
appointment inbox rows come from the replayed flows.

---

## 8. Django admin registration plan (`/django-admin/`, read-only except six professionals models)
- `core/admin.py` (B0) provides `ReadOnlyAdmin(ModelAdmin)`: `has_add_permission/has_change_permission/
  has_delete_permission → False`, `readonly_fields = all`, `list_per_page = 50`, `show_full_result_count = False`,
  `raw_id_fields` for every FK. `/django-admin/` sits behind `ADMIN_API_ALLOWED_NETS` (spec) and Django sessions;
  `createstaff` must set `is_staff=True` (the model defaults it to `False`) for staff to log in there.
- Read-only registrations of this group (`list_display` / `list_filter` / `search_fields`):

| Model | list_display | list_filter | search |
|---|---|---|---|
| `Order` | id, customer, status, fulfilment, total, payment_provider, created_at | status, fulfilment, payment_provider, channel | id, customer phone/name |
| `OrderItem` (inline on Order, read-only) | position, name, billz_id, quantity, unit_price, promo | — | — |
| `OrderEvent` (inline + list) | at, type, source, status_after, actor | type, source | order id |
| `StockReservation` | order, product, quantity, state, expires_at | state | order id, product billz_id |
| `Cart` / `CartItem` | customer, updated_at / cart, product, quantity | — | customer phone |
| `Appointment` | id, professional, customer, start, status, requested_via, price | status, professional, requested_via | customer phone/name |
| `AppointmentEvent` | at, type, source, actor | type | appointment id |
| `StatusTemplate` | status, text_uz, updated_at, updated_by | — | — |
| `CustomerSegment` / `SegmentMember` | name, rules, created_at / segment, customer, added_by | — | name / customer phone |
| `Campaign` | id, kind, title, audience_type, state, scheduled_at, sent_at, sent, failed | kind, state, audience_type | title |
| `CampaignRecipient` | campaign, customer, device, state, sent_at | state | campaign id |
| `InboxNotification` | customer, kind, title, push_state, created_at, read_at | kind, push_state, source_type | dedupe_key, customer phone |

  Plus the g1/g2 models by their own documents (all read-only). Every append-only model additionally hides the
  "save" buttons (`ReadOnlyAdmin` already does).
- **Six editable models** (`professionals`: `Location`, `Professional`, `Service`, `ProfessionalService`,
  `WorkingHours`, `BlockedPeriod`): `ServiceBackedAdmin(ModelAdmin)` overrides `save_model(request, obj, form,
  change)` → `professionals.services.create_<model>/update_<model>(obj_or_id, validated_data, actor=request.user,
  request=request)` and `delete_model` → `services.delete_<model>(…)`; the services validate (01 §5 invariants:
  weekday uniqueness, `start < end`, break inside the day, block `end > start`, `PROTECT` on referenced rows →
  friendly `messages.error`) and write `audit.record(action=f"{snake_model}.{created|updated|deleted}",
  entity_type=snake_model, entity_id=str(pk), actor=request.user, request=request, before=…, after=…)` with the
  §3.2 names (`location`, `professional`, `service`, `professional_service`, `working_hours`, `blocked_period`).
  `save_formset`/inlines are disabled so every write passes through `save_model`. `WorkingHoursOverride` stays
  read-only in M1 (open point S-3: make it the seventh editable model with `working_hours_override.*` audit
  actions, which §3.2 would need to list; the B6 admin endpoint creates overrides anyway).
- `StaffUser` admin is the M0 read-only one; `AuditEntry` read-only with `list_filter = (action, actor_type)`.

---

## 9. Complete migration order across apps (dependencies)
`INSTALLED_APPS` order: Django's contrib apps **including `django.contrib.postgres`** (after `staticfiles`, before
`rest_framework`; required by `GinIndex`, `ExclusionConstraint`, `DateTimeRangeField` — review blocker 1), then
`glowderma.core, glowderma.accounts, glowderma.integrations, glowderma.site_config,
glowderma.catalog, glowderma.professionals, glowderma.loyalty, glowderma.payments, glowderma.delivery,
glowderma.commerce, glowderma.appointments, glowderma.notifications, glowderma.customers, glowderma.reporting`
(the last two have no models). `.importlinter` layers per plan §4.1.

| # | App · migration | Contents | Depends on |
|---|---|---|---|
| 1 | `core.0007_db_extensions` | `pg_trgm`, `btree_gist`, ICU collation `glowderma_ci` (01 §6.1, T12) | `core.0006` |
| 2 | `core.0008_forbid_mutation_purge_guc` | `CREATE OR REPLACE FUNCTION glowderma_forbid_mutation()` — raises unless `current_setting('glowderma.allow_purge', true) = 'on'` **and** `TG_TABLE_NAME IN ('integrations_webhookinbox', 'core_auditentry')`, in which case it returns `OLD` (DELETE) / `NEW` (UPDATE) (T6). **`core/db.py` must be updated to emit this same body** in `append_only_trigger_sql`, because every later trigger migration re-runs `CREATE OR REPLACE FUNCTION` and would otherwise revert the GUC support (open point S-4) | `core.0007` |
| 3 | `accounts.0004_customer_device_address_smscode_social` | 01 §7 | `accounts.0003`, `core.0008` |
| 4 | `accounts.0005_authsession_customer_fk` | 01 §7 | `accounts.0004` |
| 5 | `integrations.0001_initial` · `0002_webhookinbox_append_only` · `0003_seed_integration_state` | 01 §7 | `accounts.0004`, `core.0008` |
| 6 | `site_config.0001_initial` · `0002_seed_singleton` | 01 §7 | `accounts.0001` |
| 7 | `catalog.0001_initial` | 01 §7 (generated column, GIN, deferrable unique; collation from `core.0007`) | `core.0007`, `accounts.0001` |
| 8 | `professionals.0001_initial` | 01 §7 | `accounts.0001` |
| 9 | `commerce.0001_initial` | `Cart`, `CartItem`, `Order`, `OrderItem`, `OrderEvent`, `StockReservation` (+ all constraints/indexes above) | `accounts.0004`, `catalog.0001`, `core.0007` |
| 10 | `commerce.0002_order_sequence` | `RunSQL("ALTER SEQUENCE commerce_order_id_seq START WITH 10001 RESTART WITH 10001", reverse "… START WITH 1 RESTART WITH 1")` | `commerce.0001` |
| 11 | `commerce.0003_orderevent_append_only` | `RunSQL(*append_only_trigger_sql("commerce_orderevent"))` | `commerce.0002`, `core.0008` |
| 12 | `loyalty.0001_initial` · `0002_append_only` | 02 §0.2 (`CashbackPolicy`, `CashbackTier`, `LoyaltyAccount`, `LedgerEntry`, `CashbackHold`, `Purchase`, `PurchaseItem`, `PosDevice`; three triggers) | 0001: `accounts.0004`, `catalog.0001`, `commerce.0001`, `core.0007` · 0002: `loyalty.0001`, `core.0008` |
| 13 | `payments.0001_initial` | `Payment`, `Refund` | `accounts.0004`, `commerce.0001`, `core.0007` |
| 14 | `delivery.0001_initial` · `0002_append_only` | `Shipment` (FK `core.OutboxMessage`), `ShipmentEvent` + trigger | 0001: `commerce.0001`, `core.0007` · 0002: `delivery.0001`, `core.0008` |
| 15 | `appointments.0001_initial` (as built) | `Appointment` (with `ExclusionConstraint`; `btree_gist` comes from `core.0007`), `AppointmentEvent`, and the `RunSQL` `appointment_duration_matches` (`"end" - start = make_interval(mins => duration_min)`) in the same migration | `accounts.0004`, `professionals.0001`, `core.0007` |
| 16 | `appointments.0002_appointmentevent_append_only` (as built) | trigger | `appointments.0001`, `core.0008` |
| 17 | — (merged into rows 15–16) | | |
| 18 | `notifications.0001_initial` | `StatusTemplate`, `CustomerSegment`, `SegmentMember`, `Campaign`, `CampaignRecipient`, `InboxNotification` | `accounts.0004`, `catalog.0001` |
| 19 | `notifications.0002_seed_status_templates` | `RunPython` inserting the four templates (§3.1; reverse deletes them) | `notifications.0001` |

Reverse migrations drop only what they created (`migrate <app> zero` smoke on a scratch DB in B2: triggers, the
exclusion constraint, the duration check, the sequence restart, the extension `btree_gist` — dropped by
`BtreeGistExtension` reverse only if nothing else uses it). Server step: the first `migrate` needs `CREATE` on the
database for the two extensions and the collation (plan B2).

---

## 10. Tests this design implies for B2 (models and migrations; services/APIs come in B5/B6/B7)
- One test per `CheckConstraint`/`UniqueConstraint`/exclusion above through `IntegrityError` on raw `objects.create`:
  `order_total_formula`, `order_address_by_fulfilment`, `order_cancellation_*`, `orderitem_line_total`, second
  active reservation of the same line, `orderevent_status_by_type` (e.g. `cancelled` with `status_after=""`),
  `orderevent_source_by_type` (`status_changed` from `system`), overlapping `requested`/`confirmed` appointments of one
  professional (and **no** error when one of them is `rejected`/`cancelled`/`attended`), `appointment_duration_matches`,
  `template_only_raqam_placeholder` (`{ism}` rejected, `{raqam}` twice accepted), `segment_name_ci_unique`
  ("VIP" vs "vip"), `campaign_audience_fields`, `recipient_campaign_customer_unique`, `inbox` `dedupe_key` unique.
- Append-only (§0.2) for `OrderEvent` and `AppointmentEvent`, incl. the purge GUC being refused on them.
- Sequence: after `migrate`, `pg_sequences.start_value = 10001` for `commerce_order_id_seq` and the first `Order`
  created in a fresh test DB has `id ≥ 10001`; the seeder's `setval` leaves `nextval ≥ GREATEST(MAX(id)+1, 10001)`.
- `available_stock` arithmetic with active/converted/released rows and a `synced_at` before/after conversion.
- `segment_members_q` vectors: rule-only member, manual-only member, deleted manual member (excluded), no rules
  (manual only), both rules ANDed.
- `manage.py check` is clean with `django.contrib.postgres` installed (no `postgres.E005`; A-1 resolved);
  `btree_gist` exists after migrate on PG 16 and 18.
- Seeder: `seed_demo` twice → identical row counts (idempotent); №10248 history vector equals contract §4 key for key
  (ids 1–7 included); Malika's card vector (§7.7); `status-counts` and `attention` values of §7.8; customers 13–432
  names equal the frontend's `CUSTOMERS` fixture for a sample of ids (PRNG port).
- `makemigrations --check --dry-run` clean; `migrate <app> zero` for the three apps on a scratch DB.

---

## 11. Consolidated open points (this group)

| # | Point | Default in this design |
|---|---|---|
| C-1 / C-2 | Cart persists no checkout draft; per-line quantity ceiling | no draft; 10 (setting), DB hard cap 99 |
| C-3 | Recipient snapshot on `Order` for BTS (D-3) | `recipient_name/phone` columns, copied at checkout |
| C-4 | Cashback never pays the delivery fee (`cashback_used ≤ items_subtotal`) | enforced by CHECK |
| C-6 | Customer-initiated cancel endpoint (M2) | only from `awaiting_payment`, optional |
| C-7 | `item_count` = order lines | lines |
| C-8 | No `payment_failed` event on admin cancel / expiry of a pending payment | **decided** (review major 7): `cancelled`/`reservation_expired` carry the fact; `02-…` §2.1 aligned; Payme state-1 transactions stay `pending` (02 P-2) |
| C-10 / C-11 | `release_reason = payment_failed` unused; admin "available" stock column | keep; no contract change |
| A-1 | `ExclusionConstraint` / `django.contrib.postgres` | **resolved**: the app is installed (review blocker 1); ORM constraint, no fallback |
| A-2 / A-3 | Audit actions and pushes for admin create/reschedule/cancel of appointments (T10) | add `appointment.created/rescheduled/cancelled`; push on reschedule and admin cancel with fixed texts |
| A-4 | Slot alignment for admin-created appointments | any minute for admin, `slot_step_min` for M2 |
| N-1 | Push on reservation expiry | none |
| N-2 | Appointment push texts not admin-editable | fixed `texts.py` |
| N-5 | `Campaign.audience_segment` `SET_NULL` (skeleton said `PROTECT`) | `SET_NULL` + name snapshot, because the contract allows deleting a segment with sent history |
| N-6 | Batch size 500, fake FCM failure rate | settings |
| N-7 / N-10 | Seeded `delivered` counters; inbox rows for customers without a device | stored as drawn; rows created with `push_state = no_device` |
| N-8 / N-9 | Retention of recipients (180 d) and inbox rows (365 d) | M3 purge tasks |
| X-1 | Dead-letter hook registry in `core.outbox` (`on_dead(topic)`) so `commerce.tasks` can mark shipments failed / refunds **failed (`dead_letter`)** | add in B2 (small core change) |
| S-1 | `record_online_purchase(…, billz_order_uuid=None)` keyword (02 §1.6) used by the seeder to pin the №10248 Billz uuid | merged into the 02 signature, default `uuid4()` |
| S-2 | Contract example `available_actions.transitions: []` for a `handed_to_courier` order contradicts its own rule | the rule wins (`["delivered"]`); fix the example |
| S-3 | `WorkingHoursOverride` editable in Django admin (seventh model) | read-only in M1; propose adding |
| S-4 | `core/db.py` must emit the GUC-aware `glowderma_forbid_mutation()` body (every later trigger migration re-creates the function) | change `db.py` together with `core.0008` |
| S-5 | Anchor-based seeding: dashboard `today` reproduces the seeded day only with a pinned clock | `--anchor` + `range=custom` for live demos |
| S-6 | `--keep-open` sets seeded `reservation_expires_at` (order, reservations and hold alike) into the future (inconsistent with `created_at`) | default on for demos, off in tests |
| G-1 / G-2 / G-3 | (from `02-…`) `commerce` migrates before loyalty/payments/delivery; policy v3 `earn_on_cashback_paid = true` (**decided by plan §4.3**, contract §7 delta line); Malika's balance 5 680 | adopted here |
