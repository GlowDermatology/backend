# M1 models — group g2-financial: `loyalty`, `payments`, `delivery`

> Status: design document for slice B2 (field-level expansion of the approved skeleton, plan §5.3 and §4). It is a
> **document**: no code, no migrations, no commits are produced by it. Sibling documents cover the other groups
> (`01-…` accounts/catalog, `03-…` commerce, …). Where this file refers to models of other groups it states the
> assumption it relies on (section 0.3) so the reviewers can reconcile.

Sources: `AGENTS.md`, `docs/api/admin-api-v1.md` (the contract; every field, filter, export column, enum and error
rule cited below is a requirement on these models), `docs/specs/2026-10-24-backend-foundation-design.md`
("Financial integrity and the settle-now foundations", "Provisional business rules", "M1 admin endpoint inventory",
"M2 — Customer (mobile) API"), `docs/decisions/open-questions.md` (L1–L8, O1–O8, A1), the approved plan §4.1–4.3 and
§5.3, `glowderma/core/{models,db,money,timeutils,outbox,tasks}.py`, `glowderma/accounts/models.py`, and the
frontend domain rules in `frontend/src/domain/cashback.ts` + `cashback.test.ts` (the FE preview must equal the
backend engine).

---

## 0. Conventions and cross-group interface

### 0.1 Hard conventions applied to every model below
- Primary keys: `BigAutoField` (project default `DEFAULT_AUTO_FIELD`).
- Money: `BigIntegerField`, integer so'm, never tiyin (Payme ×100 exists only inside its adapter).
- Percent: `DecimalField(max_digits=5, decimal_places=2)`; cashback math in integer basis points via
  `core.money.percent_to_bp` / `apply_bp` (`amount * bp // 10_000`).
- FKs by string reference `"app.Model"`. FKs **to or from** append-only rows: `on_delete=PROTECT` (a `SET_NULL`
  would be an `UPDATE` the trigger rejects). FKs between mutable rows: `PROTECT` unless a cascade is explicitly
  wanted (only `PurchaseItem.purchase` cascades). Actor FKs to `accounts.StaffUser` (one rule for all three
  documents, `01-…` §0): `PROTECT` on append-only rows and on business rows (`Refund.requested_by`,
  `PosDevice.created_by`, `CashbackPolicy.author`); `SET_NULL` only on configuration/content rows (none in this group).
- **Lower-layer services take scalar ids and values** (`order_id`, `customer_id`, amounts, timestamps), never a
  `commerce.Order` instance: mypy (`django-stubs`, strict) would need `from glowderma.commerce.models import Order`
  under `TYPE_CHECKING`, which import-linter still counts as an upward import. The orchestrator passes the locked
  order's values (signatures in 1.5, 1.6, 1.10, 3.1).
- **Layer reads**: a lower module's selector may traverse its *own* FK upward (`Payment.order__status`,
  `Shipment.order__customer__phone`, `order__status='processing'` in `bts_failed_count`) but never a reverse relation
  into an upper module's table. Under this rule `payments_list` (2.1) and `bts_failed_count` (3.1) are allowed;
  a `catalog` selector over `commerce_stockreservation` is not (`01-…` §4.3 was corrected accordingly).
- Append-only (`AO`) models subclass `AppendOnlyModel` **and** get a `RunSQL(*core.db.append_only_trigger_sql(table))`
  migration. Event-like AO rows (`LedgerEntry`, `ShipmentEvent`; `OrderEvent`/`AppointmentEvent` in `03-…`) carry
  the business time as **`at`** (`DateTimeField(default=timestamp_default, editable=False)`, the contract's field
  name); `CashbackPolicy`/`CashbackTier` keep `created_at` because `PolicyVersion.created_at` is the contract name.
  No AO row has `updated_at`.
- Time-stamped (`TS`) models subclass `TimeStampedModel` (`created_at`, `updated_at`; `save()` refreshes `updated_at`).
- `DateTimeField` defaults use `core.models.timestamp_default` (never `timezone.now` directly, never `auto_now*`).
- `CharField` is never `null=True`; "absent" is `""` and the serializer renders `null` where the contract says so.
- Choices come from `glowderma/core/enums.py` `TextChoices` with class names equal to the contract enum names
  (`enums.PaymentStatus.choices`, …). Where a column needs an *internal* state set that is not a contract enum
  (Shipment record state, hold release reason, refund origin, shipment-event kind) the model declares a nested
  `TextChoices` and the serializer maps it to the contract enum; those are marked **internal** below.
- Every model has `__str__`.
- Constraint and index names are ≤ 63 characters (PostgreSQL identifier limit) and unique per app.
- `CheckConstraint(condition=…)` (Django 6 keyword), `UniqueConstraint(condition=…)` for partial uniques. The exact
  SQL that PostgreSQL ends up with is given next to every constraint so the review can check it independently of
  the ORM rendering.
- i18n text columns: none of the models in this group carry user-facing i18n text.

### 0.2 Append-only tables of this group and their trigger migrations
| Table | Migration |
|---|---|
| `loyalty_cashbackpolicy` | `loyalty/migrations/0002_append_only.py` (`RunSQL` ×3 in one migration) |
| `loyalty_cashbacktier` | same migration |
| `loyalty_ledgerentry` | same migration |
| `delivery_shipmentevent` | `delivery/migrations/0002_append_only.py` |

The forward SQL re-creates the shared `glowderma_forbid_mutation()` function (`CREATE OR REPLACE`, idempotent across
apps) and adds `BEFORE UPDATE OR DELETE` triggers; the reverse drops only the triggers. Tests per table: ORM
`save()` of a loaded row, `QuerySet.update()`, `QuerySet.delete()`, `bulk_update()` raise `AppendOnlyError`; raw
`UPDATE`/`DELETE` raise `append-only table …` (PG 16 and 18).

### 0.3 Models of other groups this group depends on (assumptions to reconcile)
| Referenced | What this group relies on |
|---|---|
| `accounts.Customer` | `id`, `phone`, `name` (`""` when deleted), `status active\|deleted`, `language`, `registered_at`. Rows are never physically deleted (A1: delete scrubs and keeps the row; re-registration reactivates it), so `PROTECT` FKs never block. |
| `accounts.StaffUser` | exists (M0); actor FKs. |
| `commerce.Order` | `id` is the order number (sequence from 10001); `customer_id`; `fulfilment delivery\|pickup`; `status` (OrderStatus); `items_subtotal`, `promo_subtotal`, `delivery_fee`, `cashback_used`, `total = items_subtotal + delivery_fee − cashback_used`; `payment_provider`; `reservation_expires_at`; `paid_at`. `commerce` has **no FK** to `loyalty`, `payments` or `delivery` (holds, purchases, payments and shipments point at the order, never the reverse). |
| `commerce.OrderEvent` | written by `commerce.services` only; this group never inserts order events itself — it returns the data the orchestrator puts into `cashback_credited`, `billz_sale_recorded`, `shipment_created`, `shipment_failed`, `refund_*` events. |
| `core.OutboxMessage` | `topic`, `payload` JSON, `dedupe_key` unique, `status pending\|in_flight\|done\|failed\|dead`, `attempts`. `delivery.Shipment` holds a nullable FK to it (section 3.1). |
| `integrations.WebhookInbox` | immutable raw provider payloads `(provider, event_key)`; this is why `Payment` keeps *protocol state* only and no `raw_*` columns (section 2.1, open points). |

**Migration dependency note.** The plan §5.3 lists the migration order "… loyalty → payments → delivery → commerce".
The FK graph forces the opposite: `loyalty`, `payments` and `delivery` all reference `commerce.Order`, and `commerce`
references only `accounts` and `catalog`. `makemigrations` will therefore emit `commerce.0001` first and
`loyalty/payments/delivery.0001` with a dependency on it. Layering (import-linter) is unaffected: string FKs do not
import. Recorded as open point G-1.

---

## 1. `loyalty`

### 1.0 Module overview
```
loyalty/
  models.py      CashbackPolicy (AO), CashbackTier (AO), LoyaltyAccount (TS), LedgerEntry (AO), CashbackHold (TS),
                 Purchase (TS), PurchaseItem, PosDevice (TS)
  engine.py      pure functions: rate_for, eligible_amount, credit_for_purchase (integer bp)
  services.py    post_entry, get_or_create_account, create_hold, capture_hold, release_hold, record_online_purchase,
                 record_store_purchase, link_billz_sale, reverse_order_entries, adjust, create_policy_version
  selectors.py   policy_versions (derived state), active_policy_at, customer_totals, tier_for, ledger_for_customer,
                 ledger_for_order, available_balance, purchases_for_customer
  tasks.py       loyalty.reconcile_balances (nightly beat)
```
Rule reminders: `LoyaltyAccount.balance` changes **only** in `loyalty.services.post_entry()`; every write to this
module from `commerce`/`customers` goes through `loyalty.services`; reads through `loyalty.selectors`.

### 1.1 `CashbackPolicy` (append-only, no mutable columns)
One immutable version of the cashback rules; `state`, `effective_to` and `superseded_by` are **derived** from time
and from later rows, never stored.

```python
class CashbackPolicy(AppendOnlyModel):
    version = models.PositiveIntegerField(unique=True)
    effective_from = models.DateTimeField()
    basis = models.CharField(max_length=16, choices=enums.RateBasis.choices)
    earn_on_cashback_paid = models.BooleanField()
    author = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="cashback_policies"
    )  # null = system / seed
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["-version"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(effective_from__gt=models.F("created_at")),
                name="policy_effective_after_created",
            ),
        ]
        indexes = [models.Index(fields=["effective_from"], name="policy_effective_from_idx")]

    def __str__(self) -> str:
        return f"Cashback policy v{self.version} ({self.basis}, from {self.effective_from:%Y-%m-%d})"
```

Exact SQL of the constraints:
```sql
ALTER TABLE loyalty_cashbackpolicy ADD CONSTRAINT loyalty_cashbackpolicy_version_key UNIQUE (version);
ALTER TABLE loyalty_cashbackpolicy ADD CONSTRAINT policy_effective_after_created
  CHECK (effective_from > created_at);
```

**Derived state and supersession (the only rule; implemented once in `selectors.policy_versions()`).**
- `superseded_by(v)` = the smallest `w.version` with `w.version > v.version AND w.created_at < v.effective_from`
  (a later version was created while `v` was still in the future, i.e. while `v` was `scheduled`; the contract allows
  at most one future version, so the new one replaces it). `null` if none.
- `effective_to(v)` = `MIN(n.effective_from)` over non-superseded `n` with `n.version > v.version`; `null` = open.
  (For a superseded row it is informational only; the FE renders "… dan · almashtirildi" from `state`.)
- `state(v)`:
  `superseded` if `superseded_by` is not null; else `scheduled` if `effective_from > now`; else `active` if
  `effective_to is null or effective_to > now`; else `expired`.
- Because `effective_from > created_at` is enforced and every later non-superseded `w` has
  `w.created_at ≥ v.effective_from`, `effective_from` is strictly increasing in `version` order among non-superseded
  versions; hence there is at most one `active` and at most one `scheduled` version at any instant, and the rule is
  stable over time (a version that is superseded now was never active).
- "Version active at time T" (used by the engine: *a purchase uses the version active at its confirmation time*):

```sql
-- selectors.active_policy_at(t)
SELECT p.* FROM loyalty_cashbackpolicy p
WHERE p.effective_from <= %(t)s
  AND NOT EXISTS (SELECT 1 FROM loyalty_cashbackpolicy w
                  WHERE w.version > p.version AND w.created_at < p.effective_from)
ORDER BY p.version DESC LIMIT 1;

-- selectors.policy_versions(now): every version with derived columns (feeds GET cashback/policy/)
SELECT p.*,
  (SELECT MIN(w.version) FROM loyalty_cashbackpolicy w
     WHERE w.version > p.version AND w.created_at < p.effective_from)          AS superseded_by,
  (SELECT MIN(n.effective_from) FROM loyalty_cashbackpolicy n
     WHERE n.version > p.version
       AND NOT EXISTS (SELECT 1 FROM loyalty_cashbackpolicy w2
                       WHERE w2.version > n.version AND w2.created_at < n.effective_from)) AS effective_to
FROM loyalty_cashbackpolicy p
ORDER BY p.version DESC;
-- state = CASE WHEN superseded_by IS NOT NULL THEN 'superseded'
--              WHEN effective_from > now THEN 'scheduled'
--              WHEN effective_to IS NULL OR effective_to > now THEN 'active'
--              ELSE 'expired' END   (computed in Python from the two annotations, with now = timeutils.now())
```
ORM: two `Subquery(OuterRef(...))` annotations (`superseded_by`, `effective_to`) on `CashbackPolicy.objects`; the
state is attached in Python. `GET cashback/policy/` is a single query plus one `prefetch_related("tiers")`.

**Invariants (services)**
- `create_policy_version(effective_from, basis, earn_on_cashback_paid, tiers, author)`:
  validation in contract §2.7 order (`min_items`, `empty_range`, `overlap` on the sorted list with `i` = submitted
  index, `out_of_range`, `max_decimal_places`, `effective_from_in_past`); warnings `gap` / `uncovered_above` are
  returned, not errors. Then, in one transaction: `SELECT pg_advisory_xact_lock(hashtext('loyalty.cashbackpolicy'))`
  (serialises concurrent creators; the unique `version` is the safety net), `version = COALESCE(MAX(version),0)+1`,
  insert policy + tiers, compute `superseded_version` = the previously scheduled version (if any) via the derivation
  above, audit `cashback.policy_version_created` and, when applicable, `cashback.policy_version_superseded`.
  **`now` is evaluated once** (`now = timeutils.now()` before validation) and passed as the explicit `created_at`
  of the insert, so the DB check `effective_from > created_at` is the same comparison as the
  `effective_from_in_past` validation and cannot turn a request that waited under the advisory lock into a 500; a
  residual `IntegrityError` on `policy_effective_after_created` (clock skew between workers) is mapped to the same
  field error (review minor 16).
- Nothing ever updates or deletes a policy or tier row (trigger + `AppendOnlyModel`).
- `next_effective_from` (contract) = start of the next Tashkent calendar month from `timeutils.now()`
  (`timeutils.month_start` of the next month, `+05:00`); it is a *suggestion* for the FE, the validator only requires
  `effective_from > now`.
- Seed (`seed_policy`): v1 tiers 0–20M 1 % (author null = "Tizim"), and `seed_demo` adds v2 (20–30M 2 %) and v3
  (30–50M 3 %) with `created_at`/`effective_from` matching the design periods ("1-iyun – 14-avg 2026",
  "15-avg – 30-sen 2026", "1-okt 2026 dan"). See section 4.5 for `earn_on_cashback_paid` of v3.

**Contract fields served**
- `PolicyVersion`: `version`, `state` (derived), `effective_from`, `effective_to` (derived), `basis`,
  `earn_on_cashback_paid`, `tiers[]` (1.2), `author` (`Actor` from `author`), `created_at`, `superseded_by` (derived).
- `GET cashback/policy/`: `active`, `scheduled` (at most one each, by derived state), `versions` (newest first by
  `version`), `next_effective_from`.
- `POST cashback/policy-versions/` 201: `version`, `superseded_version`, `warnings`.
- `LedgerEntry.policy_version`, `Purchase.policy_version`, `CustomerCard.tier.policy_version` = `policy.version`.
- `CustomerListItem.percent`, `CustomerCard.tier` use the active version's tiers (1.2, section 4.3).

**Open points**
- L-1 Whether `effective_from` must be a Tashkent month start (the FE only *suggests* `next_effective_from`). Default:
  any future instant is accepted, as the contract states.
- L-2 Supersession of a version that is scheduled *and* whose superseder has a later `effective_from` leaves the
  previous active version in force until the superseder starts (section 1.1 rule). Confirm this is the intended
  reading of "a new one supersedes it".

### 1.2 `CashbackTier` (append-only)
One band `[from_amount, to_amount)` of a policy version with its percent.

```python
class CashbackTier(AppendOnlyModel):
    policy = models.ForeignKey("loyalty.CashbackPolicy", on_delete=models.PROTECT, related_name="tiers")
    from_amount = models.BigIntegerField()  # inclusive, so'm
    to_amount = models.BigIntegerField(null=True)  # exclusive, so'm; null = open-ended
    percent = models.DecimalField(max_digits=5, decimal_places=2)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["policy_id", "from_amount"]
        constraints = [
            models.UniqueConstraint(fields=["policy", "from_amount"], name="tier_policy_from_unique"),
            models.CheckConstraint(condition=models.Q(from_amount__gte=0), name="tier_from_nonneg"),
            models.CheckConstraint(
                condition=models.Q(to_amount__isnull=True) | models.Q(to_amount__gt=models.F("from_amount")),
                name="tier_to_above_from",
            ),
            models.CheckConstraint(
                condition=models.Q(percent__gt=0, percent__lte=100), name="tier_percent_range"
            ),
        ]

    def __str__(self) -> str:
        upper = "∞" if self.to_amount is None else str(self.to_amount)
        return f"v{self.policy_id} [{self.from_amount}, {upper}) {self.percent}%"
```
```sql
ALTER TABLE loyalty_cashbacktier ADD CONSTRAINT tier_policy_from_unique UNIQUE (policy_id, from_amount);
ALTER TABLE loyalty_cashbacktier ADD CONSTRAINT tier_from_nonneg CHECK (from_amount >= 0);
ALTER TABLE loyalty_cashbacktier ADD CONSTRAINT tier_to_above_from
  CHECK (to_amount IS NULL OR to_amount > from_amount);
ALTER TABLE loyalty_cashbacktier ADD CONSTRAINT tier_percent_range CHECK (percent > 0 AND percent <= 100);
```

**Invariants (services)** — non-overlap of the bands of one policy (`from_i ≥ to_{i−1}` on the list sorted by
`from`, `to null` = ∞) is validated in `create_policy_version`; the DB only guarantees per-row sanity (an
`EXCLUDE USING gist (policy_id WITH =, int8range(from_amount, to_amount) WITH &&)` would need `btree_gist`, which the
plan creates in the `appointments` migration that runs later — open point L-3). Tiers are inserted sorted by `from`,
so `id` order equals `from` order within a version.

**Contract fields served** — `PolicyVersion.tiers[] {id, from, to, percent}` (percent as a 2-dp string);
`CustomerListItem.percent`; `CustomerCard.tier {percent, next {percent, from, remaining, progress}}`;
`LedgerEntry.percent`, `Order.cashback[].percent` are *snapshots* on the entry (1.4), not reads of this table.

**Open points** — L-3 (gist exclusion vs service validation; default: service validation + per-row checks).
L-4 `percent` precision above 2 dp is rejected by the serializer (`max_decimal_places`) and by `percent_to_bp`.

### 1.3 `LoyaltyAccount` (time-stamped)
The customer's cashback balance; one row per customer, created lazily by `get_or_create_account()` the first time
a hold, entry or card read needs it (and by the customer seeder).

```python
class LoyaltyAccount(TimeStampedModel):
    customer = models.OneToOneField(
        "accounts.Customer", on_delete=models.PROTECT, related_name="loyalty_account"
    )
    balance = models.BigIntegerField(default=0)  # signed; negative allowed (L5) and flagged in selectors

    def __str__(self) -> str:
        return f"Loyalty account of customer {self.customer_id}: {self.balance}"
```
No CHECK on `balance` (L5: a reversal larger than the balance is still posted; `balance` and `available` may be
negative). No extra columns: reconciliation results go to the audit log (1.9), negative balances are derived
(`balance < 0`), `updated_at` therefore means "balance last changed".

**Invariants (services)**
- `balance` changes only in `post_entry()` (1.4) under `SELECT … FOR UPDATE` of this row; every change is paired with
  exactly one `LedgerEntry` whose `balance_after` equals the new balance, in the same transaction.
- `available = balance − Σ CashbackHold.amount WHERE state = 'active'` (holds past `expires_at` still count until the
  expiry job releases them; see 1.5). Checked under the row lock for `subtract` adjustments and for hold creation.
- Deleted customers keep their account (contract §2.6: adjustments are allowed for deleted customers; A1).
- Locking helper `services._lock_account(customer_id)`: `LoyaltyAccount.objects.select_for_update(of=("self",)).get_or_create(customer_id=…)`
  (`of=("self",)` so a joined `Customer` row is not locked).

**Contract fields served** — `CustomerListItem.balance` (ordering `balance`/`-balance` ties by `id`);
`CustomerCard.balance`, `held`, `available`; adjustment 201 `balance`, `available`; export `mijozlar.Balans (so‘m)`;
audit `cashback.adjusted.balance_after`.

**Open points** — L-5 Whether the account row should be created eagerly at customer registration (M2) so that
`customers/` ordering by `balance` never has to `COALESCE` a missing account. Default: lazy + `COALESCE(balance, 0)`
in the list selector (`LEFT OUTER JOIN`).

### 1.4 `LedgerEntry` (append-only)
One immutable, signed movement of a customer's cashback balance with the balance after it.

```python
class LedgerEntry(AppendOnlyModel):
    account = models.ForeignKey("loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="entries")
    kind = models.CharField(
        max_length=16, choices=enums.LedgerKind.choices
    )  # credit|debit|adjustment|reversal
    amount = models.BigIntegerField()  # signed so'm
    balance_after = models.BigIntegerField()
    source_type = models.CharField(
        max_length=16, choices=enums.LedgerSourceType.choices
    )  # order|billz_sale|admin
    order = models.ForeignKey(
        "commerce.Order", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    purchase = models.ForeignKey(
        "loyalty.Purchase", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    source_ref = models.CharField(
        max_length=64, blank=True
    )  # order → str(order_id); billz_sale → Billz document no.
    # credit-only snapshot of the engine inputs
    base = models.BigIntegerField(null=True)  # eligible amount the percent was applied to
    percent = models.DecimalField(max_digits=5, decimal_places=2, null=True)
    promo_excluded = models.BooleanField(default=False)  # promo items were excluded from the base
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy", null=True, on_delete=models.PROTECT, related_name="ledger_entries"
    )
    # reversal-only
    reverses = models.OneToOneField("self", null=True, on_delete=models.PROTECT, related_name="reversed_by")
    # adjustment-only
    reason = models.TextField(blank=True)
    actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="ledger_adjustments"
    )
    at = models.DateTimeField(default=timestamp_default, editable=False)  # business time; API "at"

    class Meta:
        ordering = ["-at", "-id"]
        indexes = [
            models.Index(fields=["account", "at", "id"], name="ledger_account_at_idx"),
            models.Index(fields=["order"], name="ledger_order_idx"),
            models.Index(fields=["purchase"], name="ledger_purchase_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(kind="credit", amount__gt=0)
                    | models.Q(kind="debit", amount__lt=0)
                    | models.Q(kind__in=["adjustment", "reversal"]) & ~models.Q(amount=0)
                ),
                name="ledger_amount_sign",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="reversal", reverses__isnull=False)
                    | ~models.Q(kind="reversal") & models.Q(reverses__isnull=True)
                ),
                name="ledger_reversal_link",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        kind="credit",
                        base__isnull=False,
                        base__gte=0,
                        percent__isnull=False,
                        policy__isnull=False,
                    )
                    | ~models.Q(kind="credit")
                    & models.Q(
                        base__isnull=True, percent__isnull=True, policy__isnull=True, promo_excluded=False
                    )
                ),
                name="ledger_credit_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_type="admin", kind="adjustment")
                    | ~models.Q(source_type="admin") & ~models.Q(kind="adjustment")
                ),
                name="ledger_admin_is_adjustment",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_type="order", order__isnull=False) & ~models.Q(source_ref="")
                    | models.Q(source_type="billz_sale", order__isnull=True, purchase__isnull=False)
                    & ~models.Q(source_ref="")
                    | models.Q(source_type="admin", order__isnull=True, purchase__isnull=True, source_ref="")
                ),
                name="ledger_source_refs",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="adjustment", actor__isnull=False) & ~models.Q(reason="")
                    | ~models.Q(kind="adjustment") & models.Q(actor__isnull=True, reason="")
                ),
                name="ledger_adjustment_fields",
            ),
            models.CheckConstraint(
                condition=models.Q(percent__isnull=True) | models.Q(percent__gt=0, percent__lte=100),
                name="ledger_percent_range",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"Ledger #{self.pk} {self.kind} {self.amount:+} → {self.balance_after} (acct {self.account_id})"
        )
```
Exact SQL:
```sql
-- reverses_id unique comes from OneToOneField:
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT loyalty_ledgerentry_reverses_id_key UNIQUE (reverses_id);
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_amount_sign CHECK (
  (kind = 'credit' AND amount > 0) OR (kind = 'debit' AND amount < 0)
  OR (kind IN ('adjustment','reversal') AND amount <> 0));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_reversal_link CHECK (
  (kind = 'reversal' AND reverses_id IS NOT NULL) OR (kind <> 'reversal' AND reverses_id IS NULL));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_credit_fields CHECK (
  (kind = 'credit' AND base IS NOT NULL AND base >= 0 AND percent IS NOT NULL AND policy_id IS NOT NULL)
  OR (kind <> 'credit' AND base IS NULL AND percent IS NULL AND policy_id IS NULL AND promo_excluded = false));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_admin_is_adjustment CHECK (
  (source_type = 'admin' AND kind = 'adjustment') OR (source_type <> 'admin' AND kind <> 'adjustment'));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_source_refs CHECK (
  (source_type = 'order' AND order_id IS NOT NULL AND source_ref <> '')
  OR (source_type = 'billz_sale' AND order_id IS NULL AND purchase_id IS NOT NULL AND source_ref <> '')
  OR (source_type = 'admin' AND order_id IS NULL AND purchase_id IS NULL AND source_ref = ''));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_adjustment_fields CHECK (
  (kind = 'adjustment' AND actor_id IS NOT NULL AND reason <> '')
  OR (kind <> 'adjustment' AND actor_id IS NULL AND reason = ''));
ALTER TABLE loyalty_ledgerentry ADD CONSTRAINT ledger_percent_range CHECK (
  percent IS NULL OR (percent > 0 AND percent <= 100));
CREATE INDEX ledger_account_at_idx ON loyalty_ledgerentry (account_id, at, id);
CREATE INDEX ledger_order_idx     ON loyalty_ledgerentry (order_id);
CREATE INDEX ledger_purchase_idx  ON loyalty_ledgerentry (purchase_id);
```

Column semantics by `kind` (contract §2.6 table, made precise):

| `kind` | `source_type` | `amount` | `order` | `purchase` | `source_ref` | `base/percent/policy/promo_excluded` | `reverses` | `reason/actor` |
|---|---|---|---|---|---|---|---|---|
| `credit` | `order` | `> 0` | set | set (online purchase) | `str(order_id)` | set | null | `""`/null |
| `credit` | `billz_sale` | `> 0` | null | set (store purchase) | Billz document no. (`Purchase.billz_sale_ref`) | set | null | `""`/null |
| `debit` | `order` | `< 0` | set | null (posted at hold capture, before the purchase row exists) | `str(order_id)` | null/false | null | `""`/null |
| `reversal` | `order` or `billz_sale` | `= −reverses.amount` | as reversed entry | as reversed entry | as reversed entry | null/false | set, unique | `""`/null |
| `adjustment` | `admin` | `≠ 0` (`add` → `+amount`, `subtract` → `−amount`) | null | null | `""` | null/false | null | set, set |

**Invariants (services)**
- `post_entry(*, account, kind, amount, source_type, order_id=None, purchase=None, source_ref="", base=None,
  percent=None, promo_excluded=False, policy=None, reverses=None, reason="", actor=None, at=None) -> LedgerEntry`
  (`order_id` is a scalar — `loyalty` never holds a `commerce.Order` instance; `at` defaults to `timeutils.now()`)
  is the **only** writer of `LoyaltyAccount.balance`. It requires an open transaction and the account row already
  locked by the caller *or* locks it itself (`select_for_update(of=("self",))`), then:
  `LoyaltyAccount.objects.filter(pk=account.pk).update(balance=F("balance") + amount)`,
  `account.refresh_from_db(fields=["balance"])` (same lock, same transaction), `LedgerEntry.objects.create(…,
  balance_after=account.balance)`. The entry row and the balance update are atomic; a failed insert rolls back the
  balance. It never posts `amount == 0` (callers skip zero credits; see 1.6).
- `reverse_order_entries(order_id)`: under Order → Payment → LoyaltyAccount, for each entry of the order with
  `kind IN ('debit','credit')` that has no `reversed_by`, posts one `reversal` with `amount = −entry.amount`,
  `reverses = entry`, same `source_type/order/purchase/source_ref`. The unique `reverses_id` makes a double cancel
  (or an at-least-once redelivery) impossible: the second attempt raises `IntegrityError`, which the service turns
  into a no-op after re-checking `reversed_by`. The contract's `cancelled.data.cashback_reversed` is
  `len(reversals) > 0`.
- `adjust(customer_id, direction, amount, reason, actor)` (called by `customers`): validation in contract order; under
  the account lock, for `subtract`: `amount > available` → `insufficient_balance`; posts
  `kind='adjustment', source_type='admin', amount=±amount`. Returns `(entry, balance, available)`.
- `balance_after` of the newest entry of an account always equals `LoyaltyAccount.balance` (checked nightly).
- `at` is the business time (the contract's `at`; named like `OrderEvent.at`/`ShipmentEvent.at`, review minor 11):
  for credits/debits of an online order it equals the payment confirmation instant (same `timeutils.now()` as the
  `payment_confirmed` event); for store sales it is the posting time, while `Purchase.at` keeps the Billz sale time.

**Contract fields served**
- `CustomerCard.ledger[]` (full history, `ORDER BY at DESC, id DESC`): `id`, `at`, `kind`,
  `amount`, `balance_after`, `source_type`, `source_ref` (`""` → `null`), `order_id`, `base`, `percent` (2-dp string),
  `promo_excluded`, `policy_version` = `policy.version` (`select_related("policy")`), `reverses_id`, `reason`
  (`""` → `null`), `actor` (`Actor` | null).
- `Order.cashback[]` (`WHERE order_id = … ORDER BY id`): `ledger_entry_id`=`id`, `kind` (`OrderCashbackKind` =
  debit/credit/reversal; adjustments never carry an order), `amount`, `at`, `base`, `percent`, `reverses_id`.
- `OrderEvent cashback_credited.data {ledger_entry_id, amount, base, percent, policy_version}` is built by the
  orchestrator from the returned entry.
- Adjustment 201 `entry`; audit `cashback.adjusted {direction, amount, reason, balance_after}`.
- FE display strings (§5.3) all derive from these columns: `source` (`Buyurtma №{source_ref}` /
  `Billz savdo {source_ref}` / actor), `note` (base, percent, promo_excluded, reason).

**Open points**
- L-6 Reversals keep `actor = null` (contract: `reason`/`actor` are "adjustments only"); the cancelling admin is in the
  audit entry and the `cancelled` order event. Confirm the FE never needs the actor on a reversal row.
- L-7 A system-posted adjustment (e.g. a future reconciliation correction) is impossible under
  `ledger_adjustment_fields` (actor required). Relax to `actor IS NULL AND reason <> ''` only if such a flow is
  designed (none in M1–M3).
- L-8 `source_ref` for order entries duplicates `order_id` as text; kept so the serializer and exports never cast
  and `source_ref` has a single meaning ("the document the customer sees").

### 1.5 `CashbackHold` (time-stamped)
Cashback reserved at checkout for an unpaid order so it cannot be spent twice; captured into a debit when the
payment is confirmed, released otherwise.

```python
class CashbackHold(TimeStampedModel):
    class ReleaseReason(models.TextChoices):  # internal
        RESERVATION_EXPIRED = "reservation_expired"
        ORDER_CANCELLED = "order_cancelled"
        PAYMENT_FAILED = "payment_failed"

    account = models.ForeignKey("loyalty.LoyaltyAccount", on_delete=models.PROTECT, related_name="holds")
    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="cashback_holds")
    amount = models.BigIntegerField()
    state = models.CharField(
        max_length=16, choices=enums.CashbackHoldState.choices, default=enums.CashbackHoldState.ACTIVE
    )
    expires_at = models.DateTimeField()  # = Order.reservation_expires_at at creation
    captured_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="captured_hold"
    )
    captured_at = models.DateTimeField(null=True)
    released_at = models.DateTimeField(null=True)
    release_reason = models.CharField(max_length=24, blank=True, choices=ReleaseReason.choices)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order"], condition=models.Q(state="active"), name="hold_one_active_per_order"
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="hold_amount_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="active",
                        captured_entry__isnull=True,
                        captured_at__isnull=True,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | models.Q(
                        state="captured",
                        captured_entry__isnull=False,
                        captured_at__isnull=False,
                        released_at__isnull=True,
                        release_reason="",
                    )
                    | models.Q(
                        state="released",
                        captured_entry__isnull=True,
                        captured_at__isnull=True,
                        released_at__isnull=False,
                    )
                    & ~models.Q(release_reason="")
                ),
                name="hold_state_fields",
            ),
        ]
        indexes = [
            models.Index(
                fields=["account"], condition=models.Q(state="active"), name="hold_active_account_idx"
            ),
            models.Index(
                fields=["expires_at"], condition=models.Q(state="active"), name="hold_active_expires_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"Hold {self.amount} on order {self.order_id} ({self.state})"
```
```sql
CREATE UNIQUE INDEX hold_one_active_per_order ON loyalty_cashbackhold (order_id) WHERE state = 'active';
ALTER TABLE loyalty_cashbackhold ADD CONSTRAINT hold_amount_positive CHECK (amount > 0);
ALTER TABLE loyalty_cashbackhold ADD CONSTRAINT hold_state_fields CHECK (
  (state = 'active'   AND captured_entry_id IS NULL     AND captured_at IS NULL     AND released_at IS NULL     AND release_reason = '')
  OR (state = 'captured' AND captured_entry_id IS NOT NULL AND captured_at IS NOT NULL AND released_at IS NULL  AND release_reason = '')
  OR (state = 'released' AND captured_entry_id IS NULL  AND captured_at IS NULL AND released_at IS NOT NULL AND release_reason <> ''));
CREATE INDEX hold_active_account_idx ON loyalty_cashbackhold (account_id) WHERE state = 'active';
CREATE INDEX hold_active_expires_idx ON loyalty_cashbackhold (expires_at) WHERE state = 'active';
```

**Transition table**

| From | To | Trigger (caller → service) | Effects |
|---|---|---|---|
| — | `active` | checkout with `cashback_used > 0` (commerce → `create_hold(*, customer_id, order_id, amount, expires_at)`) | under the account lock: `amount ≤ available` else `insufficient_balance`; row with `expires_at = order.reservation_expires_at` (passed in); `Order.cashback_used == amount` is the orchestrator's invariant |
| `active` | `captured` | payment confirmed (commerce → `capture_hold(order_id)`) | `post_entry(kind=debit, amount=−hold.amount, source_type=order, order_id, source_ref=str(order_id))`; `captured_entry`, `captured_at`; `held` drops, `balance` drops by the same amount, so `available` is unchanged |
| `active` | `released` | reservation expiry job, cancel from `awaiting_payment`, terminal payment failure (commerce → `release_hold(order_id, reason)`) | `released_at`, `release_reason`; no ledger entry |
| `captured` | — | terminal | a later cancel posts a **reversal** of `captured_entry` (1.4); the hold itself stays `captured` |
| `released` | — | terminal | a re-payment of the same order never happens (the order is cancelled when the hold is released) |

`capture_hold`/`release_hold` on a hold that is already in the target state are no-ops (idempotent for
at-least-once callers); any other state mismatch raises `InvalidTransition`.

**Invariants (services)**
- At most one `active` hold per order (partial unique) and, in practice, one hold per order in total (an order is
  cancelled whenever its hold is released, and a captured hold is never recreated).
- `available` counts every `state='active'` hold, including ones past `expires_at` until the reservation-expiry job
  (beat, 1 min) releases them; the job and `create_hold` both run under the account lock, so `available` is never
  computed from a half-released state.
- Expiry handling belongs to the commerce expiry job (it refuses/cancels the provider transaction first, then
  releases stock and the hold, then writes `reservation_expired`); `loyalty` never scans holds itself.

**Contract fields served** — `Order.cashback_hold {amount, state, expires_at}` = **the order's `active` hold, or
`null`** (`CashbackHold.objects.filter(order_id=…, state="active").first()`; decided per review minor 14: the
contract example shows `"cashback_hold": null` for №10248, whose hold is `captured`, and the FE reads only
`state == "active"`; captured/released holds are visible through `Order.cashback[]` and the events instead);
`CustomerCard.held` = `Σ amount WHERE account_id = … AND state = 'active'`; FE §5.1 prepends a synthetic
"Ishlatildi" row from an `active` hold when no debit exists yet.

**Open points** — L-9 Whether `available` should exclude holds past `expires_at` immediately (SQL
`AND expires_at > now()`), making it independent of the expiry job's latency. Default: no (contract wording "active
holds"; one-minute job latency is acceptable; keeps the lock-based reasoning simple).

### 1.6 `Purchase` (time-stamped)
One purchase that counts towards the customer's lifetime total — an online order after payment, or an in-store
Billz sale resolved through the personal QR — with the cashback engine's inputs and result snapshotted.

```python
class Purchase(TimeStampedModel):
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="purchases")
    source = models.CharField(max_length=16, choices=enums.PurchaseSource.choices)  # online|store_qr
    order = models.OneToOneField(
        "commerce.Order", null=True, on_delete=models.PROTECT, related_name="purchase"
    )
    # Billz identifiers
    billz_sale_id = models.CharField(
        max_length=64, blank=True
    )  # Billz's own sale id (store sale, or the online echo)
    billz_sale_ref = models.CharField(
        max_length=32, blank=True
    )  # human document number shown to staff ("S-88213")
    billz_order_uuid = models.UUIDField(
        null=True
    )  # online: generated here BEFORE the push, used as the Billz order id
    billz_pushed_at = models.DateTimeField(null=True)  # online: set when billz.push_sale succeeded
    # engine inputs (snapshots)
    amount = (
        models.BigIntegerField()
    )  # gross purchase total before cashback redemption (= items_subtotal online; receipt total in store)
    promo_amount = models.BigIntegerField(default=0)  # part of amount that is promotional
    cashback_paid = models.BigIntegerField(
        default=0
    )  # part paid from the cashback balance (= Order.cashback_used)
    lifetime_before = (
        models.BigIntegerField()
    )  # Σ non-reversed purchases of the customer before this one, at credit time
    # engine result
    policy = models.ForeignKey(
        "loyalty.CashbackPolicy", null=True, on_delete=models.PROTECT, related_name="purchases"
    )  # version active at `at`; null = no policy existed
    cashback_amount = models.BigIntegerField(
        default=0
    )  # credited so'm (0 when no tier covers or eligible = 0)
    credited_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="credited_purchase"
    )
    # reversal
    reversed = models.BooleanField(default=False)
    reversed_at = models.DateTimeField(null=True)
    reversal_entry = models.OneToOneField(
        "loyalty.LedgerEntry", null=True, on_delete=models.PROTECT, related_name="reversed_purchase"
    )  # the reversal of credited_entry (null if nothing was credited)
    at = models.DateTimeField(
        default=timestamp_default
    )  # business time: payment confirmation / Billz sale time

    class Meta:
        ordering = ["-at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["billz_sale_id"],
                condition=~models.Q(billz_sale_id=""),
                name="purchase_billz_sale_unique",
            ),
            models.UniqueConstraint(fields=["billz_order_uuid"], name="purchase_billz_uuid_unique"  # ≤ 30 chars (E034)),
            models.CheckConstraint(
                condition=models.Q(
                    amount__gte=0,
                    promo_amount__gte=0,
                    cashback_paid__gte=0,
                    cashback_amount__gte=0,
                    lifetime_before__gte=0,
                    promo_amount__lte=models.F("amount"),
                    cashback_paid__lte=models.F("amount"),
                ),
                name="purchase_amounts",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source="online", order__isnull=False, billz_order_uuid__isnull=False)
                    | models.Q(source="store_qr", order__isnull=True, billz_order_uuid__isnull=True)
                    & ~models.Q(billz_sale_id="")
                ),
                name="purchase_source_refs",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(reversed=True, reversed_at__isnull=False)
                    | models.Q(reversed=False, reversed_at__isnull=True, reversal_entry__isnull=True)
                ),
                name="purchase_reversed_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(cashback_amount__gt=0, credited_entry__isnull=False)
                    | models.Q(cashback_amount=0, credited_entry__isnull=True, reversal_entry__isnull=True)
                ),
                name="purchase_credit_link",
            ),
        ]
        indexes = [
            models.Index(fields=["customer", "at", "id"], name="purchase_customer_at_idx"),
            models.Index(
                fields=["customer", "source"],
                condition=models.Q(reversed=False),
                name="purchase_live_cust_src_idx",
            ),
        ]

    def __str__(self) -> str:
        ref = (
            f"order {self.order_id}"
            if self.source == "online"
            else f"Billz {self.billz_sale_ref or self.billz_sale_id}"
        )
        return (
            f"Purchase #{self.pk} {self.source} {self.amount} ({ref}){' reversed' if self.reversed else ''}"
        )
```
```sql
CREATE UNIQUE INDEX purchase_billz_sale_unique ON loyalty_purchase (billz_sale_id) WHERE billz_sale_id <> '';
ALTER TABLE loyalty_purchase ADD CONSTRAINT purchase_billz_uuid_unique UNIQUE (billz_order_uuid);  -- NULLs distinct
ALTER TABLE loyalty_purchase ADD CONSTRAINT loyalty_purchase_order_id_key UNIQUE (order_id);              -- OneToOne
ALTER TABLE loyalty_purchase ADD CONSTRAINT purchase_amounts CHECK (
  amount >= 0 AND promo_amount >= 0 AND cashback_paid >= 0 AND cashback_amount >= 0 AND lifetime_before >= 0
  AND promo_amount <= amount AND cashback_paid <= amount);
ALTER TABLE loyalty_purchase ADD CONSTRAINT purchase_source_refs CHECK (
  (source = 'online' AND order_id IS NOT NULL AND billz_order_uuid IS NOT NULL)
  OR (source = 'store_qr' AND order_id IS NULL AND billz_order_uuid IS NULL AND billz_sale_id <> ''));
ALTER TABLE loyalty_purchase ADD CONSTRAINT purchase_reversed_fields CHECK (
  (reversed = true AND reversed_at IS NOT NULL)
  OR (reversed = false AND reversed_at IS NULL AND reversal_entry_id IS NULL));
ALTER TABLE loyalty_purchase ADD CONSTRAINT purchase_credit_link CHECK (
  (cashback_amount > 0 AND credited_entry_id IS NOT NULL)
  OR (cashback_amount = 0 AND credited_entry_id IS NULL AND reversal_entry_id IS NULL));
CREATE INDEX purchase_customer_at_idx   ON loyalty_purchase (customer_id, at, id);
CREATE INDEX purchase_live_cust_src_idx ON loyalty_purchase (customer_id, source) WHERE reversed = false;
```
(`Purchase ↔ LedgerEntry` is a circular FK; `makemigrations` emits `Purchase` without `credited_entry`/`reversal_entry`
and adds them in a later operation of the same migration — no manual work.)

**Creation flows (services)** — the write order matters because `purchase_credit_link` is a CHECK (evaluated per
statement, never deferrable) and `LedgerEntry.purchase` needs the purchase id before the entry can exist
(review major 3). Both recorders follow the same numbered sequence:

- `record_online_purchase(*, customer_id, order_id, items_subtotal, promo_subtotal, cashback_used, at,
  items, billz_order_uuid=None) -> tuple[Purchase, LedgerEntry | None]` — called by the commerce after-payment
  orchestration *after* `capture_hold`, under Order → Payment → LoyaltyAccount (the account row is already locked by
  the orchestrator's chain; the service re-acquires it with `_lock_account`, which is a no-op inside the same
  transaction). `items` is the list of line snapshots the orchestrator copies from `OrderItem`
  (`{product_id, billz_product_id, name, quantity, unit_price, promo, line_total}`) — `loyalty` never reads
  `commerce_orderitem`. `billz_order_uuid` defaults to `uuid4()`; the seeder passes the design UUID of №10248
  (03 S-1, seed-only use).
  1. `lifetime_before = customer_totals(customer_id).lifetime` (4.1 SQL; stable because the account lock is held and
     every purchase of this customer is written under it).
  2. `policy = active_policy_at(at)` with its tiers; `result = credit_for_purchase(amount=items_subtotal,
     promo_amount=promo_subtotal, cashback_paid=cashback_used, lifetime_before, policy, tiers)` (1.9.1).
  3. **`INSERT` the `Purchase` with `cashback_amount = 0`, `credited_entry = None`, `reversal_entry = None`**
     (all other columns final: `source=online`, `order_id`, `billz_order_uuid`, `amount`, `promo_amount`,
     `cashback_paid`, `lifetime_before`, `policy`, `at`). `purchase_credit_link` holds (`0 / NULL / NULL`).
  4. `bulk_create` the `PurchaseItem` rows from `items` (positions in order).
  5. If `result.credit > 0`: `entry = post_entry(kind=credit, amount=result.credit, source_type=order, order_id,
     purchase=purchase, source_ref=str(order_id), base=result.eligible, percent=result.percent,
     promo_excluded=promo_subtotal > 0, policy=policy, at=at)`.
  6. If an entry was posted: **one** `Purchase.objects.filter(pk=purchase.pk).update(cashback_amount=result.credit,
     credited_entry=entry, updated_at=at)` — both columns change in the same statement, so the CHECK holds at
     every statement boundary (`> 0 / NOT NULL`). Never two separate saves.
  7. Return `(purchase, entry or None)` so the orchestrator writes `cashback_credited` and enqueues
     `billz.push_sale` with `dedupe_key = f"billz.push_sale:{purchase.billz_order_uuid}"`. The UUID is persisted
     **before** the outbox row is committed, so an at-least-once redelivery always pushes the same Billz order id
     and the Billz-side duplicate check works.
- `link_billz_sale(purchase_id, billz_sale_id, billz_sale_ref)` — the online echo discovered later by Billz sync (M3)
  sets `billz_sale_id`/`billz_sale_ref`/`billz_pushed_at`; it **never** credits again (`purchase_billz_sale_unique`
  also blocks a store-style re-ingestion of the same Billz sale).
- `record_store_purchase(*, customer_id, billz_sale_id, billz_sale_ref, amount, promo_amount, items, sold_at)`
  (M2/M3): same seven steps under the `LoyaltyAccount` lock only, with `cashback_paid = 0` (L6: no in-store
  redemption), `source_type = billz_sale`, `source_ref = billz_sale_ref`, `at = sold_at` on the purchase and
  `at = now` on the entry. Idempotent on `billz_sale_id` (unique; an existing row is returned unchanged).
- `reverse_purchase(order_id)` — see 1.10 (part of the cancel flow).

**Invariants (services)**
- `customer_totals` (section 4.3) read only `reversed = false` rows; a reversed purchase keeps all its numbers for
  history (`CustomerCard.purchases[].reversed = true`).
- `cashback_amount == credited_entry.amount` and, when reversed with a credit, `reversal_entry.reverses_id ==
  credited_entry_id` (checked nightly, 1.9).
- `policy` is recorded even when `cashback_amount = 0` (no covering tier), so the card can still show
  `policy_version`; `null` only when no policy row exists at all.

**Contract fields served**
- `CustomerCard.purchases[]` (newest first by `at, id`): `id`, `at`, `source`, `order_id`, `billz_sale_ref`
  (`""` → `null`), `amount`, `promo_amount`, `cashback` = `cashback_amount`, `policy_version` = `policy.version`,
  `reversed`.
- `CustomerCard.totals {store, online, lifetime}`, `CustomerListItem.lifetime_total`, export
  `mijozlar.Jami xaridlar (so‘m)`, segment rule `min_lifetime_amount` and `min_online_orders` (= count of non-reversed
  `online` purchases), `CustomerCard.online_orders.count` (same count; `last_order_*` come from `commerce.Order`).
- `OrderEvent billz_sale_recorded.data.billz_order_id` = `str(billz_order_uuid)`.
- Engine `lifetime_before` for the `before_purchase` basis; `lifetime_before + amount` for `with_purchase`.

**Open points**
- L-10 `amount` for store sales = Billz receipt total *after* Billz discounts? and how promo lines are detected in a
  Billz receipt (by `billz_product_id` ∈ promo products at sale time). Blocks M3 with L7.
- L-11 Should a store purchase ever be reversed (Billz return)? The columns allow it (`reverse_purchase` is generic);
  no trigger exists until Billz returns are ingested (M3).

### 1.7 `PurchaseItem`
Line snapshot of a purchase (what was bought, at which unit price, promo or not) for the customer card and for
future Billz reconciliation; not needed by any M1 endpoint but written by `record_online_purchase` from the order
items so the ledger can be explained without joining `commerce`.

```python
class PurchaseItem(models.Model):
    purchase = models.ForeignKey("loyalty.Purchase", on_delete=models.CASCADE, related_name="items")
    position = models.PositiveSmallIntegerField()
    product = models.ForeignKey(
        "catalog.Product", null=True, on_delete=models.PROTECT, related_name="purchase_items"
    )  # null for Billz lines not in the catalog
    billz_product_id = models.CharField(max_length=32, blank=True)
    name = models.CharField(max_length=200)  # uz display name at purchase time, or the Billz name
    quantity = models.PositiveIntegerField()
    unit_price = models.BigIntegerField()
    promo = models.BooleanField(default=False)
    line_total = models.BigIntegerField()  # quantity × unit_price

    class Meta:
        ordering = ["purchase_id", "position"]
        constraints = [
            models.UniqueConstraint(fields=["purchase", "position"], name="purchaseitem_position_unique"),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0, unit_price__gte=0, line_total__gte=0),
                name="purchaseitem_amounts",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity} @ {self.unit_price} (purchase {self.purchase_id})"
```
```sql
ALTER TABLE loyalty_purchaseitem ADD CONSTRAINT purchaseitem_position_unique UNIQUE (purchase_id, position);
ALTER TABLE loyalty_purchaseitem ADD CONSTRAINT purchaseitem_amounts
  CHECK (quantity > 0 AND unit_price >= 0 AND line_total >= 0);
```
**Invariants** — `Σ line_total = purchase.amount` and `Σ line_total WHERE promo = purchase.promo_amount` for online
purchases (written from `OrderItem` snapshots in the same transaction). **Contract fields served** — none in M1
(M2 `/me/cashback` purchase detail). **Open points** — L-12 Billz may report fractional quantities for some goods;
`PositiveIntegerField` is kept (cosmetics are unit goods) and store lines with non-integer quantity are rounded with
a warning in the sync log until proven otherwise.

### 1.8 `PosDevice` (time-stamped, M2)
A cashier device/terminal allowed to call `POST /api/v1/pos/cashback/resolve-qr`, identified by a hashed API key.

```python
class PosDevice(TimeStampedModel):
    name = models.CharField(max_length=100)
    location_label = models.CharField(
        max_length=100, blank=True
    )  # free text; no FK to professionals.Location (sibling layer)
    key_prefix = models.CharField(max_length=12)  # first 8–12 chars of the key, for display/rotation
    key_hash = models.CharField(max_length=64, unique=True)  # SHA-256 hex of the full key
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="pos_devices"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return f"POS {self.name} ({self.key_prefix}…){'' if self.is_active else ' inactive'}"
```
**Invariants** — keys are generated by a management command (`createposkey`), shown once, stored only hashed;
lookups hash the presented key and compare with `compare_digest`; `last_used_at` is updated with
`update_fields=["last_used_at"]` (throttled to once per minute to avoid write amplification). `POS_API_KEYS` (env)
remains the fake-mode bootstrap in M2 and is loaded into rows by the seeder. **Contract fields served** — none in the
admin API; audit `pos.qr_resolved {device_id, customer_id}` (M2). **Open points** — L-13 whether the POS endpoint
should also record which `PosDevice` resolved the QR on the resulting `Purchase` (`resolved_by_device` FK); default:
only in the audit entry.

### 1.9 Engine, lock order and reconciliation

#### 1.9.1 Engine (`loyalty/engine.py`, pure functions, integer math only)
```
bp(percent)                = core.money.percent_to_bp(percent)              # Decimal('1.00') → 100, '0.57' → 57
eligible(amount, promo, cashback_paid, earn_on_cashback_paid)
                           = max(0, amount − promo − (0 if earn_on_cashback_paid else cashback_paid))
lifetime_basis(policy, lifetime_before, amount)
                           = lifetime_before                     if policy.basis == 'before_purchase'
                           = lifetime_before + amount            if policy.basis == 'with_purchase'
rate_for(tiers, lifetime)  = first tier (sorted by from) with from ≤ lifetime < (to or ∞), else None
credit                     = core.money.apply_bp(eligible, bp(tier.percent)) = eligible × bp // 10_000   (0 if no tier)
```
`credit_for_purchase(amount, promo_amount, cashback_paid, lifetime_before, policy, tiers) -> CreditResult(eligible,
tier, percent, credit)`. No floats anywhere; the FE preview (`frontend/src/domain/cashback.ts`) must switch from
`Math.floor(eligible * percent / 100)` to the same basis-point formula (plan F2).

**Test vectors** (asserted in `loyalty/tests/test_engine.py`; the first four are also added to `cashback.test.ts`):

| Case | amount | promo | cashback_paid | lifetime_before | policy | eligible | rate | credit |
|---|---|---|---|---|---|---|---|---|
| FE sample | 1 000 000 | 200 000 | 100 000 | 19 500 000 | before_purchase, earn=false, tiers 1/2/3 % | 700 000 | 1.00 % (bp 100) | **7 000** |
| FE sample, `with_purchase` | 1 000 000 | 200 000 | 100 000 | 19 500 000 | basis with_purchase (20 500 000 → tier 2) | 700 000 | 2.00 % | 14 000 |
| FE sample, `earn_on_cashback_paid` | 1 000 000 | 200 000 | 100 000 | 19 500 000 | earn=true | 800 000 | 1.00 % | 8 000 |
| floor | 999 | 0 | 0 | 0 | 1 % | 999 | 1.00 % | 9 (999×100 // 10 000) |
| 0.57 % | 231 000 | 0 | 0 | 0 | single tier 0.57 % | 231 000 | 0.57 % (bp 57) | 1 316 (13 167 000 // 10 000; float would give 1 316.7) |
| 0.57 % | 700 000 | 0 | 0 | 0 | 0.57 % | 700 000 | 0.57 % | 3 990 |
| 2.3 % | 568 000 | 0 | 0 | 0 | 2.3 % (bp 230) | 568 000 | 2.30 % | 13 064 |
| 2.3 % floor | 999 | 0 | 0 | 0 | 2.3 % | 999 | 2.30 % | 22 (229 770 // 10 000) |
| 1.5 % | 700 000 | 0 | 0 | 0 | 1.5 % (bp 150) | 700 000 | 1.50 % | 10 500 |
| 1.5 % floor | 999 | 0 | 0 | 0 | 1.5 % | 999 | 1.50 % | 14 (149 850 // 10 000) |
| no covering tier | 1 000 000 | 0 | 0 | 50 000 000 | tiers end at 50M | 1 000 000 | None | 0 → no ledger entry, `Purchase.cashback_amount = 0` |
| order №10248 (seed) | 808 000 (items_subtotal) | 240 000 | 48 600 | 12 772 000 (Malika: 13 580 000 − 808 000) | v3 **earn_on_cashback_paid = true** | **568 000** | 1.00 % | **5 680** |
| order №10248 if `earn=false` | 808 000 | 240 000 | 48 600 | 12 772 000 | v3 earn=false | 519 400 | 1.00 % | 5 194 (not the drawn value — see 4.5) |
| Malika Billz S-88213 (seed) | 231 000 | 0 | 0 | < 20M | v3 | 231 000 | 1.00 % | 2 310 ("Chek 231 000 so‘m · 1%") |
| Malika order №10231 (seed) | 314 000 + promo | promo > 0 | 20 000 | < 20M | v3 earn=true | 314 000 | 1.00 % | 3 140 ("314 000 so‘m · 1% · aksiyasiz qism") |
| `rate_for` bounds | — | — | — | 0 / 19 999 999 / 20 000 000 / 49 999 999 / 50 000 000 | tiers 1/2/3 % to 50M | — | 1 / 1 / 2 / 3 / None | — |

Delivery fee never enters `amount` (`items_subtotal`, not `total`).

#### 1.9.2 Lock order and who holds which lock
Global rule: **Order → Payment → LoyaltyAccount**, always `select_for_update(of=("self",))`, never in the reverse
direction, and a flow may skip a level but never go back (a transaction that holds a `LoyaltyAccount` lock must not
then lock an `Order`). `Shipment`, `CashbackHold`, `Purchase` and `Refund` rows have no lock of their own: they are
mutated only while their order's (resp. payment's) lock is held, which serialises them.

| Flow (owner module → service) | Locks taken, in order | Notes |
|---|---|---|
| Checkout (M2, `commerce.services.checkout`) | new `Order` row (insert = implicit lock) → `LoyaltyAccount` (inside `loyalty.services.create_hold`) | stock reservations are catalog rows locked before the account (catalog is below loyalty in the call chain, but it is not part of the three-level rule; document the full order as Order → StockReservation/ProductStock → Payment → LoyaltyAccount) |
| Payment confirmed (M2 webhook view in `commerce`) | `Order` → `Payment` → `LoyaltyAccount` (once; `capture_hold(order_id)` and `record_online_purchase(...)` re-lock the account inside the same transaction, which PostgreSQL treats as already held) | `payments.services.confirm()` mutates the payment under the Payment lock; then `loyalty.services.capture_hold`, `record_online_purchase`; then `delivery.services.request_bts_create`; outbox rows; events |
| Admin cancel (`commerce.services.cancel_order`) | `Order` → `Payment` (the active one, if any) → `LoyaltyAccount` (inside `reverse_order_entries`/`release_hold`) | synchronous BTS cancel happens **before** taking the Payment lock but after the Order lock (it is a network call; keep the transaction short: open the transaction, lock Order, call BTS, continue). `payments.services.create_refund` under the Payment lock |
| Reservation expiry job (`commerce.tasks`) | `Order` → `Payment` (fail it) → `LoyaltyAccount` (`release_hold`) | per order, one transaction each |
| Cashback adjustment (`customers` → `loyalty.services.adjust`) | `LoyaltyAccount` only | no order involved; `insufficient_balance` is evaluated under this lock |
| Store purchase (M2 POS / M3 Billz sync → `loyalty.services.record_store_purchase`) | `LoyaltyAccount` only | idempotent on `billz_sale_id` |
| Refund submit handler (`commerce.tasks` → `payments.services.mark_*`) | `Payment` → (no account) | `Payment.status → refunded` under the Payment lock; the Order is read, not locked (no order column changes) — allowed by the "skip but never go back" rule |
| Payme inbound `CancelTransaction` on a performed transaction (M2/M3) | `Order` → `Payment` | resolves the `manual_required` refund; does not touch the account (reversals were posted at cancel time) |
| BTS create handler (`commerce.tasks`) | `Order` (to read status and write the event) | `delivery.services.mark_created/mark_failed` under that lock; no Payment/Account |
| Policy version create | advisory lock `hashtext('loyalty.cashbackpolicy')` | independent of the three-level rule |
| Nightly reconciliation | none | single-statement snapshots (1.9.3) |

#### 1.9.3 Nightly reconciliation (`loyalty.tasks.reconcile_balances`, beat 03:30 Tashkent)
Each check is one SQL statement (one snapshot under READ COMMITTED), so concurrent postings cannot produce false
mismatches; findings are written as audit entries `loyalty.reconciliation_mismatch` (actor `system`,
`entity_type="loyalty_account"`, `after={check, balance, expected, delta}`), and a summary
`loyalty.reconciliation_completed {accounts, mismatches, negative_balances}`. Nothing is corrected automatically.

```sql
-- C1: balance == Σ entries
SELECT a.id, a.balance, COALESCE(SUM(e.amount), 0) AS ledger_sum
FROM loyalty_loyaltyaccount a
LEFT JOIN loyalty_ledgerentry e ON e.account_id = a.id
GROUP BY a.id, a.balance
HAVING a.balance <> COALESCE(SUM(e.amount), 0);

-- C2: the newest entry's balance_after == balance (catches a balance change without an entry)
SELECT a.id, a.balance, last.balance_after
FROM loyalty_loyaltyaccount a
JOIN LATERAL (SELECT e.balance_after FROM loyalty_ledgerentry e
              WHERE e.account_id = a.id ORDER BY e.at DESC, e.id DESC LIMIT 1) last ON true
WHERE last.balance_after <> a.balance;

-- C3: the running chain is consistent (balance_after_i == balance_after_{i-1} + amount_i)
SELECT id, account_id FROM (
  SELECT e.id, e.account_id, e.amount, e.balance_after,
         LAG(e.balance_after, 1, 0) OVER (PARTITION BY e.account_id ORDER BY e.at, e.id) AS prev_after
  FROM loyalty_ledgerentry e) x
WHERE balance_after <> prev_after + amount;

-- C4: available = balance − Σ active holds (reported, never asserted; negative values are the L5 flag)
SELECT a.id, a.balance,
       COALESCE((SELECT SUM(h.amount) FROM loyalty_cashbackhold h
                 WHERE h.account_id = a.id AND h.state = 'active'), 0) AS held,
       a.balance - COALESCE((SELECT SUM(h.amount) FROM loyalty_cashbackhold h
                             WHERE h.account_id = a.id AND h.state = 'active'), 0) AS available
FROM loyalty_loyaltyaccount a
WHERE a.balance < 0;

-- C5: purchase ↔ entry links
SELECT p.id FROM loyalty_purchase p
LEFT JOIN loyalty_ledgerentry c ON c.id = p.credited_entry_id
LEFT JOIN loyalty_ledgerentry r ON r.id = p.reversal_entry_id
WHERE (p.cashback_amount > 0 AND (c.amount <> p.cashback_amount OR c.purchase_id <> p.id))
   OR (p.reversed AND p.credited_entry_id IS NOT NULL AND (r.id IS NULL OR r.reverses_id <> p.credited_entry_id));

-- C6: captured holds ↔ debit entries
SELECT h.id FROM loyalty_cashbackhold h JOIN loyalty_ledgerentry d ON d.id = h.captured_entry_id
WHERE d.kind <> 'debit' OR d.amount <> -h.amount OR d.order_id <> h.order_id;
```
C3 relies on `at` ordering within an account being the posting order; `post_entry` runs under the account lock and
uses the caller's `at` (defaulting to `timeutils.now()` at insert time), so ties can only be equal timestamps
resolved by `id`.

### 1.10 Purchase reversal flow (order cancelled after payment)
Owner: `commerce.services.cancel_order` (one transaction). Steps in lock order:

1. `Order` locked; status check (contract cancel table); synchronous BTS cancel if a parcel exists (3.1).
2. `Payment` locked (the active `paid` one): `payments.services.create_refund(payment, requested_by=actor,
   origin=order_cancelled)` → Click `requested` + outbox `refund.submit`, Payme `manual_required` (2.2).
3. `loyalty.services.reverse_purchase(order_id)` — `LoyaltyAccount` locked inside:
   - `Purchase` of the order (`order` 1:1): `reversed = true`, `reversed_at = now` (no-op if already reversed → the
     whole step is idempotent).
   - `reverse_order_entries(order_id)`: one `reversal` per unreversed `debit`/`credit` of the order (unique
     `reverses_id`); the reversal of `credited_entry` is stored as `Purchase.reversal_entry`.
   - Net effect on the balance: `+cashback_used − cashback_amount`; it may go negative (L5), which is allowed and
     surfaces as `balance < 0` in C4 and on the customer card.
   - `lifetime` drops by `purchase.amount` for every later rate computation; `lifetime_before` snapshots of earlier
     purchases are untouched.
4. Hold: stays `captured` (its debit is now reversed). If the order was still `awaiting_payment` the flow is
   different: `release_hold(order_id, reason=order_cancelled)`, no purchase exists, nothing is reversed.
5. Events `cancelled {reason, note, refund_id, cashback_reversed: n_reversals > 0, stock_released, bts_cancelled,
   push_sent}` and `refund_requested`; outbox `push.order_status`; audit `order.cancelled`.

| Order status at cancel | Purchase | Ledger | Hold | Payment |
|---|---|---|---|---|
| `awaiting_payment` | none | none | `active → released (order_cancelled)` | `pending → failed (error_code 'cancelled')`, **no `payment_failed` event** (2.1, 03 C-8); a Payme state-1 transaction stays `pending` until Payme's `CancelTransaction` (P-2) |
| `processing` (paid) | `reversed = true` | reversal of debit (+) and credit (−) | stays `captured` | stays `paid`; Refund created |
| `handed_to_courier` (paid, BTS status 100 only) | same as processing | same | same | same |
| `delivered` / `cancelled` | — | — | — | 409 `not_cancellable` |

---

## 2. `payments`

### 2.0 Module overview
```
payments/
  models.py    Payment (TS), Refund (TS)
  services.py  create_payment, confirm_payment, fail_payment, create_refund, mark_submitting, mark_succeeded,
               mark_failed, mark_unknown, resolve_manual
  selectors.py order_payment (display payment of an order), payments_list (filters/export), refunds_for_payment,
               revenue_buckets (dashboard), refunds_needing_action
  providers/   registry + ports (click, payme) — the protocol helpers used by commerce/api/webhooks
```
Protocol *handlers* (Click prepare/complete, Payme JSON-RPC) live in `commerce/api/webhooks/` (plan §4.1); this
module owns the rows, the state machines and the signature/amount helpers.

### 2.1 `Payment` (time-stamped)
One attempt to collect an order's `total` with one provider; at most one of them is active (`pending` or `paid`)
per order.

```python
class Payment(TimeStampedModel):
    order = models.ForeignKey("commerce.Order", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=16, choices=enums.PaymentProvider.choices)  # click|payme
    status = models.CharField(
        max_length=16, choices=enums.PaymentStatus.choices, default=enums.PaymentStatus.PENDING
    )  # pending|paid|failed|refunded
    amount = models.BigIntegerField()  # = Order.total at creation
    provider_txn_id = models.CharField(
        max_length=64, blank=True, db_index=True
    )  # Click click_trans_id / Payme transaction id
    confirmed_at = models.DateTimeField(null=True)
    failed_at = models.DateTimeField(null=True)
    refunded_at = models.DateTimeField(null=True)
    error_code = models.CharField(
        max_length=32, blank=True
    )  # provider error code as text ("-9", "-31008", "cancelled", "abandoned", "expired")
    error_message = models.CharField(max_length=254, blank=True)
    provider_state = models.JSONField(
        default=dict, blank=True
    )  # provider protocol state, keys in the table below

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order"],
                condition=models.Q(status__in=["pending", "paid"]),
                name="payment_one_active_per_order",
            ),
            models.UniqueConstraint(
                fields=["provider", "provider_txn_id"],
                condition=~models.Q(provider_txn_id=""),
                name="payment_provider_txn_unique",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="payment_amount_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        status="pending",
                        confirmed_at__isnull=True,
                        failed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | models.Q(
                        status="paid",
                        confirmed_at__isnull=False,
                        failed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | models.Q(
                        status="failed",
                        failed_at__isnull=False,
                        confirmed_at__isnull=True,
                        refunded_at__isnull=True,
                    )
                    | models.Q(
                        status="refunded",
                        confirmed_at__isnull=False,
                        refunded_at__isnull=False,
                        failed_at__isnull=True,
                    )
                ),
                name="payment_status_times",
            ),
            models.CheckConstraint(
                condition=~models.Q(status__in=["paid", "refunded"]) | ~models.Q(provider_txn_id=""),
                name="payment_paid_has_txn",
            ),
        ]
        indexes = [
            models.Index(fields=["order", "id"], name="payment_order_idx"),
            models.Index(fields=["created_at", "id"], name="payment_created_idx"),
            models.Index(fields=["status", "created_at"], name="payment_status_created_idx"),
            models.Index(
                fields=["confirmed_at"],
                condition=models.Q(status__in=["paid", "refunded"]),
                name="payment_confirmed_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Payment #{self.pk} {self.provider} {self.status} {self.amount} (order {self.order_id})"
```
```sql
CREATE UNIQUE INDEX payment_one_active_per_order ON payments_payment (order_id) WHERE status IN ('pending', 'paid');
CREATE UNIQUE INDEX payment_provider_txn_unique  ON payments_payment (provider, provider_txn_id) WHERE provider_txn_id <> '';
ALTER TABLE payments_payment ADD CONSTRAINT payment_amount_positive CHECK (amount > 0);
ALTER TABLE payments_payment ADD CONSTRAINT payment_status_times CHECK (
  (status = 'pending'  AND confirmed_at IS NULL     AND failed_at IS NULL     AND refunded_at IS NULL)
  OR (status = 'paid'     AND confirmed_at IS NOT NULL AND failed_at IS NULL     AND refunded_at IS NULL)
  OR (status = 'failed'   AND failed_at IS NOT NULL    AND confirmed_at IS NULL  AND refunded_at IS NULL)
  OR (status = 'refunded' AND confirmed_at IS NOT NULL AND refunded_at IS NOT NULL AND failed_at IS NULL));
ALTER TABLE payments_payment ADD CONSTRAINT payment_paid_has_txn CHECK (
  status NOT IN ('paid', 'refunded') OR provider_txn_id <> '');
CREATE INDEX payment_order_idx          ON payments_payment (order_id, id);
CREATE INDEX payment_created_idx        ON payments_payment (created_at, id);
CREATE INDEX payment_status_created_idx ON payments_payment (status, created_at);
CREATE INDEX payment_confirmed_idx      ON payments_payment (confirmed_at) WHERE status IN ('paid', 'refunded');
```

`provider_state` keys (all optional, set as the protocol progresses; raw payloads live in `WebhookInbox`):

| Provider | Key | Type | Set at |
|---|---|---|---|
| click | `merchant_trans_id` | str (= `str(order.id)`) | creation |
| click | `merchant_prepare_id` | int (= `payment.id`) | prepare |
| click | `click_trans_id`, `click_paydoc_id` | str | prepare (also copied to `provider_txn_id`) |
| click | `service_id` | int | prepare (bound; a mismatch is rejected) |
| click | `prepare_at`, `complete_at` | ISO datetime | prepare / complete |
| click | `error`, `error_note` | int, str | last callback (`error < 0` → cancel, reply `-9`) |
| click | `sign_time` | str | last callback |
| click | `refund_status` | str | refund status checks (M3) |
| payme | `transaction` | str (also `provider_txn_id`) | CreateTransaction |
| payme | `time`, `create_time`, `perform_time`, `cancel_time` | int ms | respective methods |
| payme | `state` | int (`1`, `2`, `-1`, `-2`) | respective methods |
| payme | `reason` | int \| null | CancelTransaction |
| payme | `account` | object (`{"order_id": "10248"}`) | CreateTransaction |

**State machine**

| From | To | Trigger | Effects |
|---|---|---|---|
| — | `pending` | checkout (M2) creates the row with `provider = order.payment_provider`, `amount = order.total` | partial unique guarantees no second active payment |
| `pending` | `pending` (txn attached) | Click prepare / Payme CreateTransaction | `provider_txn_id`, `provider_state`; `(provider, provider_txn_id)` unique blocks a duplicate transaction id; a second Payme CreateTransaction for the same order while one is active → `-31008` before any write |
| `pending` | `paid` | Click complete (error 0, amount == payable) / Payme PerformTransaction | `confirmed_at`; orchestrator continues (hold capture, purchase, Billz push, BTS create, `processing`, event `payment_confirmed`) |
| `pending` | `failed` | Click complete with `error < 0`, Payme CancelTransaction (state `-1`), reservation expiry (`expired`), admin cancel from `awaiting_payment` (`cancelled`), provider switch (`abandoned`) | `failed_at`, `error_code`; **event `payment_failed` only when the trigger is a provider callback** (Click `error < 0`, Payme `CancelTransaction` on a state-1 transaction — also when it arrives after the order was already cancelled/expired); expiry, admin cancel and provider switch write **no** event — `reservation_expired.data.payment_failed` / `cancelled` carry the fact (contract §4 defines `payment_failed` as the provider-failure row; 03 C-8, review major 7). A Payme transaction in state 1 is never failed unilaterally by expiry/admin cancel/switch: it stays `pending` and is failed by Payme's own `CancelTransaction` (our `PerformTransaction` answer is `-31008`), see P-2 |
| `paid` | `refunded` | `Σ Refund.amount WHERE state = 'succeeded' == amount` (set by `mark_succeeded` / Payme cancel of a performed transaction) | `refunded_at` |
| `paid` | `paid` (late capture) | capture arrives for an order no longer `awaiting_payment` (M2) | refused at protocol level; if it still lands: `paid` + automatic `Refund(origin=late_capture)` + staff alert (spec) |
| `failed`, `refunded` | — | terminal | a new attempt is a new row |

**Invariants (services)**
- `amount == order.total` at creation and never changes; providers are told to pay exactly `amount` (Click `-2`,
  Payme `-31001` on mismatch).
- Only `confirm_payment`/`fail_payment`/`mark_refunded` write `status`, all under the Payment lock
  (`select_for_update`) with the Order already locked by the orchestrator (1.9.2).
- `payment.status` stays `paid` while refunds are in progress (O5); `refunded` only when succeeded refunds cover
  the full amount.
- Display payment of an order (`selectors.order_payment(order)`): the row with the highest rank among
  `refunded/paid` (rank 2) → `pending` (rank 1) → `failed` (rank 0), newest `id` first. When no row exists yet (an
  `awaiting_payment` order created before checkout attached one — only possible in seeds) the serializer returns
  `{status: "pending", provider: order.payment_provider, amount: order.total, transaction_id: null, confirmed_at: null}`.

**Contract fields served**
- `OrderSummary.payment {status, provider}`, `Order.payment {status, provider, amount, transaction_id
  (provider_txn_id, "" → null), confirmed_at}`, `Order.refunds[]` (2.2), `orders/?payment=` filter (on the display
  payment's status — implemented as `EXISTS` on the ranked subquery), export `buyurtmalar.To‘lov` (status label) and
  `Usul` (provider label).
- `GET payments/` list item: `id`, `order_id`, `customer` (`order.customer`), `provider`, `status`, `amount`,
  `transaction_id`, `created_at`, `confirmed_at`, `refunds[]`. Filters: `status`, `provider`, `refund_state`
  (`EXISTS refund WHERE payment_id = … AND state = …`), `period`/`date_from`/`date_to` on `created_at` (Tashkent
  semantics as `orders/`), `search` (`order_id::text ILIKE %term%` OR `provider_txn_id ILIKE %term%` OR, with ≥3
  digits, `customer.phone` contains them), `ordering ±created_at` ties by `id`. Export `tolovlar`: `ID; Buyurtma №;
  Mijoz; Telefon; Usul; Holat; Summa (so‘m); Tranzaksiya; Yaratilgan; Tasdiqlangan; Qaytarish` (`Qaytarish` = the
  newest refund's state label, else empty).
- Dashboard `revenue` per bucket: `Σ amount` of payments with `confirmed_at` in the bucket and
  `status IN ('paid','refunded')` **minus** `Σ Refund.amount WHERE state='succeeded'` of those payments (attributed
  to the payment's bucket, never negative). SQL sketch:
  ```sql
  SELECT date_trunc(%(unit)s, p.confirmed_at AT TIME ZONE 'Asia/Tashkent') AS bucket,
         SUM(p.amount) - COALESCE(SUM(r.refunded), 0) AS revenue
  FROM payments_payment p
  LEFT JOIN (SELECT payment_id, SUM(amount) AS refunded FROM payments_refund WHERE state = 'succeeded'
             GROUP BY payment_id) r ON r.payment_id = p.id
  WHERE p.status IN ('paid', 'refunded') AND p.confirmed_at >= %(from)s AND p.confirmed_at < %(to)s
  GROUP BY 1;
  ```
- `OrderEvent payment_confirmed.data {provider, transaction_id, amount, reservation_converted}` and
  `payment_failed.data {provider, transaction_id, error_code}` are built from this row.

**Open points**
- P-1 `raw_*` JSON columns from the skeleton are **dropped**: every raw callback is already an immutable
  `integrations.WebhookInbox` row (`(provider, event_key)`, `result` references `payment_id`), and keeping raw copies
  on a mutable row would be a second, unaudited store. `provider_state` holds only the structured protocol fields
  above. Reinstate `raw_last_callback` if the review wants a quick-look column.
- P-2 Payment row at checkout (this design) vs at `POST orders/{id}/pay` (M2 alternative). Creating it at checkout
  keeps "one active payment per order" meaningful from the start and makes `orders/?payment=pending` exact; a
  provider switch is `fail_payment(abandoned)` + new row in one transaction. A Payme transaction in state 1 cannot
  be abandoned unilaterally (Payme would still call `PerformTransaction`): the switch is refused while
  `provider_state.state == 1` (409 `conflict` in the customer API). **Decided (review major 7):** reservation expiry
  and admin cancel from `awaiting_payment` apply the same rule — `fail_payment("expired"|"cancelled")` only for a
  `pending` payment whose `provider_state.state != 1` (Click, or Payme before `CreateTransaction`); a Payme state-1
  transaction stays `pending` on the now-cancelled order, the next `PerformTransaction` is answered `-31008`, and
  Payme's `CancelTransaction` fails it (`error_code = reason`, event `payment_failed`). Contract §2.2's "a `pending`
  payment becomes `failed`" is read as "every pending payment that can be failed unilaterally"; recorded as a
  contract §7 delta note. `reservation_expired.data.payment_failed` is `true` iff a payment was failed in that
  transaction.
- P-3 `provider_txn_id` examples in the contract ("CLK-7741920385") are illustrative; real Click ids are numeric
  strings and Payme ids are 24-hex strings. The fake adapters generate the contract-style ids so seeds match the
  design.

### 2.2 `Refund` (time-stamped)
A request to return a paid amount to the customer, driven by the 6-state machine of the contract; at most one open
refund per payment.

```python
class Refund(TimeStampedModel):
    class Origin(models.TextChoices):  # internal
        ORDER_CANCELLED = "order_cancelled"
        LATE_CAPTURE = "late_capture"  # capture landed after expiry (M2 auto-refund)
        MANUAL = "manual"  # staff-initiated retry (M3 command)

    payment = models.ForeignKey("payments.Payment", on_delete=models.PROTECT, related_name="refunds")
    state = models.CharField(
        max_length=16, choices=enums.RefundState.choices, default=enums.RefundState.REQUESTED
    )  # requested|submitting|succeeded|failed|manual_required|unknown
    amount = models.BigIntegerField()  # = payment.amount in M1 (full refunds only)
    origin = models.CharField(max_length=16, choices=Origin.choices, default=Origin.ORDER_CANCELLED)
    provider_refund_id = models.CharField(max_length=64, blank=True)
    requested_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="refunds_requested"
    )  # null = system
    requested_at = models.DateTimeField(default=timestamp_default)
    submitted_at = models.DateTimeField(null=True)  # first handover to the provider
    last_attempt_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)  # succeeded only
    attempts = models.PositiveIntegerField(default=0)
    error_code = models.CharField(max_length=32, blank=True)
    last_error = models.TextField(blank=True)
    provider_state = models.JSONField(
        default=dict, blank=True
    )  # click: {"payment_status": int, "error": int, "error_note": str, "checked_at": iso}; payme: {"cancel_time": int, "reason": int, "state": -2}

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment"],
                condition=models.Q(state__in=["requested", "submitting", "manual_required", "unknown"]),
                name="refund_one_open_per_payment",
            ),
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="refund_amount_positive"),
            models.CheckConstraint(
                condition=(
                    models.Q(state="succeeded", completed_at__isnull=False)
                    | ~models.Q(state="succeeded") & models.Q(completed_at__isnull=True)
                ),
                name="refund_completed_at",
            ),
            models.CheckConstraint(
                condition=~models.Q(state__in=["submitting", "unknown"])
                | models.Q(submitted_at__isnull=False),
                name="refund_submitted_at",
            ),
        ]
        indexes = [
            models.Index(fields=["payment", "id"], name="refund_payment_idx"),
            models.Index(
                fields=["state"],
                condition=models.Q(state__in=["failed", "unknown", "manual_required"]),
                name="refund_attention_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Refund #{self.pk} {self.state} {self.amount} (payment {self.payment_id})"
```
```sql
CREATE UNIQUE INDEX refund_one_open_per_payment ON payments_refund (payment_id)
  WHERE state IN ('requested', 'submitting', 'manual_required', 'unknown');
ALTER TABLE payments_refund ADD CONSTRAINT refund_amount_positive CHECK (amount > 0);
ALTER TABLE payments_refund ADD CONSTRAINT refund_completed_at CHECK (
  (state = 'succeeded' AND completed_at IS NOT NULL) OR (state <> 'succeeded' AND completed_at IS NULL));
ALTER TABLE payments_refund ADD CONSTRAINT refund_submitted_at CHECK (
  state NOT IN ('submitting', 'unknown') OR submitted_at IS NOT NULL);
CREATE INDEX refund_payment_idx   ON payments_refund (payment_id, id);
CREATE INDEX refund_attention_idx ON payments_refund (state) WHERE state IN ('failed', 'unknown', 'manual_required');
```

**Transition table** (service = `payments.services.*`; every transition is audited `refund.state_changed`
(system) with `{from, to, attempts, error_code}`; the orchestrator writes the order events named in the last column)

| From | To | Trigger | Effects / guards | Order event |
|---|---|---|---|---|
| — | `requested` | `create_refund` for a **Click** payment (cancel of a paid order; late capture) | row + outbox `refund.submit` with `dedupe_key = f"refund.submit:{refund.id}"`; audit `refund.requested` | `refund_requested {refund_id, amount, provider, manual: false}` |
| — | `manual_required` | `create_refund` for a **Payme** payment | row only; staff do it in the Payme cabinet | `refund_requested {…, manual: true}` |
| `requested` | `submitting` | handler `refund.submit` claims the row (`mark_submitting`, own short transaction, committed before the provider call) | `submitted_at` (first time), `last_attempt_at`, `attempts += 1` | — |
| `requested` | `failed` | the `refund.submit` message goes `dead` (10 transport failures **before** any submission; `submitted_at IS NULL`) → dead-letter hook `commerce.tasks.on_outbox_dead` calls `mark_failed(refund, error_code="dead_letter", last_error=message.last_error)` (review major 5: `unknown` would violate `refund_submitted_at` and misstate that money reached the provider) | `error_code = "dead_letter"`, `last_error`; `refunds_needing_action` counts it; a retry is a new `Refund(origin=manual)` | `refund_failed {…, state: "failed", error_code: "dead_letter"}` |
| `submitting` | `succeeded` | provider confirms reversal (Click `payment_status` = reversed, or the reversal call returned success) | `provider_refund_id`, `completed_at`; `Payment → refunded` if the succeeded sum equals `payment.amount` | `refund_succeeded {refund_id, amount, provider}` |
| `submitting` | `failed` | provider answers with a definitive error | `error_code`, `last_error`; dashboard `refunds_needing_action` | `refund_failed {…, state: "failed", error_code}` |
| `submitting` | `unknown` | transport failure **after** submission *and* the follow-up status check also failed (or is inconclusive) | `last_error`; **never auto-retried**; alert (audit `outbox.dead`-style attention) | `refund_failed {…, state: "unknown", error_code: null}` |
| `submitting` | `submitting` | outbox redelivery finds the row already `submitting` | status check first (`payment_status` by `provider_txn_id`): reversed → `succeeded`; not reversed → safe to submit again (`attempts += 1`); check failed → `unknown` | — |
| `manual_required` | `succeeded` | Payme inbound `CancelTransaction` with the transaction in state `2` → `-2` | `provider_state`, `completed_at`; `Payment → refunded` | `refund_succeeded` |
| `unknown` | `succeeded` \| `failed` | M3 reconciliation command / staff resolve (`resolve_unknown`) after a manual status check | as above | `refund_succeeded` / `refund_failed` |
| `failed` | — | terminal | a retry is a **new** `Refund(origin=manual)` (the partial unique allows it because `failed` is closed) | new `refund_requested` |
| `succeeded` | — | terminal | | |

Handler idempotency at the provider boundary (`commerce/tasks.py`, topic `refund.submit`): lock the `Payment`, load
the refund; `succeeded`/`failed` → mark the message done; `requested` → `mark_submitting` and commit; then **always**
check the provider's status for `provider_txn_id` before calling the reversal; a redelivery after a crash therefore
either finds the reversal already done (→ `succeeded`) or knows it is safe to submit. The handler raises (→ outbox
backoff) only on transport errors *before* submission; after submission it resolves to `succeeded`/`failed`/`unknown`
and returns normally, so the outbox never retries an `unknown` refund. Consequently a message can only go `dead`
while the refund is still `requested`, which is why the dead-letter outcome is `failed (dead_letter)`, never
`unknown` (row above; 03 §5).

**Invariants (services)** — `amount ≤ payment.amount` and, in M1, `amount == payment.amount` (partial refunds are not
designed: O2); one open refund per payment (partial unique; `create_refund` on a payment with an open refund returns
the existing one, which makes `cancel_order` safe under idempotent replay); `payment.status` is derived from the
succeeded sum only.

**Contract fields served** — `Order.refunds[]` and `payments/[].refunds[]` (`Refund`: `id`, `state`, `provider` =
`payment.provider`, `amount`, `requested_at`, `updated_at`, `completed_at`), newest first by `requested_at, id`;
`payments/?refund_state=`; export `tolovlar.Qaytarish`; dashboard `attention.refunds_needing_action` =
`COUNT(*) WHERE state IN ('failed','unknown','manual_required')` (the partial index); FE §5.1 refund state line uses
`refunds[0].state`; order events `refund_requested/succeeded/failed` carry `refund_id`, `amount`, `provider`.

**Open points**
- P-4 There is no admin endpoint to retry a `failed` refund or to resolve an `unknown` one in the M1 contract; the
  state machine supports both through services (M3 management command `payments_resolve_refund`). Propose
  `POST payments/{id}/refunds/` (retry) for the undesigned Payments screen (F3).
- P-5 Whether `manual_required` can be marked `failed` by staff (e.g. Payme cabinet refuses); default: no, it stays
  open until the cabinet refund arrives as `CancelTransaction`.

---

## 3. `delivery`

### 3.0 Module overview
```
delivery/
  models.py    Shipment (TS), ShipmentEvent (AO)
  services.py  ensure_shipment, request_bts_create (enqueue + FK), mark_created, mark_failed, apply_status,
               cancel (synchronous BTS cancel; raises Conflict/ProviderUnavailable)
  selectors.py shipment_for_order (with derived `pending`), bts_failed_count (dashboard attention)
  bts.py       status-code mapping (100 = accepted/cancellable; codes → handed_to_courier / delivered)
```
The outbox handler for `bts.create_shipment` lives in `commerce/tasks.py` (it needs the order and writes
`OrderEvent`s) and calls `delivery.services` + `integrations.bts`.

### 3.1 `Shipment` (time-stamped, 1:1 order)
The BTS parcel record of a delivery order; one row per delivery order, created at checkout in state `not_created`.

```python
class Shipment(TimeStampedModel):
    class State(models.TextChoices):  # internal; API ShipmentState adds `not_required` and hides `cancelled`
        NOT_CREATED = "not_created"
        FAILED = "failed"
        CREATED = "created"
        CANCELLED = "cancelled"

    order = models.OneToOneField("commerce.Order", on_delete=models.PROTECT, related_name="shipment")
    state = models.CharField(max_length=16, choices=State.choices, default=State.NOT_CREATED)
    tracking_number = models.CharField(max_length=32, blank=True)
    tariff = models.BigIntegerField(
        null=True
    )  # so'm; NULL until created; from the BTS create response, else Order.delivery_fee (D-2)
    bts_order_id = models.CharField(max_length=64, blank=True)  # BTS's own id of the parcel
    bts_status = models.CharField(max_length=16, blank=True)  # raw BTS status code as text ("100", "200", …)
    via = models.CharField(
        max_length=16, blank=True, choices=enums.ShipmentVia.choices
    )  # source of the last status
    last_status_at = models.DateTimeField(null=True)  # API `updated_at` of the created state
    bts_created_at = models.DateTimeField(null=True)
    cancelled_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(
        default=0
    )  # BTS create attempts that got an answer (failed or created)
    create_requests = models.PositiveIntegerField(
        default=0
    )  # outbox enqueues (automatic + manual retries); part of the dedupe key
    last_attempt_at = models.DateTimeField(null=True)
    last_error_code = models.CharField(max_length=32, blank=True)
    last_error = models.TextField(blank=True)
    create_message = models.ForeignKey(
        "core.OutboxMessage", null=True, on_delete=models.SET_NULL, related_name="+"
    )  # the newest `bts.create_shipment` message; `pending` is derived from its status

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(tariff__isnull=True) | models.Q(tariff__gte=0),
                name="shipment_tariff_nonneg",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="not_created",
                        tracking_number="",
                        bts_created_at__isnull=True,
                        cancelled_at__isnull=True,
                    )
                    | models.Q(
                        state="failed",
                        tracking_number="",
                        bts_created_at__isnull=True,
                        cancelled_at__isnull=True,
                        attempts__gte=1,
                        last_attempt_at__isnull=False,
                    )
                    | models.Q(
                        state="created",
                        bts_created_at__isnull=False,
                        cancelled_at__isnull=True,
                        tariff__isnull=False,
                    )
                    & ~models.Q(tracking_number="")
                    | models.Q(
                        state="cancelled",
                        bts_created_at__isnull=False,
                        cancelled_at__isnull=False,
                        tariff__isnull=False,
                    )
                    & ~models.Q(tracking_number="")
                ),
                name="shipment_state_fields",
            ),
        ]
        indexes = [
            models.Index(fields=["state"], name="shipment_state_idx"),
            models.Index(
                fields=["tracking_number"],
                condition=~models.Q(tracking_number=""),
                name="shipment_tracking_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Shipment of order {self.order_id}: {self.state}{' ' + self.tracking_number if self.tracking_number else ''}"
```
```sql
ALTER TABLE delivery_shipment ADD CONSTRAINT delivery_shipment_order_id_key UNIQUE (order_id);   -- OneToOne
ALTER TABLE delivery_shipment ADD CONSTRAINT shipment_tariff_nonneg CHECK (tariff IS NULL OR tariff >= 0);
ALTER TABLE delivery_shipment ADD CONSTRAINT shipment_state_fields CHECK (
  (state = 'not_created' AND tracking_number = '' AND bts_created_at IS NULL AND cancelled_at IS NULL)
  OR (state = 'failed' AND tracking_number = '' AND bts_created_at IS NULL AND cancelled_at IS NULL
      AND attempts >= 1 AND last_attempt_at IS NOT NULL)
  OR (state = 'created' AND tracking_number <> '' AND bts_created_at IS NOT NULL AND cancelled_at IS NULL AND tariff IS NOT NULL)
  OR (state = 'cancelled' AND tracking_number <> '' AND bts_created_at IS NOT NULL AND cancelled_at IS NOT NULL AND tariff IS NOT NULL));
CREATE INDEX shipment_state_idx    ON delivery_shipment (state);
CREATE INDEX shipment_tracking_idx ON delivery_shipment (tracking_number) WHERE tracking_number <> '';
```

**Derived API `pending`** — "true while a BTS create message for the order is queued or in flight". Outbox statuses
`pending` and `failed` are both re-claimable by the dispatcher (backoff retry), `in_flight` is leased, `done`/`dead`
are final; hence:
```sql
-- selectors.shipment_for_order / orders list annotation
SELECT s.*, COALESCE(m.status IN ('pending', 'failed', 'in_flight'), false) AS pending
FROM delivery_shipment s
LEFT JOIN core_outboxmessage m ON m.id = s.create_message_id
WHERE s.order_id = %(order_id)s;
```
ORM: `Shipment.objects.annotate(pending=Case(When(create_message__status__in=["pending", "failed", "in_flight"],
then=Value(True)), default=Value(False)))`. The FK (rather than a `payload->>'order_id'` scan) keeps the lookup an
index hit and survives outbox retention purges (`SET_NULL` → `pending = false`).

**API rendering of `state`** (`OrderSummary.shipment`):

| Order / row | API `state` | Other keys |
|---|---|---|
| `fulfilment = pickup` (no row) | `not_required` | — |
| row `not_created` | `not_created` | `pending` |
| row `failed` | `failed` | `last_attempt_at`, `attempts`, `pending` |
| row `created` | `created` | `tracking_number`, `tariff`, `updated_at = COALESCE(last_status_at, bts_created_at)`, `via` |
| row `cancelled` (internal) | `created` | same keys as created (the parcel existed; the `cancelled` order event carries `bts_cancelled: true`) — open point D-1 |

**Transition table** (all writes under the **Order** lock held by the caller in `commerce`)

| From | To | Trigger | Effects | Order event |
|---|---|---|---|---|
| — | `not_created` | checkout of a delivery order (`ensure_shipment(order_id)`) | row | — |
| `not_created`/`failed` | (enqueue) | payment confirmed (automatic) or `POST orders/{id}/retry-shipment/` (`request_bts_create(order_id, *, manual, actor_id)`) | guard: `pending` → 409 `conflict`; `create_requests += 1`; `outbox.enqueue("bts.create_shipment", {"order_id": id, "manual": bool, "request_no": n, "actor_id": actor_id}, dedupe_key=f"bts.create_shipment:{order_id}:{n}")`; `create_message = message` | — (202 with `pending: true`) |
| `not_created`/`failed` | `created` | handler: BTS lookup by `clientId = order id` **first**; if found, adopt it; else create (`mark_created(shipment, response, *, delivery_fee)`) | `tracking_number`, **`tariff = response.tariff if response.tariff is not None else delivery_fee`** (the order's quoted fee; the contract's `created` shape has `tariff: int`, so the column is never NULL in `created`/`cancelled` — review minor 13), `bts_order_id`, `bts_status` (from the create/lookup response), `via = polling`, `bts_created_at = last_status_at = now`, `attempts += 1`, `last_attempt_at` | `shipment_created {tracking_number, tariff, manual}` (source `admin` when `manual`, else `system`) |
| `not_created`/`failed` | `failed` | handler: BTS rejects (definitive) or the outbox gives up (`dead` after transport retries → `mark_failed` from the dead-letter hook) | `attempts += 1`, `last_attempt_at`, `last_error_code`, `last_error` | `shipment_failed {attempt: attempts, manual, error_code}`; dashboard `attention.bts_failed` |
| `created` | `created` | BTS webhook / polling status update (`apply_status(shipment, code, via, payload)`) | `bts_status`, `via`, `last_status_at`; `ShipmentEvent(status_received)`; mapping drives the order: code → `handed_to_courier` / `delivered` (commerce transitions the order and writes the event with `{via, bts_status, push_sent}`) | `handed_to_courier` / `delivered` (source `bts`) |
| `created` | `cancelled` | admin cancel: synchronous BTS cancel **succeeded** (`cancel(shipment)`) | `cancelled_at`; `ShipmentEvent(cancelled)` | part of `cancelled {bts_cancelled: true}` |
| `created` | `created` | admin cancel: BTS refuses (status ≠ 100) | raise `NotCancellable("BTS buyurtmani bekor qilmadi: posilka kuryerda.")`; transport failure → `ProviderUnavailable` (503) | — |
| `not_created`/`failed` | (unchanged) | admin cancel from `processing` without a parcel | nothing to cancel; the create handler **skips** when it later finds `order.status = cancelled` (marks the message done, writes no event) | — |
| `cancelled` | — | terminal | | |

Handler guard against at-least-once duplicates: BTS lookup by `clientId` before create; if the shipment row is
already `created` (a previous delivery committed but the message was not acked) the handler writes nothing and
returns. Plan §4.2's "Shipment row unique per order" is the `OneToOne`.

**Invariants (services)**
- `attempts` counts BTS answers (success or definitive failure); `create_requests` counts enqueues. A manual retry
  is refused while `pending` (409 `conflict`) and when the precondition fails (delivery ∧ `processing` ∧ `failed`,
  else 409 `invalid_transition`); both checks run under the Order lock so two concurrent retries cannot both enqueue.
- `available_actions.retry_shipment = fulfilment = delivery ∧ order.status = processing ∧ shipment.state = failed ∧
  ¬pending` — evaluated from the same annotated row.
- `tariff` from BTS may differ from `Order.delivery_fee` quoted at checkout; the order total is never changed by it
  (O2 territory; reported in the shipment card).

**Contract fields served** — `OrderSummary.shipment` union (table above) and the same object in `Order`;
`orders/?fulfilment=` is an `Order` column; export `buyurtmalar.BTS` = `tracking_number` or empty; dashboard
`attention.bts_failed` = `delivery.selectors.bts_failed_count()` = `Shipment.objects.filter(state="failed",
order__status="processing").count()` (SQL: `COUNT(*) FROM delivery_shipment s JOIN commerce_order o ON o.id =
s.order_id WHERE s.state = 'failed' AND o.status = 'processing'`) — allowed by the layer-reads rule of §0.1 because it
traverses `Shipment`'s own FK, not a reverse relation (review minor 15); `retry-shipment` 202 `shipment.pending =
true`; order events `shipment_created/shipment_failed` data.

**Open points**
- D-1 The contract `ShipmentState` has no `cancelled`; this design renders a cancelled parcel as `created` (with its
  tracking number) and lets the order's `cancelled` event tell the story. Alternative: add `cancelled` to the
  contract enum and the FE union (contract change, §7 delta).
- D-2 `via` for a freshly created parcel: the contract requires `via` on `created`; the create response is treated as
  a `polling` status read (`via = polling`, `last_status_at = bts_created_at`). Alternative: make `via` nullable in
  the contract until the first status update. Same neighbourhood: `tariff` falls back to `Order.delivery_fee` when
  the BTS create answer carries none (transition table above), so `created.tariff` is always an `int`.
- D-3 Recipient snapshot (name/phone/address) for the BTS create call is read from `Order.address_text` and
  `Customer` at handler time; if the customer is deleted (name scrubbed) before the handler runs, the parcel is
  created with the phone only. Store a recipient snapshot on `Order` (commerce group) if BTS requires a name.

### 3.2 `ShipmentEvent` (append-only)
Immutable BTS protocol trail of a shipment (creation answer, failures, every status received, cancellation), used
for diagnostics, polling de-duplication and future reconciliation; never rendered in the admin API (order events
are the customer-facing history).

```python
class ShipmentEvent(AppendOnlyModel):
    class Kind(models.TextChoices):  # internal
        CREATED = "created"
        CREATE_FAILED = "create_failed"
        STATUS_RECEIVED = "status_received"
        CANCELLED = "cancelled"
        CANCEL_REFUSED = "cancel_refused"

    shipment = models.ForeignKey("delivery.Shipment", on_delete=models.PROTECT, related_name="events")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    bts_status = models.CharField(max_length=16, blank=True)
    via = models.CharField(max_length=16, blank=True, choices=enums.ShipmentVia.choices)
    manual = models.BooleanField(default=False)  # the triggering create was a manual retry
    error_code = models.CharField(max_length=32, blank=True)
    payload = models.JSONField(
        default=dict, blank=True, encoder=DjangoJSONEncoder
    )  # BTS response / webhook body (PII-light: no customer name)
    at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        ordering = ["shipment_id", "at", "id"]
        indexes = [models.Index(fields=["shipment", "at", "id"], name="shipmentevent_shipment_at_idx")]

    def __str__(self) -> str:
        return f"Shipment {self.shipment_id}: {self.kind} {self.bts_status} ({self.at:%Y-%m-%d %H:%M})"
```
```sql
CREATE INDEX shipmentevent_shipment_at_idx ON delivery_shipmentevent (shipment_id, at, id);
-- + append-only trigger (0.2)
```
**Invariants** — one row per BTS answer; `apply_status` skips a webhook/poll whose `(bts_status, payload)` equals
the newest event's (idempotent polling), while `WebhookInbox` keeps the raw inbound record. **Contract fields
served** — none directly; `Shipment.via`/`bts_status`/`last_status_at` are the newest `status_received` row's
values. **Open points** — D-4 retention: these rows are never purged (small); the `glowderma.allow_purge` GUC applies
only to `WebhookInbox`/`AuditEntry`.

---

## 4. Selectors the contract needs (SQL-level)

### 4.1 Customer card totals (`loyalty.selectors.customer_totals(customer_id)`)
```sql
SELECT COALESCE(SUM(amount) FILTER (WHERE source = 'store_qr'), 0) AS store,
       COALESCE(SUM(amount) FILTER (WHERE source = 'online'),   0) AS online,
       COALESCE(SUM(amount), 0)                                   AS lifetime,
       COUNT(*)  FILTER (WHERE source = 'online')                 AS online_orders_count
FROM loyalty_purchase
WHERE customer_id = %(id)s AND reversed = false;
```
`lifetime = store + online` by construction (gross amounts incl. promo and cashback-paid parts, excl. delivery
fees). For the customer list the same sums are `Subquery` annotations (`lifetime_total`, used by
`ordering=±lifetime_total`, ties by `id`) over the partial index `purchase_live_cust_src_idx`.

### 4.2 Balance, held, available (`loyalty.selectors.account_summary(customer_id)`)
```sql
SELECT a.balance,
       COALESCE(h.held, 0)              AS held,
       a.balance - COALESCE(h.held, 0)  AS available
FROM loyalty_loyaltyaccount a
LEFT JOIN (SELECT account_id, SUM(amount) AS held FROM loyalty_cashbackhold
           WHERE state = 'active' GROUP BY account_id) h ON h.account_id = a.id
WHERE a.customer_id = %(id)s;
-- no account row → {0, 0, 0}
```
Inside a write (adjust/subtract, create_hold) the same numbers are computed after `SELECT … FOR UPDATE OF a`.

### 4.3 Tier and next tier (`loyalty.selectors.tier_for(lifetime, policy=active)`)
Pure Python over the active version's tiers (≤ a handful of rows, prefetched once per request):
```
tiers   = sorted(active.tiers, key=from)
current = first t with t.from ≤ lifetime < (t.to or ∞)          # None → tier = null, percent = null
nxt     = tiers[index(current) + 1] if current and index+1 < len(tiers) else None
tier    = {policy_version: active.version, percent: str(current.percent),
           next: nxt and {percent: str(nxt.percent), from: nxt.from,
                          remaining: nxt.from − lifetime,
                          progress: round((lifetime − current.from) / (nxt.from − current.from), 4)}}
```
Vector: lifetime 13 580 000 with seed tiers → `percent "1.00"`, `next {percent "2.00", from 20 000 000,
remaining 6 420 000, progress 0.679}`; 35 000 000 → `next = null`; 50 000 000 with a bounded last tier →
`tier = null`. `CustomerListItem.percent` = `current.percent` or `null` (no policy → `null`). Equivalent SQL for
reporting (not used by the API):
```sql
SELECT t.percent FROM loyalty_cashbacktier t
WHERE t.policy_id = %(active)s AND t.from_amount <= %(lifetime)s
  AND (t.to_amount IS NULL OR t.to_amount > %(lifetime)s)
ORDER BY t.from_amount LIMIT 1;
```

### 4.4 Ledger and purchases for the card / order
```sql
-- CustomerCard.ledger (full, newest first)
SELECT e.*, p.version AS policy_version, s.full_name, s.email
FROM loyalty_ledgerentry e
JOIN loyalty_loyaltyaccount a ON a.id = e.account_id
LEFT JOIN loyalty_cashbackpolicy p ON p.id = e.policy_id
LEFT JOIN accounts_staffuser s ON s.id = e.actor_id
WHERE a.customer_id = %(id)s
ORDER BY e.at DESC, e.id DESC;

-- Order.cashback (id ascending)
SELECT e.id, e.kind, e.amount, e.at, e.base, e.percent, e.reverses_id
FROM loyalty_ledgerentry e WHERE e.order_id = %(order_id)s ORDER BY e.id;

-- CustomerCard.purchases (newest first)
SELECT pu.*, p.version AS policy_version FROM loyalty_purchase pu
LEFT JOIN loyalty_cashbackpolicy p ON p.id = pu.policy_id
WHERE pu.customer_id = %(id)s ORDER BY pu.at DESC, pu.id DESC;
```

### 4.5 Seed consistency notes for the financial vectors (`seed_demo`)
The seeder reproduces only mutually consistent design numbers; the following are decided here (plan §4.3):
1. **Policy v3 `earn_on_cashback_paid = true`** so that order №10248's credit is "1% × 568 000 = 5 680" as drawn
   (`808 000 − 240 000`). With `false` the base would be 519 400 → 5 194. **Decided by plan §4.3** ("the 'keep as
   drawn' 568 000 base via a policy with `earn_on_cashback_paid` as seed data"), not an open point of this design
   (review minor 18): the contract §2.7 example (`earn_on_cashback_paid: false`) and the §6 bullet "the cashback-paid
   part does not earn" describe the *default* for new versions; v3 is seed data. Contract §7 delta line: "seed policy
   v3 sets `earn_on_cashback_paid = true`; the engine supports both". The contract's audit example
   `cashback.adjusted … balance_after 51550` (= 48 600 + 2 950) is likewise not reproducible against the seeded
   chain (the +2 950 adjustment lands on 2026-10-12 at balance 63 150) — listed as 03 §7.10 row 17.
2. Malika's card: the drawn ledger ends with l-12 (credit 2 310, 2026-10-21) at balance 48 600, while order №10248
   (2026-10-23) debits 48 600 and credits 5 680. The seed posts both: her ledger gains entries 120/121 and the card
   shows `balance = 5 680`, `held = 0`, `available = 5 680`, `totals {store 9 940 000, online 3 640 000, lifetime
   13 580 000}` (the purchases list is seeded to sum exactly to these), `tier.next.progress = 0.679`. The
   contract's "balance 48 600" card example is therefore not reproduced (G-3).
3. Order №10231 (l-10 debit −20 000, l-11 credit 3 140 on base 314 000 "aksiyasiz qism"): seeded with
   `items_subtotal = 314 000 + promo_subtotal`, `cashback_used = 20 000`, v3 rules (consistent under item 1).
4. The 56 generated customers with negative balances in the FE mock are **not** reproduced: generated ledgers only
   post adjustments/credits that keep `balance ≥ 0`; exactly one demo customer gets a deliberate negative balance
   (a cancelled order after the cashback was spent) to exercise L5.
5. `policy_version` on seeded entries follows the seeded `effective_from`s (June/August/October 2026), never a
   version that was not active at the entry's `at`.

---

## 5. Consolidated open points (this group)

| # | Point | Default in this design |
|---|---|---|
| G-1 | Migration order: `commerce.0001` must precede `loyalty/payments/delivery.0001` (FK direction), contrary to the plan's listed order | follow the FK graph; layering untouched |
| G-2 | Seed policy v3 `earn_on_cashback_paid = true` to keep the drawn 568 000 base | **decided (plan §4.3)**: true in seed; contract §7 delta line; examples are the default for new versions |
| G-3 | Malika's seeded balance becomes 5 680 (order №10248 entries included), not the card example's 48 600 | post both; tests assert the computed vectors |
| L-1 | `effective_from` any future instant vs month start | any future instant |
| L-2 | Supersession keeps the previous active version until the superseder starts | yes |
| L-3 | Tier non-overlap enforced in service only (gist exclusion would need `btree_gist` earlier) | service |
| L-5 | Lazy `LoyaltyAccount` creation + `COALESCE` in list ordering | lazy |
| L-6 / L-7 | Reversals carry no actor; adjustments require an actor | strict CHECKs |
| L-9 | `available` ignores `expires_at` (relies on the 1-min expiry job) | yes |
| L-10 / L-11 | Store-sale amount/promo detection; store reversals | M3 |
| L-13 | Record the resolving `PosDevice` on store purchases | audit only |
| P-1 | No `raw_*` columns on `Payment` (WebhookInbox is the raw store) | dropped |
| P-2 | `Payment` row created at checkout; provider switch = fail + new row; Payme state-1 transactions are never failed unilaterally (switch refused; expiry/admin cancel leave them `pending` for Payme's `CancelTransaction`); `payment_failed` event only on provider callbacks | checkout; decided (review major 7), contract §7 delta note |
| P-4 / P-5 | Admin retry/resolve endpoints for refunds; `manual_required → failed` | not in M1 contract |
| D-1 | Internal `cancelled` shipment rendered as `created` | render as created |
| D-2 | `via = polling` on creation; `tariff` falls back to `Order.delivery_fee` | polling; fallback, column NOT NULL in `created`/`cancelled` |
| D-3 | Recipient snapshot for BTS lives on `Order` | commerce group (03 C-3: `recipient_name/phone`) |

---

## 6. Django admin registrations (read-only, `core.admin.register_read_only`)

| Model | `list_display` | `list_filter` | `search_fields` | `exclude` |
|---|---|---|---|---|
| `CashbackPolicy` (+ `CashbackTier` inline, read-only) | version, effective_from, basis, earn_on_cashback_paid, author, created_at | basis | — | — |
| `LoyaltyAccount` | customer, balance, updated_at | — | customer phone | — |
| `LedgerEntry` | at, account, kind, amount, balance_after, source_type, source_ref, order, actor | kind, source_type | source_ref, order id, customer phone | — |
| `CashbackHold` | order, account, amount, state, expires_at, captured_at, released_at | state, release_reason | order id | — |
| `Purchase` (+ `PurchaseItem` inline) | id, customer, source, at, amount, cashback_amount, order, billz_sale_ref, reversed | source, reversed | billz_sale_id, billz_sale_ref, order id, customer phone | — |
| `PosDevice` | name, location_label, key_prefix, is_active, last_used_at, created_by | is_active | name | **`key_hash`** |
| `Payment` | id, order, provider, status, amount, provider_txn_id, confirmed_at, created_at | provider, status | provider_txn_id, order id | — (`provider_state` holds no credentials) |
| `Refund` (also inline on Payment) | id, payment, state, origin, amount, requested_at, completed_at, error_code | state, origin | provider_refund_id, payment id | — |
| `Shipment` (+ `ShipmentEvent` inline) | order, state, tracking_number, tariff, bts_status, via, attempts, last_attempt_at | state, via | tracking_number, order id | — |
| `ShipmentEvent` | at, shipment, kind, bts_status, via, error_code | kind, via | shipment id | — |

All append-only models hide the save buttons through `ReadOnlyAdmin`; `raw_id_fields` for every FK. The secret
column of this group is `PosDevice.key_hash` (review minor 20); `01-…` §11 and `03-…` §8 list the other groups.
