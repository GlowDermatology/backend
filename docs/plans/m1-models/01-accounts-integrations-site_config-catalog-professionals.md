# M1 data model — group g1: accounts, integrations, site_config, catalog, professionals

> Design document for slice B2 (`feat/m1-models`). It expands §5.3 of the approved plan into field-level Django
> models for the five lowest business modules. Nothing here is code yet; the implementer transcribes the fenced
> blocks, the migration list in §7 and the tests in §8. Sibling documents cover loyalty/payments/delivery/commerce
> (g2) and appointments/notifications/reporting (g3). Where this file and `docs/api/admin-api-v1.md` disagree, the
> contract wins and this file is fixed.

Inputs read for this design: `AGENTS.md`, `docs/api/admin-api-v1.md` (all sections), the design spec
("Financial integrity", "Provisional business rules", "M1 admin endpoint inventory", "M2 — Customer API"),
`docs/decisions/open-questions.md`, `glowderma/core/{models,db}.py`, `glowderma/accounts/models.py`,
`core/migrations/0002_auditentry_append_only.py`, and the approved plan §4 / §5.3. The SQL in §6 was executed
against the local PostgreSQL 16.15 inside a rolled-back transaction (extension, stored generated column, GIN
trgm index, ICU collation, `ORDER BY … COLLATE`, and the `LIKE` refusal) before being written down.

---

## 0. Conventions applied to every model in this file

| Rule | How it shows up below |
|---|---|
| Primary keys | `DEFAULT_AUTO_FIELD = BigAutoField` (already set in `config/settings/base.py`); no model declares `id` except the `SiteSettings` singleton. |
| Money | `BigIntegerField`, integer so'm. |
| Percent | `DecimalField(max_digits=5, decimal_places=2)` (not used by this group; loyalty owns percents). |
| FKs | Always string references `"app.Model"`; `on_delete=PROTECT` for any FK that points to or from an append-only row. **Actor FKs to `accounts.StaffUser`** (one rule for all three documents): `PROTECT` on append-only rows (required) and on business rows (`Job.created_by`, `Order.cancelled_by`, `Appointment.created_by`, `Campaign.author`, `Refund.requested_by`, `PosDevice.created_by`); `SET_NULL` on configuration/content rows (`Product.last_change_actor`, `ProductImage.uploaded_by`, `SiteSettings.updated_by`, `WorkingHoursOverride/BlockedPeriod.created_by`, `StatusTemplate.updated_by`, `CustomerSegment.created_by`, `SegmentMember.added_by`). Staff rows are never deleted (`deactivate_staff` flips `is_active`), so the distinction is documentation only. **Lower-layer services take scalar ids/values** (`order_id`, `customer_id`, amounts), never an upper-layer model instance (mypy needs a type, and a `TYPE_CHECKING` import of `commerce.models` would still break import-linter). |
| Append-only | Subclass `AppendOnlyModel` **and** a `RunSQL(*append_only_trigger_sql("<table>"))` migration. In this group: `integrations.WebhookInbox` only. |
| Timestamps | `TimeStampedModel` (`TS`) gives `created_at`/`updated_at`; every other `DateTimeField` that defaults to "now" uses `default=timestamp_default` from `glowderma.core.models` (never `auto_now*`, never `timezone.now` directly, so `monkeypatch.setattr(timeutils, "now", …)` works). |
| Text | `CharField`/`TextField`/`ImageField` never `null=True`; "absent" is `""` and the serializer renders `null` where the contract says `str \| null` (`support_phone`, `address`, block `label`). |
| i18n | Three columns `<field>_uz`, `<field>_ru`, `<field>_en`; `core.api.fields.I18nTextField` renders `{"uz","ru","en"}`. |
| Choices | `from glowderma.core import enums`; `choices=enums.<ContractEnumName>.choices` for every contract enum. States that are **not** contract enums are nested `TextChoices` on the model (`Device.Platform`, `SmsCode.Purpose`, `WebhookInbox.Provider`, `IntegrationState.Provider`; §9), the same convention as `02-…`/`03-…`; `core/enums.py` stays contract-only (its names feed `ENUM_NAME_OVERRIDES`). |
| Layer reads | A lower module's selector may traverse its **own** FK upward (`Payment.order__status`, `Shipment.order__customer__phone`) but never a reverse relation into an upper module's table (`catalog` never reads `commerce_stockreservation`). Stated once in `03-…` §0.1; applied in §4.3 below. |
| `__str__` | Every model. |
| Constraints | DB-level `CheckConstraint`/`UniqueConstraint` for invariants that must hold regardless of code path (seeders, Billz import `bulk_update`, raw SQL); everything else is a service invariant and is tested at the service level. |
| Media | `ImageField(upload_to=<module>.media.<kind>_path)` produces `MEDIA_ROOT/<kind>/<owner id>/<sha256[:16]>.<ext>`; URLs come from `core.media.absolute_media_url(name)` (`PUBLIC_MEDIA_BASE_URL`), never from the request host. |
| Layer | accounts → integrations → core is the migration/import order inside this group; `site_config` sits above `accounts`; `catalog` and `professionals` above `site_config` (import-linter layers in plan §4.1). The only upward migration dependency this group would have needed — `accounts.Customer`'s trigram index needing `pg_trgm` that §5.3 placed in `catalog` — is resolved by creating the extension in `core` (§6.1). |

Selectors mentioned below live in `<module>/selectors.py`; writes in `<module>/services.py`.

---

## 1. `accounts`

### 1.1 `Customer` (TS)

The loyalty identity of a person: one row per phone number for the lifetime of the business (soft delete + reactivation, decision A1).

```python
from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from glowderma.core import enums
from glowderma.core.models import TimeStampedModel, timestamp_default
from glowderma.accounts import media


class Customer(TimeStampedModel):
    phone = models.CharField(max_length=13, unique=True)  # E.164 "+998XXXXXXXXX"
    name = models.CharField(max_length=150, blank=True)  # "" after delete (scrubbed)
    language = models.CharField(
        max_length=2, choices=enums.ContentLanguage.choices, default=enums.ContentLanguage.UZ
    )
    status = models.CharField(
        max_length=8, choices=enums.CustomerStatus.choices, default=enums.CustomerStatus.ACTIVE, db_index=True
    )
    image = models.ImageField(upload_to=media.customer_image_path, blank=True, max_length=255)
    address_text = models.CharField(max_length=300, blank=True)  # denormalised default address
    registered_at = models.DateTimeField(default=timestamp_default, db_index=True)
    deleted_at = models.DateTimeField(null=True)
    reactivated_at = models.DateTimeField(null=True)  # last re-registration, if any
    last_login_at = models.DateTimeField(null=True)
    # M2 credentials (password login after OTP sign-up; never AbstractBaseUser — StaffUser is AUTH_USER_MODEL)
    password = models.CharField(max_length=128, blank=True)  # Django hasher format; "" = not set
    password_changed_at = models.DateTimeField(null=True)
    # M2 consent record (C3)
    terms_version = models.CharField(max_length=32, blank=True)
    consent_at = models.DateTimeField(null=True)
    consent_ip = models.GenericIPAddressField(null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(phone__regex=r"^\+998\d{9}$"), name="customer_phone_e164"),
            models.CheckConstraint(
                condition=(
                    Q(status=enums.CustomerStatus.ACTIVE, deleted_at__isnull=True)
                    | Q(status=enums.CustomerStatus.DELETED, deleted_at__isnull=False)
                ),
                name="customer_status_deleted_at_consistent",
            ),
            models.CheckConstraint(
                condition=Q(consent_at__isnull=True) | ~Q(terms_version=""),
                name="customer_consent_has_terms_version",
            ),
        ]
        indexes = [
            # expression index so that LOWER("name") LIKE '%' || LOWER(term) || '%' is index-assisted
            # (Django's icontains emits UPPER(name) LIKE UPPER(%s), which a plain column index would not serve).
            # Index.opclasses cannot be combined with an expression (ValueError at import); OpClass() is the
            # expression form: CREATE INDEX … USING gin (LOWER("name") gin_trgm_ops)
            GinIndex(OpClass(Lower("name"), name="gin_trgm_ops"), name="accounts_customer_name_trgm"),
            models.Index(fields=["status", "registered_at"], name="accounts_customer_status_reg"),
        ]
        ordering = ["-registered_at", "-id"]

    def __str__(self) -> str:
        return f"{self.phone} {self.name}".strip()
```

**Invariants**
- `phone` is the natural key for the whole system; it never changes and is never reused by another row (A1). Validation `^\+998\d{9}$` is both a DB check and a serializer rule (`PhoneField`).
- `accounts.services.delete_customer()` (M2 `/me` DELETE; service-level in B4 tests): sets `status=deleted`, `deleted_at=now`, `name=""`, `address_text=""`, deletes the image file and clears `image`, clears `password`, revokes every `AuthSession` of the customer (reason `deactivated`) and deactivates/removes `Device` rows. It never deletes the row: `loyalty.LoyaltyAccount`, `loyalty.Purchase`, `commerce.Order`, `appointments.Appointment` keep pointing at it (all `PROTECT`).
- `accounts.services.reactivate_customer()` (re-registration with the same phone): `status=active`, `deleted_at=None`, `reactivated_at=now`, keeps `registered_at` (first registration — so dashboard `new_customers` counts a person once; see Open points).
- `name` is a single free-text field (the app asks for one "Ism" field); the admin list orders by it with the default collation (not `glowderma_ci` — customers list ordering `name` is not in the contract's collation requirement; see Open points).
- `password` is set only through `accounts.services.set_customer_password()` using `django.contrib.auth.hashers.make_password` and checked with `check_password`; staff and customers never share a hasher configuration beyond `PASSWORD_HASHERS`.
- `language` defaults to `uz`; status-push templates and `Accept-Language` fallbacks read it.
- `registered_at` is settable by the seeder (historical customers); every other write path leaves the default.

**Contract fields served**
- `CustomerRef` everywhere (`id`, `name`, `phone`) — `name` is `""` once deleted (§7.16).
- §2.6 `GET customers/`: `id`, `name`, `phone`, `status`, `registered_at`; filters `search` (name: `.annotate(_name_l=Lower("name")).filter(_name_l__contains=Lower(Value(term)))` → `LOWER("name") LIKE '%' || LOWER('term') || '%'`, served by the expression GIN index above; phone: `phone__contains=digits` when the term has ≥3 digits — digits are extracted with `re.sub(r"\D", "", term)`; the two are `OR`ed), `status`; orderings `registered_at`, `name` (ties by `id`); `lifetime_total`/`balance` are annotations from loyalty (g2). Export `mijozlar`: `ID; Ism; Telefon; Ro‘yxatdan o‘tgan (fmt_dt registered_at); …; Holat ("Faol"/"O‘chirilgan")`.
- §2.6 `GET customers/{id}/`: `deleted_at`, `language`, `address` = `NULLIF(address_text, '')`.
- §2.10 dashboard: `new_customers` = `COUNT(*) WHERE registered_at ∈ [from, to)` in Tashkent day bounds (includes `deleted`); `total_customers` = `COUNT(*)` (all statuses); `deleted_customers` = `COUNT(*) WHERE status='deleted'`.
- §2.8 notifications: `audience.phone` must resolve to an `active` customer; segment rules and `total_devices` filter on `status='active'`.
- M2 `/me`, SMS sign-up/reset, consent, delete/reactivate.

**Open points**
- A2/A3 (OTP policy) do not change columns; A1 is implemented as described.
- `registered_at` on reactivation: kept (recommended) vs reset. Kept means the dashboard never double-counts a person; reset would make a reactivation look like a new sign-up. Lead to confirm; the column `reactivated_at` records the other date either way.
- The customers list `name` ordering: default collation (`C.UTF-8` in dev, `en_US.utf8` in the image) sorts Latin before Cyrillic and upper before lower. If the lead wants the ICU behaviour of products, the same `Collate(…, "glowderma_ci")` annotation applies; the collation exists DB-wide after `catalog 0002`, but `customers` (layer above) would then rely on a migration owned by `catalog` — acceptable as a migration dependency, not an import.
- `image` is in the skeleton; no M1/M2 endpoint uploads it yet (mobile profile photo is a Phase-2 candidate). Keep the column (cheap) or drop it: lead's call; the design keeps it.

### 1.2 `Device` (TS)

A mobile installation of one customer; carries the FCM token used by notifications and the facts shown on the customer card.

```python
class Device(TimeStampedModel):
    class Platform(models.TextChoices):  # internal (same literals as enums.OrderChannel)
        IOS = "ios"
        ANDROID = "android"

    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="devices")
    fid = models.CharField(max_length=128)  # Firebase installation id
    platform = models.CharField(max_length=8, choices=Platform.choices)  # ios | android
    model = models.CharField(max_length=80, blank=True)  # "iPhone 14"
    os = models.CharField(max_length=40, blank=True)  # "iOS 18.1"
    app_version = models.CharField(max_length=20, blank=True)  # "1.0.0"
    locale = models.CharField(max_length=8, blank=True)  # device locale as reported, informational
    fcm_token = models.CharField(max_length=512, blank=True)
    push_enabled = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)  # false after logout/replace/delete
    last_active_at = models.DateTimeField(default=timestamp_default)
    fcm_invalidated_at = models.DateTimeField(null=True)  # set when FCM reports the token dead

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "fid"], name="device_customer_fid_unique"),
            models.UniqueConstraint(
                fields=["fcm_token"],
                condition=Q(is_active=True) & ~Q(fcm_token=""),
                name="device_active_fcm_token_unique",
            ),
            models.CheckConstraint(
                condition=Q(push_enabled=False) | ~Q(fcm_token=""), name="device_push_requires_token"
            ),
        ]
        indexes = [
            models.Index(
                fields=["customer", "is_active", "last_active_at"], name="accounts_device_cust_active"
            ),
            models.Index(
                fields=["push_enabled"],
                condition=Q(is_active=True, push_enabled=True),
                name="accounts_device_pushable",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.platform} {self.model or self.fid} of customer {self.customer_id}"
```

**Invariants**
- One **active** device per customer is the M2 rule ("single active device"): `accounts.services.register_device()` runs under `select_for_update()` on the customer row, marks every other device `is_active=False`, upserts by `(customer, fid)`, and — before saving a non-empty `fcm_token` — deactivates any other active row holding the same token (a phone that switched accounts). The partial unique constraint is the backstop.
- `push_enabled=True` requires a token (DB check). `fcm_invalidated_at` is set by the FCM adapter's prune path (M3) and clears `push_enabled` in the same update.
- `last_active_at` is bumped by the customer authentication class at most once per 5 minutes (write amplification guard), not on every request.
- The CASCADE from `Customer` is theoretical (customers are never hard-deleted); `notifications.CampaignRecipient.device` is `SET_NULL` so a real cascade would still be safe.

**Contract fields served**
- §2.6 `CustomerCard.device` = the active device with the greatest `last_active_at` (`null` if none): `model`, `os`, `app_version`, `last_active_at`, `push_enabled`.
- §2.8 `meta.total_devices` and `recipient_count`: `Device.objects.filter(is_active=True, push_enabled=True, customer__status="active")` (plus segment/customer predicates).
- §2.8 segment `size` = push-enabled devices of members.
- `push_sent` on order/appointment events (plan §4.3): "the customer had a push-enabled device at event time" = the same predicate evaluated inside the transaction.

**Open points**
- `Device.Platform` is a nested internal enum (§9; review minor 23). Its literals equal `enums.OrderChannel`'s, and checkout derives `Order.channel` from it (`enums.OrderChannel(device.platform)`); if the lead prefers one enum, `choices=enums.OrderChannel.choices` is a one-line change.

### 1.3 `Address` (M2, TS)

A saved delivery address of a customer; the checkout snapshots its text into `commerce.Order.address_text`.

```python
class Address(TimeStampedModel):
    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="addresses")
    label = models.CharField(max_length=60, blank=True)  # "Uy", "Ish"
    text = models.CharField(max_length=300)  # full human-readable address
    region_code = models.CharField(max_length=16, blank=True)  # BTS region id (M2 tariff/create)
    city_code = models.CharField(max_length=16, blank=True)  # BTS city id
    comment = models.CharField(max_length=200, blank=True)  # entrance, floor, intercom
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["customer"], condition=Q(is_default=True), name="address_one_default_per_customer"
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="address_latlng_pair",
            ),
        ]
        ordering = ["-is_default", "-updated_at"]

    def __str__(self) -> str:
        return f"{self.label or 'address'} #{self.pk} of customer {self.customer_id}"
```

**Invariants**
- `accounts.services.set_default_address()` clears the previous default in the same transaction (the partial unique forbids two).
- As built (B2): the address write path is `accounts.services.add_address / update_address / set_default_address / remove_address`; every one of them recomputes `Customer.address_text`, and M2's `PATCH /me/addresses/{id}` (B7) must go through `update_address` rather than adding a second path.
- Saving/creating/deleting an address recomputes `Customer.address_text` = default address `text` (or the most recently updated one, or `""`), so the admin card needs no join. `delete_customer()` deletes `Address` rows (PII scrub).

**Contract fields served**
- §2.6 `CustomerCard.address` (through `Customer.address_text`); M2 `/me/addresses`, checkout.

**Open points**
- BTS geo fields (`region_code`, `city_code`) are placeholders until the BTS tariff contract is read in B7; they may become a FK to a BTS reference table.

### 1.4 `SmsCode` (M2)

One OTP issuance for a phone and purpose; also carries the single-use `verification_token` that the verified code unlocks.

```python
class SmsCode(models.Model):
    class Purpose(models.TextChoices):  # internal
        SIGNUP = "signup"
        RESET = "reset"
        NEW_DEVICE = "new_device"
        SOCIAL_LINK = "social_link"

    phone = models.CharField(max_length=13)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code_hmac = models.CharField(max_length=64)  # HMAC-SHA256(code, SMS_CODE_HMAC_KEY), hex
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    created_at = models.DateTimeField(default=timestamp_default)
    expires_at = models.DateTimeField()  # created_at + 120 s
    sent_at = models.DateTimeField(null=True)  # claimed by the sms.send handler before the Eskiz call (03 §5)
    consumed_at = models.DateTimeField(null=True)  # verified successfully
    exhausted_at = models.DateTimeField(null=True)  # attempts reached max_attempts
    verification_token_hash = models.CharField(max_length=64, blank=True)  # SHA-256 of the issued token
    verification_expires_at = models.DateTimeField(null=True)  # consumed_at + 10 min
    verification_used_at = models.DateTimeField(null=True)  # single use
    request_ip = models.GenericIPAddressField(null=True)
    provider_message_id = models.CharField(max_length=64, blank=True)  # Eskiz message id (fake: "fake-<id>")

    class Meta:
        indexes = [
            models.Index(fields=["phone", "purpose", "created_at"], name="accounts_smscode_phone_purpose"),
            models.Index(fields=["expires_at"], name="accounts_smscode_expires"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(phone__regex=r"^\+998\d{9}$"), name="smscode_phone_e164"),
            models.CheckConstraint(
                condition=Q(attempts__lte=models.F("max_attempts")), name="smscode_attempts_bound"
            ),
            models.UniqueConstraint(
                fields=["verification_token_hash"],
                condition=~Q(verification_token_hash=""),
                name="smscode_verification_token_unique",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} code for {self.phone} ({self.created_at:%Y-%m-%d %H:%M})"
```

**Invariants** (all in `accounts.services`, M2)
- Issue: refuse when the latest row for `(phone, purpose)` is younger than 60 s (resend window) or when ≥10 rows exist for the phone in the current Tashkent day (A3 cap), or when a row with `exhausted_at > now − 15 min` exists (A3 lockout). Issuing a new code expires the previous unconsumed one (`expires_at = now`). The SMS is sent through the outbox topic `sms.send` with `dedupe_key=f"sms.send:{sms_code.id}"` (03 §5); the handler sets `sent_at` in its own short transaction **before** calling Eskiz (claim), then writes `provider_message_id`; a redelivery that finds `sent_at` set does not resend (the code expires after 120 s and the user can ask for a resend after 60 s).
- Verify: `select_for_update()` the row; reject when expired/consumed/exhausted; compare with `hmac.compare_digest`; on mismatch `attempts += 1` and set `exhausted_at` when the max is reached; on match set `consumed_at`, generate a 256-bit token, store its SHA-256 and `verification_expires_at`, return the token once.
- Use of the verification token (`set-password`, `reset-password`, `login` on a new device, social link): look up by hash, require `verification_used_at IS NULL` and not expired, bind to the same `(phone, purpose)`; set `verification_used_at` under lock.
- Cleanup beat task deletes rows older than 24 h (not append-only; no trigger).

**Contract fields served**: none in the admin API; M2 auth endpoints. Throttle counters live in Redis, the day cap and lockout are derived from these rows so they survive a cache flush.

**Open points**: A3 values (5 attempts, 15 min, 10/day) stay settings-driven (`CUSTOMER_OTP_*`); `max_attempts` is stored per row so a later policy change does not alter the past.

### 1.5 `CustomerSocialIdentity` (M2)

Links a Google/Facebook identity to a customer (decision A6). A separate table rather than an extension of the staff `SocialIdentity` so that the same Google account can belong to both a staff member and a customer, and so that the M0b model and its two unique constraints stay untouched.

```python
class CustomerSocialIdentity(models.Model):
    customer = models.ForeignKey(
        "accounts.Customer", on_delete=models.CASCADE, related_name="social_identities"
    )
    provider = models.CharField(max_length=16, choices=[("google", "google"), ("facebook", "facebook")])
    subject = models.CharField(max_length=254)
    email_at_link = models.EmailField(max_length=254, blank=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    last_login_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "subject"], name="customer_social_provider_subject_unique"
            ),
            models.UniqueConstraint(
                fields=["customer", "provider"], name="customer_social_customer_provider_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.provider}: {self.subject} → customer {self.customer_id}"
```

The provider choices reuse the same literal pair as `accounts.SocialIdentity` (there is no contract enum for it; M0b hard-codes the pair). If B1 introduces `enums.OAuthProvider`, both models switch to it.

**Invariants**: an unknown identity never creates a customer by itself — it yields `next_step: verify_phone` + a signed `social_ticket`; after `verify-code` the link is created for that phone's (created or reactivated) customer. `delete_customer()` deletes these rows (the social link is PII and would otherwise log a deleted account straight back in).

**Contract fields served**: none in admin v1; M2 `POST /api/v1/auth/social`.

### 1.6 `AuthSession` — changes

Existing model (`accounts/models.py`). Three changes, one migration (`accounts 0005`):

```python
class AuthSession(models.Model):
    ...
    staff = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="sessions"
    )
    customer = models.ForeignKey(
        "accounts.Customer", null=True, on_delete=models.PROTECT, related_name="sessions"
    )
    device = models.ForeignKey(
        "accounts.Device", null=True, on_delete=models.SET_NULL, related_name="sessions"
    )  # M2
    ...
    # M2 rotating refresh (customers only; staff refresh is non-rotating and leaves these empty)
    previous_refresh_hash = models.CharField(max_length=64, blank=True)
    rotated_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=["customer", "revoked_at"], name="accounts_authsession_cust_rev"),
            models.Index(fields=["staff", "revoked_at"], name="accounts_authsession_staff_rev"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(audience="staff", staff__isnull=False, customer__isnull=True)
                    | Q(audience="customer", customer__isnull=False, staff__isnull=True)
                ),
                name="authsession_subject_matches_audience",
            ),
        ]
```

- `customer_id = BigIntegerField(null=True)` becomes `ForeignKey("accounts.Customer")` with the **same column name**, so the migration is an `AlterField` (adds the FK constraint and index; no data copy). The migration asserts in a `RunPython` precheck that no non-null `customer_id` exists yet (none can: customer auth does not exist before M2); if one did, the FK creation would fail loudly, which is the desired behaviour.
- `on_delete=PROTECT` matches the skeleton; customers are soft-deleted so it never fires.
- The audience/subject check makes the "inactive staff / deleted customer rejected" rule a lookup, never a guess.
- `previous_refresh_hash` + `rotated_at` implement "reuse after a short grace window revokes the session" (spec M2): a refresh presenting `previous_refresh_hash` within `CUSTOMER_REFRESH_GRACE_SECONDS` of `rotated_at` replays the current token; after the window it revokes the session with reason `replaced` and the client gets `SESSION_REPLACED`.

**Contract fields served**: §2.1 (unchanged for staff); `auth/me.session_expires_at`.

---

## 2. `integrations`

### 2.1 `WebhookInbox` (AO)

Immutable record of every inbound provider call (Click, Payme, BTS; Billz callbacks if they ever exist) together with how it was answered.

```python
from django.core.serializers.json import DjangoJSONEncoder

from glowderma.core.models import AppendOnlyModel, timestamp_default


class WebhookInbox(AppendOnlyModel):
    class Provider(models.TextChoices):  # internal
        CLICK = "click"
        PAYME = "payme"
        BTS = "bts"
        BILLZ = "billz"

    provider = models.CharField(max_length=16, choices=Provider.choices)
    event_key = models.CharField(max_length=160)  # provider-specific natural key of the delivery (below)
    received_at = models.DateTimeField(default=timestamp_default)
    remote_ip = models.GenericIPAddressField(null=True)
    request_id = models.CharField(max_length=64, blank=True)
    method = models.CharField(max_length=8)  # HTTP method
    path = models.CharField(max_length=200)
    headers = models.JSONField(
        encoder=DjangoJSONEncoder, default=dict
    )  # allow-listed headers only (no Authorization)
    payload = models.JSONField(
        encoder=DjangoJSONEncoder, null=True
    )  # parsed body (form or JSON); null if unparsable
    raw_body = models.TextField(blank=True)  # verbatim body (≤64 KiB; longer bodies are truncated + flagged)
    entity_ref = models.CharField(
        max_length=64, blank=True
    )  # "order:10248", "payment:301", "shipment:17" — no FK upward
    result = models.JSONField(
        encoder=DjangoJSONEncoder
    )  # {"status": 200, "body": {...}, "outcome": "accepted|duplicate|rejected|error", "error_code": "-31008"|null}
    processing_ms = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "event_key"], name="webhookinbox_provider_event_unique"
            ),
        ]
        indexes = [
            models.Index(fields=["provider", "received_at"], name="integr_webhook_provider_time"),
            models.Index(fields=["entity_ref"], name="integr_webhook_entity_ref"),
        ]
        ordering = ["-received_at", "-id"]

    def __str__(self) -> str:
        return f"{self.provider} {self.event_key} → {self.result.get('status') if self.result else '?'}"
```

Trigger migration: `RunSQL(*append_only_trigger_sql("integrations_webhookinbox"))` (`integrations 0002`).

**Invariants**
- **Insert after processing, in the same transaction** (skeleton): the webhook view opens `transaction.atomic()`, takes `SELECT pg_advisory_xact_lock(hashtext(%s))` with `f"{provider}:{event_key}"` (serialises concurrent duplicate deliveries without blocking unrelated ones), looks the key up — if a row exists, it **replays `result.body` with `result.status`** and processes nothing (`outcome` of the new attempt is not recorded; duplicates are counted in logs only) — otherwise runs the protocol handler (payments/delivery services), then inserts the row with the computed `result`. An exception rolls back both the effect and the row; the provider retries. The unique constraint is the backstop should the advisory lock ever be skipped.
- `event_key` per provider (protocol modules in M2/M3 own the exact strings; they must be stable across retries of the same logical delivery and distinct across different ones): Click `f"{action}:{click_trans_id}:{merchant_trans_id}"` (prepare and complete are separate keys); Payme `f"{method}:{params.id}"` for transaction-bound methods and `f"{method}:{uuid4}"` for stateless queries (`CheckPerformTransaction`, `GetStatement`) which are recorded but never deduplicated; BTS `f"{tracking_number}:{status}:{event_time}"`.
- `headers` is an allow-list (`Content-Type`, `User-Agent`, `X-Forwarded-For`, provider signature headers); credentials (`Authorization` of Payme basic auth, BTS secret token) are never stored.
- Rows are never updated. Retention (C4): the purge task (core, plan §5.3 "allow_purge" GUC) deletes rows older than `WEBHOOK_INBOX_RETENTION_DAYS` (default 180) after setting `SET LOCAL glowderma.allow_purge = on`; `AppendOnlyQuerySet.delete()` still raises, so the purge uses `connection.cursor()` with a plain `DELETE … WHERE received_at < %s` and writes an `AuditEntry` `integrations.inbox_purged {provider, rows, before}`.
- No FK to commerce/payments/delivery (upper layers): `entity_ref` is a string the M2 views fill once the order/payment is identified. Reporting joins on it when needed.

**Contract fields served**: none directly in admin v1 (not exposed). It backs: the "crash after provider success" tests, the Payme/Click sandbox replay tests, and support investigations (`django-admin` read-only list filtered by `entity_ref`).

**Open points**: retention default (C4) — 180 days proposed; `raw_body` size cap 64 KiB proposed.

### 2.2 `Job` (TS)

A background job the admin can start and poll (`billz_import` in M1; the model is generic).

```python
class Job(TimeStampedModel):
    kind = models.CharField(max_length=32, choices=enums.JobKind.choices)
    state = models.CharField(
        max_length=16, choices=enums.JobState.choices, default=enums.JobState.QUEUED, db_index=True
    )
    params = models.JSONField(encoder=DjangoJSONEncoder, default=dict)  # {"mode": "delta"|"full"}
    progress = models.JSONField(encoder=DjangoJSONEncoder, null=True)  # {"done": 120, "total": 248} | null
    result = models.JSONField(
        encoder=DjangoJSONEncoder, null=True
    )  # {"created","updated","unchanged","new_from_billz"}
    error = models.JSONField(
        encoder=DjangoJSONEncoder, null=True
    )  # {"code": "provider_unavailable", "message": "…"}
    started_at = models.DateTimeField(null=True)
    finished_at = models.DateTimeField(null=True)
    heartbeat_at = models.DateTimeField(null=True)  # worker touches it every batch
    attempts = models.PositiveSmallIntegerField(default=0)  # worker pickups (celery redelivery)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.PROTECT, related_name="jobs"
    )
    celery_task_id = models.CharField(max_length=36, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kind"],
                condition=Q(state__in=[enums.JobState.QUEUED, enums.JobState.RUNNING]),
                name="job_single_flight_per_kind",
            ),
            models.CheckConstraint(
                condition=(
                    Q(state=enums.JobState.QUEUED, started_at__isnull=True, finished_at__isnull=True)
                    | Q(state=enums.JobState.RUNNING, started_at__isnull=False, finished_at__isnull=True)
                    | Q(
                        state__in=[enums.JobState.SUCCEEDED, enums.JobState.FAILED],
                        started_at__isnull=False,
                        finished_at__isnull=False,
                    )
                ),
                name="job_state_timestamps_consistent",
            ),
            models.CheckConstraint(
                condition=~Q(state=enums.JobState.FAILED) | Q(error__isnull=False),
                name="job_failed_has_error",
            ),
        ]
        indexes = [models.Index(fields=["kind", "created_at"], name="integr_job_kind_created")]
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return f"{self.kind} #{self.pk} ({self.state})"
```

**Invariants**
- State machine `queued → running → succeeded | failed` (plan §5.3). Transitions only in `integrations.services`: `start_job(kind, params, actor)` inserts `queued` inside a savepoint; on `IntegrityError` from `job_single_flight_per_kind` it re-selects the active job and returns it (**single-flight**, contract §2.3) — the response is 202 with that job either way. The Celery task is sent `on_commit`; `celery_task_id` is written back by the same on-commit hook.
- The worker: `mark_running()` (`select_for_update`, `attempts += 1`, `started_at`, `heartbeat_at`), batches update `progress` + `heartbeat_at`, then `finish(result)` / `fail(error)` set `finished_at`. A beat task `integrations.reap_stale_jobs` (every 5 min) fails any `running` job whose `heartbeat_at < now − 15 min` with `error={"code": "worker_lost", "message": …}` so the single-flight constraint can never wedge the import button.
- `result`/`error` shapes are the contract's §2.14 objects verbatim; `progress` is `null` until the adapter reports a total.
- `created_by` is `null` for jobs started by beat (M3 delta sync); the API renders `null` actor as system.

**Contract fields served**
- §2.14 `GET jobs/{id}/`: every key maps 1:1 (`created_by` → `Actor`).
- §2.3 `POST integrations/billz/import/` (202 `Job`), `products/stats.billz.connected` = adapter configured ∧ the most recent `billz_import` job (by `created_at`, any state) is not `failed` (`not Job.objects.filter(kind="billz_import").order_by("-created_at","-id").values_list("state")[:1] == ["failed"]`).
- Audit `billz.import_started` (service) / `billz.import_finished` (worker, actor system).

**Open points**: stale-job threshold (15 min) and whether a failed job should block a new one until acknowledged (design: it does not).

### 2.3 `IntegrationState`

One row per provider with the last-sync facts the shell shows; never holds secrets.

```python
class IntegrationState(models.Model):
    class Provider(models.TextChoices):  # internal
        BILLZ = "billz"
        BTS = "bts"
        CLICK = "click"
        PAYME = "payme"
        ESKIZ = "eskiz"
        FCM = "fcm"

    provider = models.CharField(max_length=16, choices=Provider.choices, unique=True)
    last_sync_at = models.DateTimeField(null=True)  # last successful full/delta sync (billz) or poll (bts)
    last_success_at = models.DateTimeField(null=True)  # last successful call of any kind
    last_error_at = models.DateTimeField(null=True)
    last_error = models.TextField(blank=True)  # trimmed message of the last failure
    consecutive_failures = models.PositiveIntegerField(default=0)
    data = models.JSONField(
        encoder=DjangoJSONEncoder, default=dict
    )  # provider cursors: billz {"delta_since": iso, "catalog_version": …}, bts {"last_poll_at": iso}
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        verbose_name = "integration state"

    def __str__(self) -> str:
        return f"{self.provider}: synced {self.last_sync_at or 'never'}"
```

**Invariants**
- Rows are created lazily by `integrations.services.get_state(provider)` (`get_or_create`) and by the data migration that seeds the six providers (so GETs never write).
- Written only by adapters' outcome hooks (`record_success`, `record_failure`) and by the Billz import worker (`last_sync_at` on success). `updated_at` is set by the service (plain model, not TS, to keep the row free of `save()` overrides used from `update()`).
- `INTEGRATIONS_MODE`/`<PROVIDER>_MODE` are settings, never stored here; `settings/.integrations.mode` is read from settings.

**Contract fields served**
- §2.3 `products/stats.billz.last_sync_at` (`null` = never synced); §5.2 `lastSyncLabel`, sidebar Billz block.
- §2.15 `health?ready=1` provider checks (M3) read `consecutive_failures`.

**Open points**: none.

---

## 3. `site_config`

### 3.1 `SiteSettings` (singleton, pk = 1)

Typed, admin-editable business settings; one row forever.

```python
class SiteSettings(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    # §2.11
    reservation_minutes = models.PositiveSmallIntegerField(default=15)  # O3; 5..60
    support_phone = models.CharField(max_length=13, blank=True)  # "" ↔ null in the API
    pickup_address_uz = models.CharField(max_length=200, blank=True)
    pickup_address_ru = models.CharField(max_length=200, blank=True)
    pickup_address_en = models.CharField(max_length=200, blank=True)
    # Admin-panel PDF settings list (A1 Settings screen, M2 /settings/public)
    cosmetologist_phone = models.CharField(max_length=13, blank=True)
    store_phone = models.CharField(max_length=13, blank=True)
    telegram_url = models.URLField(max_length=200, blank=True)
    instagram_url = models.URLField(max_length=200, blank=True)
    website_url = models.URLField(max_length=200, blank=True)
    privacy_policy_uz = models.TextField(blank=True)
    privacy_policy_ru = models.TextField(blank=True)
    privacy_policy_en = models.TextField(blank=True)
    terms_uz = models.TextField(blank=True)
    terms_ru = models.TextField(blank=True)
    terms_en = models.TextField(blank=True)
    terms_version = models.CharField(max_length=32, blank=True)  # C3: stamped on Customer.consent
    terms_updated_at = models.DateTimeField(null=True)
    booking_policy_uz = models.TextField(blank=True)  # P3 free-text cancellation/reschedule terms
    booking_policy_ru = models.TextField(blank=True)
    booking_policy_en = models.TextField(blank=True)
    # bookkeeping
    updated_at = models.DateTimeField(default=timestamp_default)
    updated_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "site settings"
        constraints = [
            models.CheckConstraint(condition=Q(id=1), name="sitesettings_singleton"),
            models.CheckConstraint(
                condition=Q(reservation_minutes__gte=5, reservation_minutes__lte=60),
                name="sitesettings_reservation_5_60",
            ),
            models.CheckConstraint(
                condition=Q(support_phone="") | Q(support_phone__regex=r"^\+998\d{9}$"),
                name="sitesettings_support_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(cosmetologist_phone="") | Q(cosmetologist_phone__regex=r"^\+998\d{9}$"),
                name="sitesettings_cosmetologist_phone_e164",
            ),
            models.CheckConstraint(
                condition=Q(store_phone="") | Q(store_phone__regex=r"^\+998\d{9}$"),
                name="sitesettings_store_phone_e164",
            ),
        ]

    def __str__(self) -> str:
        return "Site settings"

    @classmethod
    def load(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
```

**Invariants**
- The row is created by a `RunPython` data migration (`site_config 0002`), so `GET settings/` never writes; `load()` is the only read entry point (`site_config.selectors.get_settings()` wraps it and caches for 30 s under key `gd:site_settings`, invalidated on commit by the update service).
- `site_config.services.update_settings(actor, changes)` runs `select_for_update()` on pk 1, applies validated fields, sets `updated_at`/`updated_by`, writes audit `settings.updated` with the field diff (`before`/`after` restricted to changed keys). Changing any `terms_*` text through the A1 screen bumps `terms_version` (`YYYYMMDD-n`) and `terms_updated_at` unless the staff explicitly keeps the version (checkbox in A1; service parameter `bump_terms_version=True`).
- Commerce reads `reservation_minutes` at checkout time and stores the resulting `expires_at` on the reservation/order; a later change never alters existing reservations (open question O3 stays data-driven).
- `updated_by` is `SET_NULL` (not an append-only relation); staff are never deleted in practice anyway.

**Contract fields served**
- §2.11 `GET/PATCH settings/`: `reservation_minutes`, `support_phone` (`NULLIF(support_phone, '')`), `pickup_address` (`I18nTextField` over the three columns), `integrations.mode` (settings, read-only), `updated_at`, `updated_by` → `Actor | null`. PATCH rules (5..60, E.164 or `null` → `""`, `pickup_address.* ≤ 200`) are serializer rules backed by the DB checks.
- A1 Settings screen and M2 `GET /api/v1/settings/public`: every other column (phones, links, pickup address, privacy policy, terms, booking policy, `terms_version`).
- Checkout `reservation_minutes` → `OrderEvent.reservation_created.data.minutes`.

**Open points**
- Whether terms/privacy texts belong in settings (one blob per language) or in a versioned `LegalDocument` table. For M1/M2 the singleton with `terms_version` satisfies C3 (the version string is what the consent record stores); a versioned table can be added later without touching `Customer`.
- Phone validation for the three phone fields assumes Uzbek numbers; if a landline/short number is needed (`+99871…` fits E.164 already) no change; a non-E.164 "short number" would need the checks relaxed.

---

## 4. `catalog`

### 4.1 `Category` (TS)

A product grouping addressed by immutable slug.

```python
class Category(TimeStampedModel):
    slug = models.SlugField(max_length=40, unique=True)  # ^[a-z0-9-]{2,40}$, never "order"
    name_uz = models.CharField(max_length=60)
    name_ru = models.CharField(max_length=60, blank=True)
    name_en = models.CharField(max_length=60, blank=True)
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    image = models.ImageField(upload_to=media.category_image_path, blank=True, max_length=255)

    class Meta:
        verbose_name_plural = "categories"
        constraints = [
            models.CheckConstraint(
                condition=Q(slug__regex=r"^[a-z0-9-]{2,40}$"), name="category_slug_format"
            ),
            models.CheckConstraint(condition=~Q(slug="order"), name="category_slug_not_reserved"),
            models.CheckConstraint(condition=~Q(name_uz=""), name="category_name_uz_required"),
        ]
        indexes = [models.Index(fields=["position", "id"], name="catalog_category_position")]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.slug}: {self.name_uz}"
```

**Invariants**
- `slug` is immutable after create (`read_only` in PATCH; the service never writes it; `SlugField` does not use `glowderma_ci` — slugs are ASCII and compared with `=`).
- `position` is dense `0..n−1` in the intended order but **not unique at DB level**: `create_category()` appends with `max(position)+1` under `pg_advisory_xact_lock(hashtext('catalog_category_position'))`; `reorder_categories(slugs)` validates the exact current set (`mismatch`), then assigns `0..n−1` under the same lock. Ordering reads are always `("position", "id")` so a duplicate position (only possible through a seeder bug) is still deterministic.
- `delete_category()` refuses (409 `conflict`) when any product references it, resets `Banner.link_category` links to `kind=none` in the same transaction (§4.5), then deletes and compacts positions. `Product.category` is `SET_NULL` as a last resort only.
- `product_count` counts **all** products of the category regardless of status (hidden ones would be orphaned by a delete too).

**Contract fields served**
- §2.4 `Category`: `slug`, `name` (i18n), `position`, `is_active`, `product_count` = `Count("products")` annotation, `created_at`, `updated_at`; list ordered by `position`; filter `is_active`. Export `kategoriyalar`: `Slug; Nomi (name_uz); Tartib; Faol (Ha/Yo‘q); Mahsulotlar (product_count)`.
- §2.3 `products/?category=<slug>|none` (`category__slug=` / `category__isnull=True`; unknown slug → 400 `invalid_choice` after a lookup); export column `Kategoriya` = `category.name_uz` or `""`.
- §5.2 category labels (`name.uz` replaces `CATEGORY_LABEL`); §2.5 `link.category`; M2 public categories (`is_active=True`).

**Open points**: whether hidden categories (`is_active=False`) should hide their products in the customer catalog (M2 proposal: yes, product shown only if its category is active or null).

### 4.2 `Product` (TS)

A sellable item: Billz owns `billz_*`, `volume` and stock; Glow Derma owns texts, images, visibility, promo flag and (provisionally, O6) price.

```python
from django.db.models import F, Func, TextField, Value
from django.db.models.functions import Coalesce, Lower


class JoinText(Func):
    """a || b || c … — immutable text concatenation usable in a stored generated column
    (PostgreSQL's CONCAT(), which Django's Concat emits, is only STABLE and is refused there)."""

    arg_joiner = " || "
    template = "(%(expressions)s)"
    output_field = TextField()


class Product(TimeStampedModel):
    # Billz-owned (read-only in the admin API)
    billz_id = models.CharField(max_length=14, unique=True)  # ^\d{6,14}$ (Billz product id or barcode)
    billz_name = models.CharField(max_length=255, blank=True)
    volume = models.CharField(max_length=40, blank=True)  # "30 ml"
    billz_price = models.BigIntegerField(null=True)  # reference only (O6)
    billz_synced_at = models.DateTimeField(null=True)  # last time billz_* were refreshed
    # Glow Derma-owned
    price = models.BigIntegerField(default=0)  # admin price; API requires 1000..100_000_000
    category = models.ForeignKey(
        "catalog.Category", null=True, on_delete=models.SET_NULL, related_name="products"
    )
    promo = models.BooleanField(default=False)
    status = models.CharField(
        max_length=8, choices=enums.ProductStatus.choices, default=enums.ProductStatus.HIDDEN, db_index=True
    )
    is_new_from_billz = models.BooleanField(default=False)
    name_uz = models.CharField(max_length=120, blank=True)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    description_uz = models.TextField(blank=True)  # ≤2000 (serializer)
    description_ru = models.TextField(blank=True)
    description_en = models.TextField(blank=True)
    ingredients_uz = models.TextField(blank=True)  # ≤1000
    ingredients_ru = models.TextField(blank=True)
    ingredients_en = models.TextField(blank=True)
    usage_uz = models.TextField(blank=True)  # ≤1000
    usage_ru = models.TextField(blank=True)
    usage_en = models.TextField(blank=True)
    # search (stored generated column, see §6.2)
    search_text = models.GeneratedField(
        expression=Lower(
            JoinText(
                F("name_uz"),
                Value(" "),
                F("name_ru"),
                Value(" "),
                F("name_en"),
                Value(" "),
                F("billz_name"),
                Value(" "),
                F("billz_id"),
            )
        ),
        output_field=models.TextField(),
        db_persist=True,
    )
    # last admin change (contract last_change; null when never edited through the API)
    last_change_at = models.DateTimeField(null=True)
    last_change_kind = models.CharField(max_length=16, choices=enums.ProductChangeKind.choices, blank=True)
    last_change_old_price = models.BigIntegerField(null=True)
    last_change_new_price = models.BigIntegerField(null=True)
    last_change_actor = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(billz_id__regex=r"^\d{6,14}$"), name="product_billz_id_format"
            ),
            models.CheckConstraint(condition=Q(price__gte=0), name="product_price_non_negative"),
            models.CheckConstraint(
                condition=Q(billz_price__isnull=True) | Q(billz_price__gte=0),
                name="product_billz_price_non_negative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        last_change_at__isnull=True,
                        last_change_kind="",
                        last_change_actor__isnull=True,
                        last_change_old_price__isnull=True,
                        last_change_new_price__isnull=True,
                    )
                    | (Q(last_change_at__isnull=False) & ~Q(last_change_kind=""))
                ),
                name="product_last_change_all_or_nothing",
            ),
            models.CheckConstraint(
                condition=~Q(last_change_kind=enums.ProductChangeKind.PRICE_CHANGED)
                | Q(last_change_old_price__isnull=False, last_change_new_price__isnull=False),
                name="product_price_change_has_prices",
            ),
        ]
        indexes = [
            GinIndex(fields=["search_text"], name="catalog_product_search_trgm", opclasses=["gin_trgm_ops"]),
            models.Index(fields=["category", "status"], name="catalog_product_cat_status"),  # ≤ 30 chars (E034)
            models.Index(
                Coalesce("last_change_at", "created_at").desc(),
                F("id").desc(),
                name="catalog_product_default_order",
            ),  # exact match for ordering=default
            models.Index(
                fields=["is_new_from_billz"], condition=Q(is_new_from_billz=True), name="catalog_product_new"
            ),
        ]
        ordering = ["-id"]

    def __str__(self) -> str:
        return self.name_uz or self.billz_name or self.billz_id

    @property
    def display_name(self) -> str:
        return self.name_uz or self.billz_name
```

**Invariants**
- `billz_id` is the upsert key for the Billz import; creating a product through the API requires it to exist in Billz (`billz_not_found`), which also fills `billz_name`, `volume`, `billz_price`, `billz_synced_at` and the `ProductStock` row. The import updates only Billz-owned columns (`bulk_update(["billz_name","volume","billz_price","billz_synced_at"])`) and stock; it never touches texts, images, `price`, `status`, `promo`, `last_change_*`, `updated_at` (`bulk_update` bypasses `TimeStampedModel.save()`, which is intended here — `updated_at` means "last admin change").
- New products from the import: `status=hidden`, `is_new_from_billz=True`, `price=billz_price or 0`, texts empty, no images. Every successful admin create/PATCH sets `is_new_from_billz=False` (contract §2.3).
- **Active rule** (service, not DB): when the resulting status is `active`, `category` is set, ≥1 image exists, and `price ≥ 1000` (the third is implied by the price validator but must also be checked when a Billz-imported product with `price=0` is activated by a PATCH that does not send `price`; field `price`, code `min_value`).
- **`last_change`**: set by `catalog.services.create_product()`/`update_product()` only, kind by priority `created > price_changed > activated/hidden > promo_added/promo_removed > updated`; `old_price`/`new_price` only for `price_changed`; image endpoints never touch it; the Billz import never touches it. "Never edited" (`last_change_at IS NULL`) covers seeded and imported-only products.
- `search_text` is **generated by PostgreSQL** (`GENERATED ALWAYS AS (lower(name_uz || ' ' || name_ru || ' ' || name_en || ' ' || billz_name || ' ' || billz_id)) STORED`): no code path (import `bulk_update`, seeder, raw SQL) can let it drift. Django never writes it; reading it back needs `refresh_from_db()` after save (Django 6 does not `RETURNING` generated fields on insert).
- `price` is a DB `≥ 0`; the business range `1000..100000000` is a serializer rule so imported rows can hold `0` while hidden.
- `promo` only flags the item; the promo price is not modelled (the mobile design shows a badge; `promo_subtotal` in orders sums item prices flagged promo). If a separate promo price arrives, it becomes `promo_price` nullable without changing this table's keys.

**Contract fields served**
- §2.3 `Product`: `id`, `billz_id`, `billz_name`, `volume`, `category` (slug via `category__slug` annotation), `price`, `billz_price`, `stock` (`Coalesce(F("stock__quantity"), 0)`), `promo`, `status`, `is_new_from_billz`, `texts` (12 columns → `I18nTextField` nested per language), `main_image_url` (§4.4), `images`, `last_change` (`null` when `last_change_at IS NULL`, else `{at, actor: Actor|null, kind, old_price, new_price}`), `created_at`, `updated_at`.
- §2.3 list filters: `search` (§6.4), `category`, `status`, `promo`, `out_of_stock` (`stock__quantity__lte=0` or no stock row), `is_new`; orderings §6.5; page size 8. Export `mahsulotlar`: `ID; Billz ID; Billz nomi; Nomi (name_uz); Kategoriya (category.name_uz or ""); Narx (so‘m) (price); Qoldiq (stock); Aksiya (Ha/Yo‘q); Holat (Faol/Yashirin); Yangi (Ha/Yo‘q)`.
- §2.3 `products/stats/`: `total`, `active`, `hidden` (`Count` by status), `new_from_billz` (`is_new_from_billz=True`), `out_of_stock` (§4.3).
- §2.10 `stats.out_of_stock`, `attention.new_from_billz` (same predicates, all statuses).
- §2.8 `meta.link_options` = `status='active'` products as `{product_id, label: display_name}` sorted by label with `glowderma_ci`; `link.product_id` must be active; §2.5 `link.product_id` must be active.
- Order item snapshots (g2): `name = display_name` (uz), `billz_id`, `unit_price = price`, `promo`, `image_url = main_image_url` at checkout.
- M2 public catalog (`status='active'`, search, `in_stock`).

**Open points**
- Whether activation should require `price ≥ 1000` (recommended above; not stated in the contract's active rule).
- Whether the admin `texts.*.name` ≥2 rule should also apply to imported products on first PATCH (yes by the contract — any PATCH validates `texts.uz.name` if present; the FE always sends it).
- `promo_price` (not in the PDFs) — out of scope until asked.

### 4.3 `ProductStock`

Billz-owned stock quantity, one row per product, updated by the import/sync without touching the product row.

```python
class ProductStock(models.Model):
    product = models.OneToOneField(
        "catalog.Product", primary_key=True, on_delete=models.CASCADE, related_name="stock"
    )
    quantity = models.IntegerField(default=0)  # Billz can report negatives after returns
    synced_at = models.DateTimeField(null=True)  # when quantity was last read from Billz
    billz_updated_at = models.DateTimeField(null=True)  # Billz's own timestamp when provided

    class Meta:
        indexes = [models.Index(fields=["quantity"], condition=Q(quantity__lte=0), name="catalog_stock_out")]

    def __str__(self) -> str:
        return f"stock of product {self.product_id}: {self.quantity}"
```

**Invariants**
- Created together with the product (`create_product()` and the import both ensure the row); selectors still `LEFT JOIN` and `Coalesce(…, 0)` so a missing row reads as 0.
- Written only by `catalog.services.sync_stock(product_id, quantity, synced_at)` (import, M3 delta sync) — never by commerce. **Reservations do not decrement it**: `commerce.StockReservation` rows are the hold, and the Billz-side decrement arrives through the sale push/sync (`billz.push_sale`), which is also why the stock table is separate from `Product` (no lock contention between admin edits, checkouts and the sync's `bulk_update`). `catalog` exposes only `ProductStock.quantity` (and `synced_at`); the available quantity for cart/checkout is **`commerce.selectors.available_stock(product_ids)`** (03 §1.6, the single definition: `quantity − Σ active reservations − Σ reservations converted after synced_at`). A `catalog` selector must not read `commerce_stockreservation` (layer rule, §0).
- `quantity` is signed on purpose; `out_of_stock` is `quantity ≤ 0` everywhere (contract §2.3, §2.10).

**Contract fields served**: `Product.stock`, `products/?out_of_stock=`, `stats.out_of_stock` (both §2.3 and §2.10), export column `Qoldiq`, M2 `in_stock` and `OUT_OF_STOCK` cart errors.

**Open points**: whether the admin should see `available` (quantity minus active reservations) next to `stock` in M2 — a `commerce`/`reporting` selector change only (03 C-11).

### 4.4 `ProductImage` (TS)

An uploaded, re-encoded photo of a product with an explicit gallery position.

```python
from django.db.models import Deferrable


class ProductImage(TimeStampedModel):
    product = models.ForeignKey("catalog.Product", on_delete=models.CASCADE, related_name="images")
    file = models.ImageField(
        upload_to=media.product_image_path, max_length=255, width_field="width", height_field="height"
    )
    position = models.PositiveSmallIntegerField()
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    content_type = models.CharField(max_length=32)  # image/jpeg | image/png | image/webp (after re-encode)
    size_bytes = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)  # of the stored (re-encoded) bytes; also the file stem
    uploaded_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "position"],
                name="productimage_product_position_unique",
                deferrable=Deferrable.DEFERRED,
            ),
            models.CheckConstraint(condition=Q(position__lte=7), name="productimage_position_max_8"),
            models.CheckConstraint(
                condition=Q(content_type__in=["image/jpeg", "image/png", "image/webp"]),
                name="productimage_content_type",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"image {self.position} of product {self.product_id}"
```

**Invariants**
- Every write (`add_image`, `delete_image`, `reorder_images`) first takes `Product.objects.select_for_update().get(pk=…)`, so positions are computed without races: add → `position = count` (≤8 → `max_items` at 8), delete → compact `0..n−1` (409 `conflict` for the last image of an active product), reorder → exact-set check (`mismatch`) then assign `0..n−1`. The **deferrable** unique lets the reorder write the new positions in any order inside the transaction; PostgreSQL checks the constraint at `COMMIT`. Because the check is deferred, the service must not rely on an immediate `IntegrityError`; tests assert the final state instead.
- `core/images.py` re-encodes (format sniff, EXIF strip, longest side ≤ 2400) before the row is created; `sha256`, `size_bytes`, `width`, `height`, `content_type` describe the stored file. File path: `products/<product_id>/<sha256[:16]>.<ext>`. Deleting the row deletes the file in an `on_commit` hook (never before commit).
- `position ≤ 7` mirrors the 8-image cap at DB level; the product-level cap is still enforced in the service (`max_items`).

**Contract fields served**
- §2.3 `images[] = {id, url: absolute_media_url(file.name), position}`; `main_image_url` = `absolute_media_url` of the subquery `ProductImage.objects.filter(product=OuterRef("pk")).order_by("position", "id").values("file")[:1]` (`null` when none).
- `POST /images/` 201 `{id, url, position}`; `DELETE`; `PUT /images/order/` → `{"images": […]}`; audit `product.image_added|image_deleted|images_reordered`.
- Order item `image_url` snapshot (g2), §5.2 `ProductImage.src/label`.

**Open points**: thumbnail variants (e.g. 400 px) are not generated in M1 (the admin table renders the original); a `variants` JSON or sibling table can be added later.

### 4.5 `Banner` (TS)

A home-screen banner with an optional deep link and an optional schedule.

```python
class Banner(TimeStampedModel):
    title_uz = models.CharField(max_length=80)
    title_ru = models.CharField(max_length=80, blank=True)
    title_en = models.CharField(max_length=80, blank=True)
    image = models.ImageField(
        upload_to=media.banner_image_path,
        max_length=255,
        width_field="image_width",
        height_field="image_height",
    )
    image_width = models.PositiveIntegerField()
    image_height = models.PositiveIntegerField()
    link_kind = models.CharField(
        max_length=8, choices=enums.BannerLinkKind.choices, default=enums.BannerLinkKind.NONE
    )
    link_product = models.ForeignKey(
        "catalog.Product", null=True, on_delete=models.SET_NULL, related_name="banners"
    )
    link_category = models.ForeignKey(
        "catalog.Category", null=True, on_delete=models.SET_NULL, related_name="banners"
    )
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField(null=True)
    ends_at = models.DateTimeField(null=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(title_uz=""), name="banner_title_uz_required"),
            models.CheckConstraint(
                condition=Q(starts_at__isnull=True) | Q(ends_at__isnull=True) | Q(ends_at__gt=F("starts_at")),
                name="banner_ends_after_starts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        link_kind=enums.BannerLinkKind.NONE,
                        link_product__isnull=True,
                        link_category__isnull=True,
                    )
                    | Q(link_kind=enums.BannerLinkKind.PRODUCT, link_category__isnull=True)
                    | Q(link_kind=enums.BannerLinkKind.CATEGORY, link_product__isnull=True)
                ),
                name="banner_link_kind_targets",
            ),
        ]
        indexes = [models.Index(fields=["position", "id"], name="catalog_banner_position")]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return f"{self.title_uz} (#{self.pk})"
```

**Invariants**
- The link check is deliberately lenient on the *target* side (`kind=product` with `link_product IS NULL` is allowed) so that a `SET_NULL` from a category delete can never violate it; the service keeps it tight: `delete_category()` resets affected banners to `kind=none` first, `create/update_banner()` require the target for `product`/`category` kinds (`does_not_exist`), and the public selector (M2) drops banners whose target is missing or hidden/inactive.
- `link_product` must be `active` at write time (contract); a later hide does not alter the banner (the public selector filters).
- Schedule semantics: visible when `is_active ∧ (starts_at IS NULL ∨ starts_at ≤ now) ∧ (ends_at IS NULL ∨ ends_at > now)`. `ends_at > starts_at` is a DB check and `invalid` on `ends_at` in the serializer.
- `position` handling is the same as `Category` (advisory lock `catalog_banner_position`, exact-set reorder).
- Image goes through the product-image pipeline (same limits: ≤5 MiB, jpeg/png/webp, re-encode); path `banners/<id>/<sha256[:16]>.<ext>`. Because `upload_to` runs before the `INSERT` (no `pk` yet), `create_banner()` is two steps in one transaction: insert the row with `image=""` **and `image_width`/`image_height` taken from the `core.images.process_image()` result** (the service already holds the decoded image; `ImageField.update_dimension_fields` leaves the dimension columns untouched while there is no file, and both are `NOT NULL`), then `banner.image.save(name, content, save=True)` with the id-based name (which re-derives the same two values); the same pattern serves `Category.image` and `Professional.photo` (no dimension columns there). The file write itself is the only non-transactional step and happens last, so a rollback leaves at most an orphan file (swept by a weekly `core.media.sweep_orphans` task), never a row without a file.

**Contract fields served**
- §2.5 `Banner`: `title` (i18n), `image_url`, `link = {kind, product_id, category: slug|null}`, `is_active`, `starts_at`, `ends_at`, `position`, timestamps; list ordered by `position`; filter `is_active`; reorder `{"ids": […]}`. Export `bannerlar`: `ID; Sarlavha (title_uz); Havola ("" | "mahsulot #<id>" | "kategoriya <slug>"); Faol; Boshlanishi; Tugashi; Tartib`.
- M2 `GET /api/v1/banners/` (schedule-filtered).

**Open points**: whether an expired banner should flip `is_active` (no: `is_active` is the admin switch, the schedule is independent; the A1 UI shows "muddati o‘tgan" from `ends_at`).

---

## 5. `professionals`

### 5.1 `Location` (TS)

A branch/clinic address (P4: one row today, several supported).

```python
class Location(TimeStampedModel):
    name_uz = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    address_uz = models.CharField(max_length=300, blank=True)
    address_ru = models.CharField(max_length=300, blank=True)
    address_en = models.CharField(max_length=300, blank=True)
    phone = models.CharField(max_length=13, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True)
    is_active = models.BooleanField(default=True)
    position = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(name_uz=""), name="location_name_uz_required"),
            models.CheckConstraint(
                condition=Q(phone="") | Q(phone__regex=r"^\+998\d{9}$"), name="location_phone_e164"
            ),
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(latitude__isnull=False, longitude__isnull=False),
                name="location_latlng_pair",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.name_uz
```

**Invariants**: editable in Django admin through `professionals.services` (audit `location.created|updated|deleted`); a location with professionals cannot be deleted (`PROTECT`). All times in this module are Tashkent; no per-location timezone.

**Contract fields served**: none in admin v1 (the day board is single-location); M2 specialists/booking screens show the location address; seed creates one row.

**Open points**: P4.

### 5.2 `Professional` (TS)

A bookable specialist.

```python
class Professional(TimeStampedModel):
    location = models.ForeignKey(
        "professionals.Location", on_delete=models.PROTECT, related_name="professionals"
    )
    name = models.CharField(max_length=150)  # shown as-is in every language
    role = models.CharField(
        max_length=16, choices=enums.SpecialistRole.choices
    )  # dermatologist | cosmetologist
    photo = models.ImageField(upload_to=media.professional_photo_path, blank=True, max_length=255)
    bio_uz = models.TextField(blank=True)
    bio_ru = models.TextField(blank=True)
    bio_en = models.TextField(blank=True)
    experience_years = models.PositiveSmallIntegerField(null=True)
    slot_step_min = models.PositiveSmallIntegerField(default=30)  # slot grid used by M2 /slots
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=~Q(name=""), name="professional_name_required"),
            models.CheckConstraint(
                condition=Q(slot_step_min__in=[10, 15, 20, 30, 60]), name="professional_slot_step_allowed"
            ),
        ]
        indexes = [
            models.Index(fields=["is_active", "position", "name"], name="prof_professional_active_pos")
        ]
        ordering = ["position", "name", "id"]

    def __str__(self) -> str:
        return f"{self.name} ({self.role})"
```

**Invariants**
- Deactivation (`is_active=False`) hides the specialist from the admin day board and from M2 booking; existing appointments stay (`PROTECT` from `appointments.Appointment`). Hard delete only when nothing references the row (Django admin via services; audit `professional.deleted`).
- `position` then `name` is the board order (contract §2.9 "by position then name").

**Contract fields served**
- §2.9 `specialists[]`: `id`, `name`, `role`, plus `working_hours`/`blocks` (§5.5–5.7); `appointments[].specialist_id`.
- §2.10 none; M2 `GET /api/v1/specialists/` (photo, bio, services with prices).

**Open points**: `slot_step_min` location (per professional vs settings) and the allowed set; whether `name` needs ru/en variants (design shows one spelling).

### 5.3 `Service` (TS)

A bookable service type with default duration; prices live on the professional link.

```python
class Service(TimeStampedModel):
    name_uz = models.CharField(max_length=120)
    name_ru = models.CharField(max_length=120, blank=True)
    name_en = models.CharField(max_length=120, blank=True)
    short_name_uz = models.CharField(max_length=40)
    short_name_ru = models.CharField(max_length=40, blank=True)
    short_name_en = models.CharField(max_length=40, blank=True)
    description_uz = models.TextField(blank=True)
    description_ru = models.TextField(blank=True)
    description_en = models.TextField(blank=True)
    default_duration_min = models.PositiveSmallIntegerField()
    default_price = models.BigIntegerField(null=True)  # suggested when assigning to a professional
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~Q(name_uz="") & ~Q(short_name_uz=""), name="service_names_uz_required"
            ),
            models.CheckConstraint(
                condition=Q(default_duration_min__gte=5, default_duration_min__lte=480),
                name="service_duration_5_480",
            ),
            models.CheckConstraint(
                condition=Q(default_price__isnull=True) | Q(default_price__gte=0),
                name="service_default_price_non_negative",
            ),
        ]
        ordering = ["position", "id"]

    def __str__(self) -> str:
        return self.name_uz
```

**Invariants**: `short_name_*` is the board label (contract `service_short_name`); `name_*` the full label. Deletion is `PROTECT`ed by `ProfessionalService` and appointments; use `is_active`.

**Contract fields served**: §2.9 `appointments[].service_name = name_uz`, `service_short_name = short_name_uz`; `duration_min`/`price` on the appointment are snapshots (g3) taken from `ProfessionalService` at booking time. M2 services list.

**Open points**: none.

### 5.4 `ProfessionalService`

Which services a professional performs, at what price and duration.

```python
class ProfessionalService(models.Model):
    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="services"
    )
    service = models.ForeignKey(
        "professionals.Service", on_delete=models.PROTECT, related_name="professional_links"
    )
    price = models.BigIntegerField()
    duration_min = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timestamp_default, editable=False)
    updated_at = models.DateTimeField(default=timestamp_default)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["professional", "service"], name="professionalservice_unique"),
            models.CheckConstraint(condition=Q(price__gte=0), name="professionalservice_price_non_negative"),
            models.CheckConstraint(
                condition=Q(duration_min__gte=5, duration_min__lte=480),
                name="professionalservice_duration_5_480",
            ),
        ]
        ordering = ["professional_id", "service_id"]

    def __str__(self) -> str:
        return f"{self.professional_id}/{self.service_id}: {self.price} so'm, {self.duration_min} min"
```

**Invariants**: booking (M2) and admin create (B6) read `price` and `duration_min` here and **snapshot** them onto the appointment; later edits never change booked appointments. `CASCADE` from the professional is acceptable because a professional with appointments cannot be deleted anyway (`PROTECT` from appointments). `updated_at` is set by the service (plain model; Django admin edits go through services too).

**Contract fields served**: appointment `price`, `duration_min` (at booking), M2 specialist cards (services × prices).

**Open points**: none.

### 5.5 `WorkingHours`

Weekly recurring hours per professional; the absence of a row for a weekday means "not working".

```python
class WorkingHours(models.Model):
    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="working_hours"
    )
    weekday = models.PositiveSmallIntegerField()  # 0 = Monday … 6 = Sunday (Python date.weekday())
    start = models.TimeField()  # Tashkent wall-clock
    end = models.TimeField()
    break_start = models.TimeField(null=True)  # optional recurring break → block kind "off"
    break_end = models.TimeField(null=True)

    class Meta:
        verbose_name_plural = "working hours"
        constraints = [
            models.UniqueConstraint(
                fields=["professional", "weekday"], name="workinghours_professional_weekday_unique"
            ),
            models.CheckConstraint(
                condition=Q(weekday__gte=0, weekday__lte=6), name="workinghours_weekday_0_6"
            ),
            models.CheckConstraint(condition=Q(start__lt=F("end")), name="workinghours_start_before_end"),
            models.CheckConstraint(
                condition=Q(break_start__isnull=True, break_end__isnull=True)
                | (
                    Q(break_start__isnull=False, break_end__isnull=False)
                    & Q(break_start__lt=F("break_end"), break_start__gte=F("start"), break_end__lte=F("end"))
                ),
                name="workinghours_break_inside_day",
            ),
        ]
        ordering = ["professional_id", "weekday"]

    def __str__(self) -> str:
        return f"{self.professional_id} wd{self.weekday} {self.start:%H:%M}-{self.end:%H:%M}"
```

**Invariants**: no overnight shifts (`start < end` on the same Tashkent day). Times are naive `TimeField`s interpreted in `timeutils.TASHKENT`; the selector builds aware datetimes with `datetime.combine(date, t, tzinfo=TASHKENT)`.

**Contract fields served**: §2.9 `working_hours: {"start": "09:00", "end": "18:00"} | null` for the requested date unless an override exists (§5.6); a recurring break renders as a `blocks[]` entry `{start, end, kind: "off", label: null}` (the FE maps a null label of kind `off` to "Ish vaqti emas" — contract §5.6: `label ?? (kind === "busy" ? "Band qilingan" : "Ish vaqti emas")`; the backend never invents a label). M2 slots = hours − break − blocks − existing appointments, stepped by `slot_step_min`.

**Open points**: whether the recurring break belongs here (recommended; the design's "Jasur 13:00–14:00 off" is a daily lunch) or must be entered as dated `BlockedPeriod`s.

### 5.6 `WorkingHoursOverride`

Date-specific hours for one professional; both times null = day off.

```python
class WorkingHoursOverride(models.Model):
    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="hours_overrides"
    )
    date = models.DateField()  # Tashkent calendar day
    start = models.TimeField(null=True)
    end = models.TimeField(null=True)
    note = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["professional", "date"], name="hoursoverride_professional_date_unique"
            ),
            models.CheckConstraint(
                condition=Q(start__isnull=True, end__isnull=True)
                | Q(start__isnull=False, end__isnull=False, start__lt=F("end")),
                name="hoursoverride_both_or_none",
            ),
        ]
        ordering = ["date"]

    def __str__(self) -> str:
        return f"{self.professional_id} {self.date}: {'off' if self.start is None else f'{self.start:%H:%M}-{self.end:%H:%M}'}"
```

**Invariants**: an override wins over `WorkingHours` for that date (contract "date override wins"). An override does not cancel confirmed appointments; the service that creates one returns the list of conflicting appointments for the staff to act on (B6 admin endpoint; Django admin shows a warning).

**Contract fields served**: §2.9 `working_hours` (override → its times or `null`); M2 slots.

**Open points**: none.

### 5.7 `BlockedPeriod`

An absolute-time block (busy or off) in a professional's calendar.

```python
class BlockedPeriod(models.Model):
    professional = models.ForeignKey(
        "professionals.Professional", on_delete=models.CASCADE, related_name="blocked_periods"
    )
    start = models.DateTimeField()
    end = models.DateTimeField()
    kind = models.CharField(max_length=8, choices=enums.BlockKind.choices)  # busy | off
    label = models.CharField(max_length=80, blank=True)  # "" ↔ null in the API
    created_by = models.ForeignKey(
        "accounts.StaffUser", null=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timestamp_default, editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(end__gt=F("start")), name="blockedperiod_end_after_start")
        ]
        indexes = [models.Index(fields=["professional", "start", "end"], name="prof_blocked_prof_range")]
        ordering = ["start", "id"]

    def __str__(self) -> str:
        return f"{self.kind} {self.start:%Y-%m-%d %H:%M}–{self.end:%H:%M} ({self.professional_id})"
```

**Invariants**: blocks may overlap each other and working hours; the day board clips them. Booking (M2) and admin appointment creation (B6) reject slots intersecting any block. Created through services from Django admin or the B6 admin endpoint (audit `blocked_period.*`).

**Contract fields served**: §2.9 `blocks[]`: for day `D` with `[d0, d1) = timeutils.day_bounds(D)`: rows with `start < d1 ∧ end > d0`, rendered `{"start": fmt(max(start, d0)), "end": fmt(min(end, d1) or "24:00" → "23:59")…}` — the renderer emits `"HH:MM"` in Tashkent, using `"23:59"` for a block that runs to the next day (the FE grid ends at the working-day end anyway), `kind`, `label: NULLIF(label, '')`.

**Open points**: exact rendering of the clipped end at midnight (`"23:59"` vs `"24:00"`): the FE time parser expects `HH:MM` ≤ 23:59.

### 5.8 Day-schedule derivation (for `GET appointments/?date=`)

```
weekday = D.weekday()
override = WorkingHoursOverride(professional, date=D)            # one query for all professionals: filter(date=D)
if override exists:  hours = None if override.start is None else (override.start, override.end)
else:                wh = WorkingHours(professional, weekday); hours = None if wh is None else (wh.start, wh.end)
blocks = clipped BlockedPeriod rows (5.7) + (wh.break as kind "off", label "") when hours came from WorkingHours and a break is set
```
Four queries total (professionals, overrides for D, hours for the weekday, blocks intersecting D); the appointments of the day come from g3.

---

## 6. Database features by migration (pg_trgm, GIN, ICU collation)

### 6.1 `pg_trgm` extension — `core/migrations/0007_db_extensions.py`

> **As built (B2 groundwork, T12):** `core.0007_db_extensions` creates `pg_trgm`, `btree_gist` **and** the ICU
> collation `glowderma_ci` in one migration; there is no `catalog.0002_glowderma_ci_collation` (§6.3 below describes
> the collation itself, which is unchanged). App migrations depend on `("core", "0007_db_extensions")` or, when they
> add an append-only trigger, on `("core", "0008_forbid_mutation_purge_guc")`.

Placed in `core` (lowest layer, migrates first) because **both** `accounts.Customer.name` (trigram index, skeleton row 1) and `catalog.Product.search_text` need it, and `accounts` migrates before `catalog`. This is the one deliberate refinement of §5.3's "catalog (+ RunSQL: pg_trgm, …)".

```python
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0006_outbox_claim_token")]
    operations = [TrigramExtension()]  # CREATE EXTENSION IF NOT EXISTS "pg_trgm" / DROP EXTENSION IF EXISTS "pg_trgm"
```

- `pg_trgm` is a **trusted** extension (PG ≥ 13): any role with `CREATE` on the database installs it without superuser. Locally the `glowderma` role is superuser; in the `postgres:18-trixie` image `POSTGRES_USER` is superuser; on a managed server the deploy doc says: either `ALTER DATABASE glowderma OWNER TO glowderma` or a one-off `CREATE EXTENSION pg_trgm` by the DBA, after which `IF NOT EXISTS` is a no-op.
- **`django.contrib.postgres` is in `INSTALLED_APPS`** (`config/settings/base.py`, after `django.contrib.staticfiles`, before `rest_framework`). In Django 6.1.2 `PostgresIndex` (base of `GinIndex`), `ExclusionConstraint` and `RangeField` (base of the `DateTimeRangeField` used by `appointments`) all mix in `CheckPostgresInstalledMixin`, whose system check raises `postgres.E005` ("'django.contrib.postgres' must be in INSTALLED_APPS in order to use GinIndex") and fails `manage.py check`/`migrate`/`makemigrations --check` otherwise (review blocker 1). Cost: one `post_migrate` handler (`register_type_handlers`) and a few extra lookups on `CharField`/`TextField`; nothing else changes. The `CreateExtension` operations (`TrigramExtension`, `BtreeGistExtension`) do not carry the check, but with the app installed they are the natural spelling.
- Reverse: `TrigramExtension` drops the extension. By the time `core 0007` reverses, every dependent index has been dropped by the reversing app migrations (they depend on `core 0007`), so the drop never cascades into anything. The only overreach is dropping an extension a DBA pre-created; `migrate … zero` is a scratch-DB-only operation, documented as such.
- Dependent migrations declare it (as built): `accounts 0004_customer_device_address_smscode_social` → `dependencies = [("accounts", "0003_…"), ("core", "0008_forbid_mutation_purge_guc")]` (0008 transitively includes 0007); `catalog 0001_initial` → `[("core", "0007_db_extensions"), ("accounts", "0001_initial")]` — no catalog field references `accounts.Customer`, so the catalog package could land in parallel with `accounts.0004`.

### 6.2 Generated search column and its GIN index — `catalog/migrations/0001_initial.py`

Emitted by `makemigrations` from the model (`GeneratedField` + `GinIndex` in `Meta.indexes`), so `makemigrations --check` stays clean; no hand-written `RunSQL` is needed for the index. The SQL Django generates (and that the migration test asserts via `pg_get_indexdef` / `pg_attribute.attgenerated`):

```sql
-- column (from GeneratedField; Django inlines the expression at CREATE TABLE / ADD COLUMN time)
"search_text" text GENERATED ALWAYS AS (
  LOWER(("name_uz" || ' ' || "name_ru" || ' ' || "name_en" || ' ' || "billz_name" || ' ' || "billz_id"))
) STORED

-- index (from GinIndex(fields=["search_text"], name="catalog_product_search_trgm", opclasses=["gin_trgm_ops"]))
CREATE INDEX "catalog_product_search_trgm" ON "catalog_product" USING gin ("search_text" gin_trgm_ops);
-- reverse (automatic on RemoveIndex / DeleteModel)
DROP INDEX IF EXISTS "catalog_product_search_trgm";
```

Why `||` and not `CONCAT`: PostgreSQL's `concat()` is `STABLE`, and a generated column requires an `IMMUTABLE` expression; `textcat` (`||`) and `lower(text)` are immutable. Django's `Concat` compiles to `CONCAT(a::text, b::text)` on PostgreSQL and would make the migration fail with "generation expression is not immutable" — hence the small `JoinText(Func)` in §4.2. All five source columns are `NOT NULL` (blank `""`), so `||` never yields `NULL`.

If the lead prefers not to use `GeneratedField`, the equivalent hand-written alternative is an **expression index without a column**:

```python
GinIndex(
    Lower(
        JoinText(
            F("name_uz"),
            Value(" "),
            F("name_ru"),
            Value(" "),
            F("name_en"),
            Value(" "),
            F("billz_name"),
            Value(" "),
            F("billz_id"),
        )
    ),
    name="catalog_product_search_trgm",
    opclasses=["gin_trgm_ops"],
)
```
```sql
CREATE INDEX "catalog_product_search_trgm" ON "catalog_product"
  USING gin ((LOWER(("name_uz" || ' ' || "name_ru" || ' ' || "name_en" || ' ' || "billz_name" || ' ' || "billz_id"))) gin_trgm_ops);
```
with the filter written against the same expression. The stored column is preferred: the query is simpler (`search_text LIKE …`), `EXPLAIN` is unambiguous, and the planner matches the index without expression equivalence.

### 6.3 ICU collation — created by `core.0007_db_extensions` (T12; the `catalog.0002` file below was not generated)

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("catalog", "0001_initial")]
    operations = [
        migrations.RunSQL(
            sql="CREATE COLLATION glowderma_ci (provider = icu, locale = 'und-u-ks-level2', deterministic = false);",
            reverse_sql="DROP COLLATION IF EXISTS glowderma_ci;",
        )
    ]
```

- `und-u-ks-level2`: root locale, strength 2 → **case-insensitive, accent-sensitive** (verified on PG 16.15: `'ZINK KREM' = 'zink krem'` → true; `'ánd' = 'and'` → false; `'ёлка' = 'елка'` → false; `'Ёлка' = 'ёлка'` → true). Sort sample under it: `A a á B b O‘zbek Oʻzbek ozbek Z z е ё Я я` — Latin before Cyrillic, case variants adjacent, which is what the products table needs (the default `C.UTF-8`/`en_US` collation would put every capital before every lowercase or split Cyrillic by case).
- Creating it needs `CREATE` on the schema (`public`, owned by the DB owner) and an ICU-enabled server build (official `postgres` images and the Ubuntu 16.15 package are ICU builds; the migration test asserts `pg_collation.collprovider = 'i'` so a non-ICU build fails the suite, not production).
- The collation lives **per database** (`pg_collation`), so each test database gets it from this migration; it is not a cluster-wide object.
- Reverse drops it; nothing else depends on it (we never set `db_collation` on a column, see limits), so the drop never cascades.
- Ops note: after an ICU library upgrade PostgreSQL warns "collation has version mismatch"; run `ALTER COLLATION glowderma_ci REFRESH VERSION;` (deploy doc). Because nothing is indexed under it, no reindex is needed.

**Usage — ORDER BY only** (`catalog/selectors.py`):

```python
from django.db.models import F, Value
from django.db.models.functions import Coalesce, Collate, NullIf

DISPLAY_NAME = Coalesce(NullIf(F("name_uz"), Value("")), F("billz_name"))


def order_products(qs, ordering: str):
    if ordering in ("name", "-name"):
        qs = qs.annotate(_display_ci=Collate(DISPLAY_NAME, "glowderma_ci"))
        return qs.order_by("_display_ci", "id") if ordering == "name" else qs.order_by("-_display_ci", "-id")
    # default: last_change.at desc (falling back to created_at), then id desc
    return qs.annotate(_changed=Coalesce(F("last_change_at"), F("created_at"))).order_by("-_changed", "-id")
```
```sql
ORDER BY (COALESCE(NULLIF("catalog_product"."name_uz", ''), "catalog_product"."billz_name") COLLATE "glowderma_ci") ASC,
         "catalog_product"."id" ASC
```

`Collate` validates the name against `^[\w-]+$` and quotes it; `django.db.models.functions.Collate` has existed since 3.2.

**Limits of a non-deterministic collation (enforced by review + a test):**
1. **Never in `LIKE`/`ILIKE`/regex/`SIMILAR TO`**: PG 16 raises `nondeterministic collations are not supported for LIKE` (reproduced on 16.15); PG 18 (CI/prod) allows `LIKE`/`ILIKE` under non-deterministic collations and still refuses regex/`SIMILAR TO`. The rule is therefore **never relied on in either direction**: search always goes through `search_text` (deterministic default collation), and the test in §6.6 asserts the rule, not a server error.
2. **Never for equality, `DISTINCT`, `GROUP BY`, `UNIQUE` or a column's `db_collation`**: under it `'Zink' = 'zink'` is true, which would silently merge rows or make unique constraints case-insensitive. Slugs, phones, `billz_id`, emails keep the default collation (`StaffUser` already has its own `Lower(email)` unique).
3. **No pattern-ops / trigram indexes under it**, and no btree index is created for the `ORDER BY` either: the products table is a few hundred rows (248 seeded), one page of 8; an in-memory sort is cheaper than maintaining an expression index. If the catalog grows past ~50 k rows, add `Index(Collate(DISPLAY_NAME, "glowderma_ci"), F("id"), name="catalog_product_display_ci")`.
4. Its result ordering can change with ICU versions (above); it is never used to define data, only to present it.

### 6.4 Product search semantics (contract §2.3)

```python
def filter_products_search(qs, raw: str):
    term = raw.strip()[:64]
    if not term:
        return qs
    cond = Q(
        search_text__contains=Lower(Value(term))
    )  # LIKE '%' || lower('term') || '%' (Django escapes % _ \)
    if term.isascii() and term.isdigit() and len(term) <= 18:  # str.isdigit() also accepts "²"/"①", which int() rejects
        cond |= Q(id=int(term))  # a numeric term also matches id exactly
    return qs.filter(cond)
```
```sql
WHERE "catalog_product"."search_text" LIKE '%' || REPLACE(REPLACE(REPLACE(LOWER('serum'), E'\\', E'\\\\'), E'%', E'\\%'), E'_', E'\\_') || '%'
   OR "catalog_product"."id" = 4607012301      -- only when the term is all digits
```

- Case-insensitive because both sides are lower-cased **in PostgreSQL** (same `lower()` as the generated column; a Python `str.lower()` is acceptable but differs on a few code points such as dotted İ). The planner constant-folds `'%' || lower('serum') || '%'`, so the trigram GIN index is used (`Bitmap Index Scan on catalog_product_search_trgm`); the migration test asserts the plan with `SET LOCAL enable_seqscan = off`.
- "Substring over `texts.{uz,ru,en}.name`, `billz_name` and `billz_id`" — exactly the five concatenated columns, space-separated so a term cannot match across two fields' boundary by accident (except the space itself; "serum 30" would match `… SERUM 30ML`, which is desirable).
- Terms shorter than 3 characters still work (the GIN index just returns everything and the recheck filters); no minimum length in the API.
- The numeric `id` match is `OR`ed, never replaces the text match (`"4607012301"` matches `billz_id` through `search_text` *and* `id` if such a product id existed).
- `billz_id` is part of `search_text`, so barcodes are searchable; the export's `ID` column is the product id, `Billz ID` the `billz_id`.

### 6.5 Product ordering semantics

| `ordering` | SQL | Notes |
|---|---|---|
| `default` | `ORDER BY COALESCE(last_change_at, created_at) DESC, id DESC` | exact match for the expression index `catalog_product_default_order` (`(COALESCE(last_change_at, created_at)) DESC, id DESC`); at this table size the planner may still sort in memory, which is fine. |
| `name` | `ORDER BY (COALESCE(NULLIF(name_uz,''), billz_name) COLLATE "glowderma_ci") ASC, id ASC` | display name = uz name else Billz name |
| `-name` | `… DESC, id DESC` | |

Display name is the same expression everywhere a product is labelled in Uzbek (`link_options.label`, order item `name` snapshot, exports' `Nomi` uses `name_uz` **only** — the export column is "Nomi" next to "Billz nomi", so it must not fall back).

### 6.6 How the test database gets these features

- `pytest-django` creates `TEST_DB_NAME` (default `test_glowderma`, per-worktree override in `config/settings/test.py`) with `CREATE DATABASE` as the `glowderma` role, then runs **every migration** of every app (`--nomigrations` / `--no-migrations` must never be added to `pyproject.toml` or CI: it would build tables from models and skip every `RunSQL`, so the extension, the collation, the append-only triggers and the data migrations would be missing). The generated column and the GIN index come from the model state and are created either way; the extension and collation come only from migrations.
- `--reuse-db`: pytest-django still runs `migrate` on an existing test DB, so new migrations are applied; `--create-db` forces a rebuild when a migration was edited in place.
- Required server state for the dev loop: the `glowderma` role can `CREATE DATABASE` (it is superuser locally) and the server has ICU (`PostgreSQL 16.15 (Ubuntu …)` does). CI's `postgres:18-trixie` service: the default superuser creates the test DB; ICU present. `postgres` image default locale is `en_US.utf8`; the local dev DB is `C.UTF-8` — both lower-case Cyrillic correctly (verified `Сыворотка → сыворотка` locally; the migration test covers CI).
- Tests that pin the features (`catalog/tests/test_db_features.py`, `core/tests/test_pg_trgm.py`): `SELECT 1 FROM pg_extension WHERE extname='pg_trgm'`; `SELECT collprovider, collisdeterministic FROM pg_collation WHERE collname='glowderma_ci'` → `('i', False)`; `pg_get_indexdef` contains `gin_trgm_ops`; `attgenerated='s'` for `search_text`; an `EXPLAIN` of the search query under `enable_seqscan=off` names the index; ordering vectors with mixed Latin/Cyrillic/case names; and the **collation-usage rule** as a version-independent test (review major 4 — the PG 16 `ProgrammingError` does not occur on PG 18): (a) a source test that no `Collate(…, "glowderma_ci")` appears in `catalog/selectors.py` outside the `order_products` ordering annotation (AST walk, not a string grep), and (b) the raw-SQL probe `SELECT 'a' COLLATE glowderma_ci LIKE 'A'` guarded by the server version: `if connection.pg_version < 180000: with pytest.raises(ProgrammingError): …` else it executes and returns a boolean. Both branches are exercised across the dev loop (16) and CI (18).
- `migrate catalog zero` / `migrate core zero` on a scratch DB: drop the collation, indexes, tables, then the extension — verified in B2's reversibility smoke.

---

## 7. Migration list for this group (in dependency order)

| App | Migration | Contents | Depends on |
|---|---|---|---|
| core | `0007_db_extensions` | `TrigramExtension()`, `BtreeGistExtension()`, ICU collation `glowderma_ci` (§6.1, §6.3; T12) | `core.0006_outbox_claim_token` |
| core | `0008_forbid_mutation_purge_guc` | GUC-aware `glowderma_forbid_mutation()` (03 §9 row 2); **S-4: `core/db.py` emits the same body, in the same commit, before any app trigger migration is generated** | `core.0007` |
| accounts | `0004_customer_device_address_smscode_social` | `Customer`, `Device`, `Address`, `SmsCode`, `CustomerSocialIdentity` (+ `OpClass` GIN index on `LOWER(name)`) | `accounts.0003`, `core.0008` |
| accounts | `0005_authsession_customer_fk` | `RunPython` precheck (no stray `customer_id`), `AlterField customer`, `AddField device/previous_refresh_hash/rotated_at`, indexes, check constraint | `accounts.0004` |
| integrations | `0001_initial` | `WebhookInbox`, `Job`, `IntegrationState` | `accounts.0004` (Job.created_by) |
| integrations | `0002_webhookinbox_append_only` | `RunSQL(*append_only_trigger_sql("integrations_webhookinbox"))` | `integrations.0001`, **`core.0008`** (so the trigger function already carries the purge GUC; same list as 03 §9 row 5) |
| integrations | `0003_seed_integration_state` | `RunPython` creating the six provider rows (reverse: delete them) | `integrations.0002` |
| site_config | `0001_initial` | `SiteSettings` | `accounts.0001` (updated_by) |
| site_config | `0002_seed_singleton` | `RunPython` `get_or_create(pk=1)` (reverse: noop) | `site_config.0001` |
| catalog | `0001_initial` | `Category`, `Product` (generated column, GIN index), `ProductStock`, `ProductImage` (deferrable unique), `Banner` | `core.0007`, `accounts.0001` |
| professionals | `0001_initial` | `Location`, `Professional`, `Service`, `ProfessionalService`, `WorkingHours`, `WorkingHoursOverride`, `BlockedPeriod` | `accounts.0001` |

`INSTALLED_APPS` gains `django.contrib.postgres` (after `django.contrib.staticfiles`, before `rest_framework` — required by `GinIndex`, `ExclusionConstraint` and `DateTimeRangeField`, §6.1) and `glowderma.integrations`, `glowderma.site_config`, `glowderma.catalog`, `glowderma.professionals` (in that order after `glowderma.accounts`); `.importlinter` layers per plan §4.1.

---

## 8. Tests this design implies for B2 (models/migrations only; APIs come in B3/B6)

- Constraint tests (one per `CheckConstraint`/`UniqueConstraint` above) through `IntegrityError` on raw `objects.create` — e.g. second active `Device` with the same `fcm_token`, `Category(slug="order")`, `Banner(ends_at ≤ starts_at)`, `Job` second `queued` of the same kind, `SiteSettings(pk=2)`, `WorkingHours(start ≥ end)`.
- Deferrable: within one `atomic()` swap two `ProductImage` positions by writing 1→0 and then 0→1 as two sequential `update()`s; the commit succeeds. The same first `update()` run in autocommit fails with `IntegrityError` (the statement is its own transaction, and an `INITIALLY DEFERRED` constraint is checked at that implicit commit). A third case: inside `atomic()` write a genuine duplicate and assert the `IntegrityError` surfaces on exit from the block (at `COMMIT`), not at the statement — so the service tests check the final state, never catch at statement level.
- Append-only: `WebhookInbox` ORM `update()`/`delete()`/re-`save()` raise `AppendOnlyError`; raw `UPDATE`/`DELETE` raise `ProgrammingError`/`InternalError` with `append-only table integrations_webhookinbox`; insert with a duplicate `(provider, event_key)` raises `IntegrityError`.
- `search_text`: after `create()` + `refresh_from_db()`, equals `lower(…)`; `bulk_update(["billz_name"])` changes it (proves the DB maintains it).
- Search and ordering vectors (§6.4/6.5) including Cyrillic/Latin mixes, `%`/`_` in the term, numeric term matching both `id` and `billz_id`, a product with empty `name_uz` sorting by `billz_name`.
- `AuthSession` migration: a staff session survives `0005`; `customer` FK enforces existence; audience check rejects a staff session with a customer set.
- `SiteSettings.load()` returns the seeded row without an extra `INSERT` (`assertNumQueries(1)`).
- Day-schedule derivation (§5.8) with an override, a day-off override, a recurring break and a block spanning midnight.

---

## 9. Internal (non-contract) enums of this group — nested `TextChoices`, not in `core/enums.py`

| Nested class | Literals | Used by |
|---|---|---|
| `Device.Platform` | `ios`, `android` | `Device.platform` (literals = `enums.OrderChannel`) |
| `SmsCode.Purpose` | `signup`, `reset`, `new_device`, `social_link` | `SmsCode.purpose` |
| `WebhookInbox.Provider` | `click`, `payme`, `bts`, `billz` | `WebhookInbox.provider` |
| `IntegrationState.Provider` | `billz`, `bts`, `click`, `payme`, `eskiz`, `fcm` | `IntegrationState.provider` |

`core/enums.py` is reserved for contract enums (its class names feed `ENUM_NAME_OVERRIDES`); the four above are never rendered by the admin API, so they stay on their models like the internal states in `02-…` §0.1 and `03-…` §0.4. Contract enums used here (names as in §3.1): `ContentLanguage`, `CustomerStatus`, `ProductStatus`, `ProductChangeKind`, `BannerLinkKind`, `SpecialistRole`, `BlockKind`, `JobKind`, `JobState`. OpenAPI `ENUM_NAME_OVERRIDES` (B0) must list each so the `status`/`state`/`kind` serializers do not collide.

---

## 10. Consolidated open points (for the lead)

1. **Extension placement**: `pg_trgm` created in `core 0007` (needed by `accounts.Customer` before `catalog`), instead of §5.3's "catalog (+ RunSQL pg_trgm)". The collation stays in `catalog`.
2. **`Customer.registered_at` on reactivation**: keep (recommended) or reset.
3. **Customer social login table**: separate `CustomerSocialIdentity` (recommended) vs nullable-FK extension of staff `SocialIdentity`.
4. **`Device.platform` enum**: nested `Device.Platform` (decided, review minor 23) — reuse `enums.OrderChannel` only if the lead wants a single class.
5. **Product activation** should also require `price ≥ 1000` (imported products carry `price=0` while hidden).
6. **`search_text` as `GeneratedField`** (recommended; cannot drift) vs a `save()`-maintained column as the skeleton's wording suggests; note that the Billz import's `bulk_update` would bypass `save()`.
7. **Recurring break** on `WorkingHours` (`break_start/end`, rendered as an `off` block) — refinement beyond the skeleton.
8. **Terms/privacy storage**: singleton columns + `terms_version` (now) vs a versioned `LegalDocument` table (later).
9. **Retention defaults**: `WebhookInbox` 180 days, `raw_body` cap 64 KiB, stale `Job` reap after 15 min without heartbeat.
10. **Customers list `name` ordering collation**: default vs `glowderma_ci` (would add a migration dependency `customers → catalog.0002`).
11. **Clipped block end at midnight**: render `"23:59"` (proposed) — FE parser accepts `HH:MM` only.
12. **Hidden category ⇒ hidden products in the customer catalog** (M2 proposal).

---

## 11. Django admin registrations (read-only, `core.admin.register_read_only`)

Every model of this group is registered through `ReadOnlyAdmin` (03 §8); the six `professionals` models additionally get the service-backed editable admin described there. Secret columns are **excluded**, never shown even read-only (AGENTS.md: "never expose `password` or `refresh_hash`"):

| Model | `list_display` | `list_filter` | `search_fields` | `exclude` |
|---|---|---|---|---|
| `Customer` | id, phone, name, status, language, registered_at | status, language | phone, name | `password` |
| `Device` | customer, platform, model, app_version, push_enabled, is_active, last_active_at | platform, push_enabled, is_active | customer phone, fid | `fcm_token` |
| `Address` | customer, label, text, is_default | is_default | customer phone, text | — |
| `SmsCode` | phone, purpose, created_at, expires_at, consumed_at, exhausted_at, attempts | purpose | phone | `code_hmac`, `verification_token_hash` |
| `CustomerSocialIdentity` | customer, provider, email_at_link, created_at | provider | subject, email_at_link | — |
| `AuthSession` (existing M0 admin) | unchanged | unchanged | unchanged | `refresh_hash` (already) **+ `previous_refresh_hash`** |
| `WebhookInbox` | received_at, provider, event_key, entity_ref, result status | provider | event_key, entity_ref | — (`headers` is allow-listed at insert time) |
| `Job` | id, kind, state, created_at, started_at, finished_at, created_by | kind, state | — | — |
| `IntegrationState` | provider, last_sync_at, last_success_at, last_error_at, consecutive_failures | — | — | — |
| `SiteSettings` | id, reservation_minutes, support_phone, updated_at, updated_by | — | — | — |
| `Category` | slug, name_uz, position, is_active | is_active | slug, name_uz | — |
| `Product` | id, billz_id, name_uz, billz_name, category, price, status, promo, is_new_from_billz | status, promo, is_new_from_billz, category | billz_id, name_uz, billz_name | — |
| `ProductStock` | product, quantity, synced_at | — | product billz_id | — |
| `ProductImage` | product, position, content_type, width, height, uploaded_by | — | product billz_id | — |
| `Banner` | id, title_uz, link_kind, is_active, starts_at, ends_at, position | link_kind, is_active | title_uz | — |
| `Location`, `Professional`, `Service`, `ProfessionalService`, `WorkingHours`, `BlockedPeriod` | editable through `professionals.services` (03 §8) | role / is_active where present | name fields | — |
| `WorkingHoursOverride` | professional, date, start, end, created_by | — | — | — (read-only in M1, 03 S-3) |

`raw_id_fields` for every FK, `list_per_page = 50`, `show_full_result_count = False` come from `ReadOnlyAdmin`. `PosDevice.key_hash` (02 §1.8) is excluded in `02-…` §6.
