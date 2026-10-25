# Adversarial review of the M1 model design (01 / 02 / 03)

> Reviewer pass over `01-accounts-integrations-site_config-catalog-professionals.md`, `02-loyalty-payments-delivery.md`
> and `03-commerce-appointments-notifications-customers-reporting.md`, read in full and checked line by line against
> `docs/api/admin-api-v1.md` (every response field, filter, export column, enum and error rule), the design spec
> sections named in the brief, `docs/decisions/open-questions.md`, the approved plan §4 / §5.3, `glowderma/core/*`,
> `glowderma/accounts/models.py` and the installed Django 6.1.2 sources (`.venv/lib/python3.14/site-packages/django`).
>
> Severity: **blocker** = the design cannot be implemented or migrated as written · **major** = a flow, test or
> contract field breaks at runtime / in CI unless changed · **minor** = inconsistency, naming or wording to fix.
>
> **Counts: 2 blockers · 5 majors · 17 minors. Verdict: REVISE** — the model set is complete against the contract
> (every response field, filter and export column has a column or an SQL-level derivation; every uniqueness the
> spec names is present; no SET_NULL/CASCADE touches an append-only table), but the two blockers make the
> migrations fail on the first `manage.py check`, and the majors break the online-purchase path, a CI test on
> PostgreSQL 18, the refund dead-letter path, a layer boundary and a contract-visible event rule.

What was verified and found **correct** (so the lead does not re-check it): the seven-row №10248 history vector
(§7.8 of 03) equals contract §4 key for key; Malika's 15-row ledger chain (3 610 → … → 48 600 → 0 → 5 680),
her totals (store 9 940 000 / online 3 640 000 / lifetime 13 580 000), `progress 0.679`, `remaining 6 420 000`,
№10248 (808 000 + 25 000 − 48 600 = 784 400, base 568 000 → 5 680) and №10231 (554 000 − 20 000 = 534 000, base
314 000 → 3 140) all add up; status-count allocation gives 6/12/9/213/12 = 252; all seven spec uniques exist
(`(provider, provider_txn_id)`, `Purchase.billz_sale_id`, `Purchase.order` 1:1, `LedgerEntry.reverses` 1:1,
`WebhookInbox (provider, event_key)`, one active `Payment` per order, one active `CashbackHold` per order); every
AO model (CashbackPolicy, CashbackTier, LedgerEntry, WebhookInbox, OrderEvent, ShipmentEvent, AppointmentEvent)
has only PROTECT FKs in both directions, no mutable column and a trigger migration; the Order state machine covers
`cancelled` from `handed_to_courier` (O1), `payment_failed`, `refund_failed` with `state unknown`, `reservation_expired`
and the BTS `handed_to_courier`/`delivered` rows; the non-deterministic ICU collation is never used in `LIKE`,
equality, `UNIQUE` or `db_collation`; Django emits `GENERATED ALWAYS AS (…) STORED` for `db_persist=True`, so the
PG 18 `VIRTUAL` default does not bite the `search_text` column.

---

## Blockers

### 1. `django.contrib.postgres` is excluded from `INSTALLED_APPS`, but `GinIndex`, `ExclusionConstraint` and `DateTimeRangeField` all fail system check `postgres.E005` without it
- **Files/headings:** 01 §7 ("`django.contrib.postgres` is **not** added"), 01 §6.1 bullet 2 ("does not need to be in
  `INSTALLED_APPS` … or for `GinIndex`"), 01 §1.1 `Customer` and §4.2 `Product` (two `GinIndex`), 03 §2.1
  `Appointment` + open point A-1 ("imported … without adding the app").
- **Problem:** In Django 6.1.2 `PostgresIndex` (base of `GinIndex`), `ExclusionConstraint` and `RangeField` (base of
  the `DateTimeRangeField` used as `TsTzRange.output_field`) all mix in `CheckPostgresInstalledMixin`
  (`django/contrib/postgres/utils.py`), whose `check()` returns
  `Error("'django.contrib.postgres' must be in INSTALLED_APPS in order to use GinIndex.", id="postgres.E005")`.
  `manage.py check`, `migrate`, `makemigrations --check` and the dev-loop gate therefore all fail on the very first
  B2 migration. Both documents assert the opposite; A-1's `RunSQL` fallback would only cover the exclusion
  constraint, not the two trigram indexes.
- **Fix:** add `"django.contrib.postgres"` to `INSTALLED_APPS` in `config/settings/base.py` (after
  `"django.contrib.staticfiles"`, before `"rest_framework"`), delete the three contrary sentences, close A-1 as
  "resolved by installing the app", and keep the ORM `ExclusionConstraint`. Cost is one `post_migrate` handler
  (`register_type_handlers`) and a few extra lookups registered on `CharField`/`TextField`; nothing else changes.
  The `CreateExtension` operations (`TrigramExtension`, `BtreeGistExtension`) do not carry the check either way.

### 2. `GinIndex(Lower("name"), …, opclasses=["gin_trgm_ops"])` raises `ValueError` at import time
- **File/heading:** 01 §1.1 `Customer.Meta.indexes` (and the same wording in §7 row `accounts.0004`).
- **Problem:** `django/db/models/indexes.py:52`: *"Index.opclasses cannot be used with expressions. Use
  django.contrib.postgres.indexes.OpClass() instead."* — raised in `Index.__init__`, i.e. while `accounts.models`
  is imported, so the whole app registry fails to load. The block also uses `Lower` without importing it.
- **Fix (field definition):**
  ```python
  from django.contrib.postgres.indexes import GinIndex, OpClass
  from django.db.models.functions import Lower
  ...
  indexes = [
      GinIndex(OpClass(Lower("name"), name="gin_trgm_ops"), name="accounts_customer_name_trgm"),
      models.Index(fields=["status", "registered_at"], name="accounts_customer_status_reg"),
  ]
  ```
  Emitted SQL: `CREATE INDEX "accounts_customer_name_trgm" ON "accounts_customer" USING gin (LOWER("name") gin_trgm_ops);`
  (the `Product.search_text` index is a plain field index with `opclasses=`, which is valid and stays as written).

---

## Majors

### 3. `purchase_credit_link` CHECK makes every credited purchase fail at `INSERT`
- **File/heading:** 02 §1.6 `Purchase` (constraint `purchase_credit_link`) and "Creation flows (services)".
- **Problem:** the CHECK requires `credited_entry_id IS NOT NULL` whenever `cashback_amount > 0`. The documented flow
  inserts the `Purchase` with the engine result (`cashback_amount = credit`) **first** — it must, because
  `LedgerEntry.purchase` (NOT NULL for `billz_sale`, and set for online credits) needs the purchase id and the entry is
  append-only — and sets `credited_entry` in a second `save(update_fields=…)`. PostgreSQL CHECK constraints are
  evaluated per statement and cannot be deferred, so the first `INSERT` fails for every purchase with a credit
  (both `record_online_purchase` and `record_store_purchase`).
- **Fix:** either (a) specify the write order explicitly: insert the row with `cashback_amount=0,
  credited_entry=None`, call `post_entry(... purchase=purchase ...)`, then
  `purchase.save(update_fields=["cashback_amount", "credited_entry", "updated_at"])` setting both together (the CHECK
  then holds at every statement); or (b) relax the constraint to what the DB can actually guarantee:
  ```python
  models.CheckConstraint(
      condition=models.Q(cashback_amount__gt=0)
      | models.Q(cashback_amount=0, credited_entry__isnull=True, reversal_entry__isnull=True),
      name="purchase_credit_link",
  )
  ```
  and leave `cashback_amount == credited_entry.amount` to reconciliation check C5 (already written). (a) is preferred;
  write it into §1.6 "Creation flows" as a numbered sequence.

### 4. The "a `LIKE` under `glowderma_ci` raises `ProgrammingError`" test is red on PostgreSQL 18 (CI/prod)
- **File/heading:** 01 §6.6 last bullet ("a `LIKE` under the collation raises `ProgrammingError`") and §8.
- **Problem:** 01 §6.3 limit 1 itself records that PG 18 allows `LIKE` with non-deterministic collations (correct:
  PG 18 lifted the restriction for `LIKE`/`ILIKE`; regex/`SIMILAR TO` remain refused). CI runs `postgres:18-trixie`
  (AGENTS.md), so a test asserting the PG 16 behaviour fails the gate on every CI run while passing locally.
- **Fix:** replace the test with one that is version-independent — assert the *rule*, not the server error: a
  grep-style test that no `Collate(…, "glowderma_ci")` appears outside `ORDER BY` annotations in
  `catalog/selectors.py`, plus (optional) the raw-SQL test guarded by
  `if connection.pg_version < 180000: with pytest.raises(ProgrammingError): …` and
  `else: assert cursor.execute("SELECT 'a' COLLATE glowderma_ci LIKE 'A'") works`. Note the same version split in
  §6.3 limit 1 ("reproduced on 16; allowed on 18 — never relied on").

### 5. Dead-letter hook marks a never-submitted refund `unknown`, which violates `refund_submitted_at` and the state's meaning
- **Files/headings:** 03 §5 "Dead letters" + X-1 ("for `refund.submit` it marks the refund `unknown`"), 02 §2.2 `Refund`
  (CHECK `refund_submitted_at`: `state IN ('submitting','unknown') → submitted_at IS NOT NULL`; transition table).
- **Problem:** 02 says the `refund.submit` handler raises (→ outbox retry) **only on transport errors before
  submission** and resolves to `succeeded/failed/unknown` itself after submission. A message can therefore only go
  `dead` while the refund is still `requested` and `submitted_at IS NULL`; the hook's `UPDATE … state='unknown'`
  then fails the CHECK (or must forge `submitted_at`). Semantically the money never reached the provider, so
  `unknown` is also wrong.
- **Fix:** in 03 §5 and X-1: "for `refund.submit` the dead-letter hook calls `payments.services.mark_failed(refund,
  error_code='dead_letter', last_error=message.last_error)` and the orchestrator writes
  `refund_failed {state: 'failed', error_code: 'dead_letter'}`"; `failed` is in `refunds_needing_action`, so staff
  still see it, and a retry is a new `Refund(origin=manual)` as 02 already designs. Add the row
  `requested → failed (dead letter)` to 02 §2.2's transition table.

### 6. `catalog.selectors.available_quantity()` reads `commerce.StockReservation` — a lower layer reading an upper layer's table, and a duplicate of 03 §1.6
- **Files/headings:** 01 §4.3 `ProductStock` invariant 2 ("`available = quantity − Σ active reservations` is computed
  by `catalog.selectors.available_quantity()` for checkout"); 03 §1.6 "Available stock
  (`commerce.selectors.available_stock`)".
- **Problem:** `catalog` sits below `commerce` (plan §4.1); a catalog selector that knows about
  `commerce_stockreservation` (and 03's refinement: converted reservations after `synced_at`) is the layering
  violation the brief asks to hunt, even if the ORM reverse relation avoids a Python import. The two documents also
  give two different formulas for the same number.
- **Fix:** delete the sentence in 01 §4.3 and replace it with "available quantity for checkout/cart is
  `commerce.selectors.available_stock(product_ids)` (03 §1.6); `catalog` exposes only `ProductStock.quantity`";
  keep 03 §1.6 as the single definition. Same pattern, lower severity, in 02 §3.1 `delivery.selectors.bts_failed_count`
  joining `commerce_order` — move that count into `reporting.selectors` composing
  `commerce.selectors` + `delivery.selectors` (or state explicitly that `Shipment.objects.filter(state="failed",
  order__status="processing")` is the accepted reverse-FK read), see minor 15.

### 7. `payment_failed` event rule differs between 02 and 03 (contract-visible history row)
- **Files/headings:** 02 §2.1 `Payment` state machine row `pending → failed` ("reservation expiry, admin cancel from
  `awaiting_payment`, provider switch (`abandoned`) … **event `payment_failed`**"), 02 §1.10 table; 03 §1.5 open
  point C-8 and §4.1 rows 3/4/16 (no `payment_failed` event on expiry/admin cancel; `payment_failed` only for
  provider-reported failures).
- **Problem:** contract §4 defines `payment_failed` as a provider failure row ("To‘lov o‘tmadi" / "… to‘lov rad
  etildi") and §2.2 lists the effects of an `awaiting_payment` cancel as events `cancelled` (+ `refund_requested`)
  only; `reservation_expired` carries `payment_failed: bool` for exactly this reason. Implementing 02's version adds
  a "To‘lov o‘tmadi" row to every expired or admin-cancelled unpaid order. 03's reading is the contract's.
  03 row 3 is itself ambiguous: it calls `fail_payment("expired")` "if a pending payment exists" and in the same cell
  says Payme state-1 transactions "are left for Payme to cancel".
- **Fix:** in 02 §2.1 change the `pending → failed` row's effects to "`failed_at`, `error_code`; event
  `payment_failed` **only when the trigger is a provider callback** (Click `error < 0`, Payme CancelTransaction on
  state 1); expiry/admin cancel/provider switch write no event (03 C-8)". In 03 §4.1 row 3 state the rule once:
  "`fail_payment('expired')` for a pending payment whose `provider_state.state != 1`; a Payme state-1 transaction
  stays `pending` and is failed by the inbound `CancelTransaction`; `reservation_expired.data.payment_failed` is true
  iff a payment was failed here". Apply the same wording to row 4 (admin cancel), where contract §2.2 says "a
  `pending` payment becomes `failed`" without the Payme caveat — decide and record it under P-2.

---

## Minors

### 8. `Banner.image_width/image_height` are NOT NULL but the two-step create inserts the row with `image=""`
- **File/heading:** 01 §4.5 `Banner` (fields + invariant "insert the row with `image=""`, then `banner.image.save(…)`").
- **Problem:** `ImageField.update_dimension_fields` leaves the dimension fields untouched when there is no file, so
  the first `INSERT` violates NOT NULL.
- **Fix:** state that `create_banner()` sets `image_width/height` from the `core.images.process_image()` result
  before the first insert (it already has the decoded image), or declare both `null=True`. Prefer the former.

### 9. `integrations.0002_webhookinbox_append_only` depends on `core.0002` in 01 but on `core.0008` in 03
- **Files/headings:** 01 §7 row `integrations 0002`; 03 §9 row 5 and S-4.
- **Problem:** with 01's dependency the trigger migration can run before `core.0008` (the GUC-aware function); the
  ordering only works if `core/db.py` already emits the GUC body (S-4). Two documents, two dependency lists.
- **Fix:** 01 §7: `depends on integrations.0001, core.0008`; add the sentence "S-4 (`core/db.py` body change) lands
  in the same commit as `core.0008`, before any app trigger migration is generated".

### 10. `SmsCode.sent_at` is used by 03 but does not exist in 01
- **Files/headings:** 03 §5 `sms.send` guard ("`SmsCode.sent_at` set in the handler's own transaction"); 01 §1.4
  `SmsCode` (has `provider_message_id` but no `sent_at`).
- **Fix:** add `sent_at = models.DateTimeField(null=True)  # claimed by the sms.send handler before the Eskiz call`
  to 01 §1.4 and mention it in the "Issue" invariant.

### 11. Business-time column named `created_at` on `LedgerEntry` but `at` on every other append-only event table
- **Files/headings:** 02 §0.1 ("AO models have `created_at`"), §1.4 `LedgerEntry.created_at  # API "at"`; 03 §0.1 ("AO
  models have `at`").
- **Problem:** same concept, two names; the contract field is `at` for LedgerEntry/OrderEvent/Purchase and
  `created_at` only for PolicyVersion. `CashbackTier.created_at` is fine (never rendered).
- **Fix:** rename `LedgerEntry.created_at` → `at` (update index `ledger_account_at_idx`, the reconciliation SQL
  C2/C3 and §4.4), keep `CashbackPolicy.created_at` (contract name). Alternatively keep `created_at` and change 03
  §0.1's convention sentence — but pick one and say so in both §0.1s.

### 12. Lower-layer services typed on `commerce.Order` instances will need `commerce` imports
- **Files/headings:** 02 §1.5 `create_hold(order, amount)`, `capture_hold(order)`, `release_hold(order, reason)`,
  §1.6 `record_online_purchase(order, …)`, §1.10 `reverse_purchase(order)`, §3.1 `ensure_shipment(order)`,
  `request_bts_create(order, manual)`; 03 §0.3 repeats them.
- **Problem:** mypy (`django-stubs`, strict in the gate) wants a type for `order`; the natural annotation is
  `from glowderma.commerce.models import Order` under `TYPE_CHECKING`, which import-linter still counts as an
  import of a higher layer (unless `exclude_type_checking_imports` is set, which the plan does not).
- **Fix:** specify scalar signatures: `create_hold(*, customer_id, order_id, amount, expires_at)`,
  `capture_hold(order_id)`, `release_hold(order_id, reason)`, `record_online_purchase(*, customer_id, order_id,
  items_subtotal, promo_subtotal, cashback_used, at, billz_order_uuid=None)`, `reverse_purchase(order_id)`,
  `ensure_shipment(order_id)`, `request_bts_create(order_id, *, manual, actor_id)`; the orchestrator passes the
  locked order's values. (Also merges the `record_online_purchase` signature difference between 02 §1.6
  `(order, policy=None)` and 03 S-1 `(order, billz_order_uuid=None)`.)

### 13. `Shipment.tariff` may be NULL in state `created`, but the contract's `created` shape has `tariff: int`
- **File/heading:** 02 §3.1 `Shipment` (`tariff = BigIntegerField(null=True)`, CHECK `shipment_state_fields`).
- **Fix:** `mark_created` sets `tariff = response.tariff if response.tariff is not None else order.delivery_fee`
  and the `created`/`cancelled` branches of `shipment_state_fields` gain `tariff__isnull=False`
  (SQL: `… AND tariff IS NOT NULL`). Record the fallback in D-2's neighbourhood.

### 14. `Order.cashback_hold` renders captured/released holds; the contract example for №10248 shows `null`
- **Files/headings:** 02 §1.5 "Contract fields served" ("the order's newest hold by `id`, or `null`"); contract §2.2
  example (`"cashback_hold": null` for an order with `cashback_used 48600`, whose hold is `captured`).
- **Problem:** not a test vector, but the seeded №10248 will return `{amount 48600, state "captured", …}` where the
  example says `null`; the FE only uses `state == "active"`.
- **Fix:** decide and write it down in 02 §1.5: either render only `active` holds (`null` otherwise — matches the
  example and the FE's use) or keep the newest hold and add a contract §7 delta note "example is illustrative;
  `cashback_hold` is non-null for every order that used cashback". Reviewer's preference: render only `active`.

### 15. `delivery.selectors.bts_failed_count` and `payments.selectors.payments_list` read upper-layer tables
- **Files/headings:** 02 §3.1 "Contract fields served" (`JOIN commerce_order o … WHERE o.status = 'processing'`),
  02 §2.1 `payments_list` (`customer.phone` through `order__customer__phone`).
- **Problem:** same family as major 6, lower severity because both go through the module's own FK
  (`Shipment.order`, `Payment.order`) rather than a reverse relation from a foreign table.
- **Fix:** write the rule once in 03 §0.1/§6: "a lower module may traverse its *own* FK upward in a selector
  (`order__status`, `order__customer__phone`) but never a reverse relation into an upper table"; under that rule
  both selectors are allowed and major 6's `catalog` case is not. Move `bts_failed_count` to `reporting` only if
  the lead rejects that rule.

### 16. `CashbackPolicy` CHECK `effective_from > created_at` can turn a valid request into a 500
- **File/heading:** 02 §1.1 `CashbackPolicy` (constraint `policy_effective_after_created`).
- **Problem:** the serializer checks `effective_from > now` at validation time; `created_at` is stamped at insert
  time a few milliseconds later. A request with `effective_from = now + 1 s` that spends >1 s under the advisory
  lock fails the CHECK with `IntegrityError` instead of `effective_from_in_past`.
- **Fix:** in `create_policy_version` evaluate `now = timeutils.now()` once, use it both for the validation and as
  the explicit `created_at` of the insert (so the CHECK is equivalent to the validation), and map a residual
  `IntegrityError` on this constraint to the `effective_from_in_past` field error.

### 17. Seed arithmetic and seed-row consistency
- **File/heading:** 03 §7.8 "Generated orders: 240 rows", §7.12 campaigns 1–3, §7.1 `--keep-open`.
- **Problems:** (a) `10001–10242` minus `10198`, `10231` and Malika's ten pre-policy ids is **230** generated orders,
  not 240 (the per-tab totals 6/12/9/213/12 = 252 still hold because Malika's ten are delivered). (b) Campaigns 1–3
  are "inserted directly in state `sent`" — `campaign_state_fields` requires `started_at`, `sent_at`, `sent` **and
  `failed`** NOT NULL; the table gives only `recipient_count/sent/delivered`; state `failed = 0`, `started_at =
  sent_at − 2 min`. (c) `--keep-open` moves `Order.reservation_expires_at` into the future; 02 §1.5/§1.6 define
  `CashbackHold.expires_at` and `StockReservation.expires_at` as "= `Order.reservation_expires_at`", so the seeder
  must move those too or K-style consistency checks and `hold_active_expires_idx` scans disagree with the order.
- **Fix:** correct the count; add `failed 0` and `started_at` to the campaign table; add "keep-open also rewrites
  `StockReservation.expires_at` and `CashbackHold.expires_at` of the same order" to §7.1.

### 18. G-2 reads as a unilateral deviation although plan §4.3 already approves it; two contract examples still disagree
- **Files/headings:** 02 §4.5 item 1 / G-2, 03 G-2; contract §2.7 example (`earn_on_cashback_paid: false`), §6
  "the cashback-paid part does not earn", audit example §2.13 (`balance_after 51550`).
- **Problem:** plan §4.3 says the seeder reproduces "the 'keep as drawn' 568 000 base via a policy with
  `earn_on_cashback_paid` as seed data", i.e. the decision is taken; the documents present it as open. The audit
  example's `balance_after 51550` (= 48 600 + 2 950) is also unreproducible with the seed's chain (63 150 on
  2026-10-12) and is missing from §7.10.
- **Fix:** in 02 §4.5 and 03 §11 cite plan §4.3 and mark G-2 "decided (plan §4.3); the §2.7 example and §6 bullet
  describe the *default* for new versions, v3 is seed data"; add a contract §7 delta line; add row 17 to 03 §7.10
  for the audit example.

### 19. `online_orders.last_order_*` and `online_orders.count` use different base sets
- **File/heading:** 03 §6.3 (`last_order_*`: newest `commerce_order` **any status**; `count`: non-reversed online
  purchases, 02 §4.1).
- **Problem:** for a customer whose newest order is `awaiting_payment` or `cancelled`, `last_order_id` names an
  order that `count` excludes. Malika's vector is unaffected (№10248 is paid).
- **Fix:** state the intended semantics in 03 §6.3 ("`last_order_*` = the newest order of any status — the card's
  'last activity'; `count` = paid, non-reversed") and add it as a contract §2.6 clarification; or restrict
  `last_order_*` to `status IN ('processing','handed_to_courier','delivered')`.

### 20. No Django-admin plan for g1/g2 models; sensitive columns must be excluded
- **Files/headings:** 03 §8 ("plus the g1/g2 models by their own documents") — 01 and 02 have no admin section.
- **Problem:** AGENTS.md: "never expose `password` or `refresh_hash`"; 01 adds `Customer.password`,
  `SmsCode.code_hmac/verification_token_hash`, `Device.fcm_token`; 02 adds `PosDevice.key_hash`.
- **Fix:** add a short §"Django admin" to 01 and 02 listing `list_display`/`exclude`, with
  `exclude = ("password",)` on `Customer`, `("code_hmac", "verification_token_hash")` on `SmsCode`, `("fcm_token",)`
  on `Device`, `("key_hash",)` on `PosDevice`, and `("previous_refresh_hash",)` added to the existing `AuthSession`
  admin.

### 21. `commerce.seed` writes `accounts.Customer.address_text` directly
- **File/heading:** 03 §7.2 ("`address_text` = the address of their first delivery order (set by `commerce.seed`)")
  and §7.8.
- **Problem:** cross-module write outside the owning module's services (AGENTS.md architecture rule), even in a
  seeder.
- **Fix:** have `commerce.seed` call `accounts.services.set_address_text(customer_id, text)` (or create the
  `Address` row through `accounts.services.add_address`, which already recomputes `address_text` per 01 §1.3).

### 22. `on_delete` for staff-actor FKs follows three different conventions
- **Files/headings:** 01 (`SET_NULL`: `Product.last_change_actor`, `ProductImage.uploaded_by`,
  `SiteSettings.updated_by`, `WorkingHoursOverride/BlockedPeriod.created_by`; `PROTECT`: `Job.created_by`);
  02 (`PROTECT` everywhere); 03 §0.1 ("actor FKs on mutable rows are `PROTECT` … except notifications configuration
  rows … as in `01-…`" — which is not what 01 does).
- **Fix:** one sentence in all three §0: "actor FKs on append-only rows: `PROTECT` (required); on mutable rows:
  `SET_NULL` where the skeleton says so (`last_change_actor`, `StatusTemplate.updated_by`), `PROTECT` elsewhere" and
  align `Job.created_by` or the 01 `created_by`s to it (staff rows are never deleted, so this is documentation only).

### 23. `Device.platform` / four non-contract enums vs nested `TextChoices` convention
- **Files/headings:** 01 §9 (`DevicePlatform`, `SmsPurpose`, `WebhookProvider`, `IntegrationProvider` proposed for
  `core/enums.py`); 02 §0.1 and 03 §0.1 (internal states are *nested* `TextChoices` on the model).
- **Problem:** two conventions for the same thing; `core/enums.py`'s docstring reserves it for contract enums whose
  names feed `ENUM_NAME_OVERRIDES`.
- **Fix:** nest the four in their models (`Device.Platform`, `SmsCode.Purpose`, `WebhookInbox.Provider`,
  `IntegrationState.Provider`), or reuse `enums.OrderChannel` for `Device.platform` as 01 offers; keep `enums.py`
  contract-only.

### 24. `WorkingHours.break_*` rendered with `label: null` relies on an undocumented FE mapping
- **File/heading:** 01 §5.5 "Contract fields served" ("the FE maps a null label of kind `off` to 'Ish vaqti emas'").
- **Problem:** correct per contract §5.6 (`label ?? (kind === "busy" ? "Band qilingan" : "Ish vaqti emas")`), but the
  seeded Jasur row in 03 §7.6 says "renders as the `off` block with `label null` → FE 'Ish vaqti emas'" while the
  contract example for Jasur is `{"start": "13:00", "end": "14:00", "kind": "off", "label": null}` — consistent; just
  cite §5.6 so the reader does not look for a backend label.
- **Fix:** add "(contract §5.6)" after the sentence in 01 §5.5 and in 03 §7.6.

---

## Hunt-list results (explicitly checked, nothing further found)
- **Contract fields with no column/derivation:** none. Every key of `OrderSummary`, `Order`, `Refund`, `Product`,
  `Category`, `Banner`, `CustomerListItem`, `CustomerCard`, `LedgerEntry`, `Purchase`, `PolicyVersion`,
  `NotificationRecord`, `Segment`, day-schedule items, `dashboard/`, `nav-counts/`, `settings/`, `payments/`,
  `jobs/{id}/`; every filter of §2.2/§2.3/§2.6/§2.8/§2.12; every export column of §1.8 (orders, products,
  customers, payments, notifications, segments/members, categories, banners) has a source. The derived values
  (`pending`, `available`, `tier.next.progress`, `state`/`effective_to`/`superseded_by`, `revenue` per bucket,
  `product_count`, `member_count`/`size`, `display payment`) are given at SQL level.
- **AO models with mutable columns / SET_NULL FKs:** none (see header). **Cascades into AO tables:** none —
  `OrderItem.order`, `StockReservation.order`, `CartItem.*`, `Device.customer`, `ProfessionalService.professional`,
  `CampaignRecipient.*`, `PurchaseItem.purchase` all cascade only into mutable tables, and every parent is itself
  PROTECTed by an AO child.
- **Uniqueness from the spec:** all seven present (header). **Inconsistent names between files:** items 10, 11, 12,
  22, 23 above; all other cross-references (`order_payment`, `revenue_buckets`, `refunds_needing_action`,
  `ensure_shipment/request_bts_create/mark_created/mark_failed/apply_status/cancel`, `customer_totals`,
  `account_summary`, `tier_for`, `segment_members_q`, `Shipment.State.CANCELLED` rendered `created`, `ReleaseReason`
  literals) match.
- **PG 16 vs 18:** item 4 (LIKE under non-deterministic collations) is the only behavioural difference touched;
  `GENERATED … STORED`, `make_interval(mins =>)`, `COUNT(*) FILTER`, `pg_sequences.start_value`,
  `current_setting(name, true)`, trusted `pg_trgm`/`btree_gist`, ICU collations and `CREATE EXTENSION IF NOT EXISTS`
  behave the same on both.
- **Layering:** items 6, 12, 15 (and 21 for the seeder); outbox handlers are all placed in the orchestrating
  module's `tasks.py`; `customers`/`reporting` own no tables and only compose lower selectors.
- **State machines:** complete against the contract (header); item 5 adds the one missing refund row; item 7 fixes
  the one contract-visible disagreement.
- **Seed vs contract vectors:** №10248 history, `Order.cashback`, Malika's ledger/totals/tier and the appointment
  day all reproduce; the deliberate non-reproductions (§7.10) are justified; items 17 and 18 are the only slips.
