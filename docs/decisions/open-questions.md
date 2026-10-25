# Open questions and provisional decisions

Business rules that are **not confirmed by the client**. Each has a provisional default that the code implements
behind a setting or a data field, so changing it later does not rewrite history.
Status: `open` (default in use, awaiting decision) · `decided` (confirmed; move the row to the log at the bottom).

## Loyalty / cashback
| # | Question | Provisional default | Where it lives | Status |
|---|---|---|---|---|
| L1 | Does the purchase that crosses a tier threshold already get the new rate? | `before_purchase`: rate from lifetime total before this purchase | `CashbackPolicy.basis` | open |
| L2 | Does the part paid with cashback earn cashback? | No | `CashbackPolicy.earn_on_cashback_paid` | open |
| L3 | First tier 0–10 mln or 0–20 mln? Rate above 50 mln? | Seed 0–20M 1%, 20–30M 2%, 30–50M 3%, top band open-ended (admin edits) | policy tiers | open |
| L4 | When do newly earned points become spendable? | Immediately on payment confirmation | `LOYALTY_PENDING_DAYS=0` | open |
| L5 | Refund larger than the remaining balance? | Reversal is always posted; balance may go negative and is flagged for staff | `LOYALTY_ALLOW_NEGATIVE=True` | open |
| L6 | Can cashiers redeem cashback inside Billz? | Not supported until Billz confirms | M3 | open (blocks M3) |
| L7 | How is an in-store sale discovered after the QR is resolved (Billz has no webhooks or sales feed)? | Polling scheme to be agreed with Billz | M3 | open (blocks M3) |
| L8 | Percent precision | Max 2 decimal places; integer basis-point math | `CashbackTier.percent` | decided (tech) |

## Orders / payments / delivery
| # | Question | Provisional default | Where | Status |
|---|---|---|---|---|
| O1 | Can an admin cancel after the parcel is handed to the courier? | Only if BTS cancel succeeds (BTS allows it only at status 100); otherwise 409 `not_cancellable` | order state machine | open |
| O2 | Who pays a return shipment? | Not modelled yet | — | open |
| O3 | Reservation length | 15 minutes | `SiteSettings.reservation_minutes` | open |
| O4 | Pickup: reuse "Delivered" or add "Picked up"? | Reuse `delivered` | order statuses | open |
| O5 | Refund display | Payment stays `paid` with a Refund in progress; becomes `refunded` only when the provider confirms. Payme refunds are manual (`manual_required`) and confirmed by Payme's CancelTransaction | Refund state machine | decided (tech) |
| O6 | Price authority: Glow Derma admin or Billz? | Admin price; Billz price stored for comparison | `PRICE_SOURCE=admin` | open |
| O7 | Cash payments | Not supported; payment providers are pluggable | provider registry | open |
| O8 | Dashboard "revenue" definition | Σ paid order totals by payment confirmation time, net of refunds; "orders" = created in the bucket | `reporting` selectors | open |

## Accounts
| # | Question | Provisional default | Where | Status |
|---|---|---|---|---|
| A1 | Account deletion and re-registration | `phone` stays unique; delete marks `deleted`, scrubs name/image/address, revokes sessions, removes devices; re-registration reactivates the same row, keeping ledger and purchases | `accounts` services | decided (tech) |
| A2 | Does every login require OTP + password? | Password login; OTP for sign-up, reset, and login on a new device | M2 auth | open |
| A3 | OTP lockout duration / daily resend cap | 5 attempts per code, 15-minute lockout, 10 sends per phone per day | M2 settings | open |
| A4 | Admin web token model | **BFF** (user decision 2026-10-24): browser JS never sees tokens. Access cookie HttpOnly+Secure+SameSite=Strict, Path=/admin; refresh cookie HttpOnly+Secure+SameSite=Strict, Path=/admin/auth (refresh + logout route handlers only); full CSRF on every state-changing Server Action and route handler | frontend | decided |

| A5 | Staff identity | Email + password (no usernames); Google/Facebook sign-in only for pre-created staff whose verified email matches; no self-registration | `accounts` | decided (user, 2026-10-24) |
| A6 | Customer social login | Google/Facebook allowed; first social sign-in must verify a phone by SMS, then the identity links to that phone's customer | M2 auth | decided (user, 2026-10-24) |
| A7 | OAuth credentials | Google OAuth clients (web for admin; Android/iOS for the app) and a Facebook app (id + secret, redirect URIs) — needed from the client; fake providers until then | settings | open |
| A8 | Restrict staff Google sign-in to a Workspace domain? | No restriction (`GOOGLE_ALLOWED_HD` empty) | settings | open |

## Appointments
| # | Question | Provisional default | Where | Status |
|---|---|---|---|---|
| P1 | Does a pending request hold the slot? | Yes | M2 booking | open |
| P2 | Deposits / prepayment | None | — | open |
| P3 | Cancellation and reschedule terms | Not enforced; free-text policy in settings | `SiteSettings` | open |
| P4 | One location or several branches? | One location record; model supports several | `professionals.Location` | open |

## Compliance (gates M3 / production)
| # | Requirement | Plan |
|---|---|---|
| C1 | Personal data of Uzbek citizens hosted in Uzbekistan | Postgres, Redis, media, backups and logs on Uzbek hosting; no PII to foreign SaaS (scrub error tracking, no names/phones in FCM payloads) |
| C2 | Personal-data database registration | Client action before production |
| C3 | Consent records | Terms version, time and IP stored at sign-up (M2); separate biometric consent for the Phase 2 AI feature |
| C4 | Retention | Raw provider payloads and audit log retention periods to be defined |

## Infrastructure
| # | Question | Provisional default |
|---|---|---|
| I1 | Production media storage | Named volume behind the reverse proxy; S3-compatible storage later |
| I2 | Deployment | `infra` repo / GitOps — owned by the user, out of scope here |

## Technical decisions (lead, 2026-10-25) — taken while planning M0b–M3
| # | Decision | Why |
|---|---|---|
| T1 | Module layering is a strict import-linter `layers` contract: `reporting \| customers` → `notifications` → `commerce \| appointments` → `payments \| delivery \| loyalty \| catalog \| professionals` → `site_config` → `accounts` → `integrations` → `core`. Siblings never import each other; upward effects travel as outbox topics (strings); the upper module owns the transaction and hosts the outbox handler. | The spec's "cross-module access only via services/selectors" cannot be expressed without a direction; commerce ↔ payments/delivery/notifications would otherwise be cyclic. |
| T2 | The admin customer list/card/cashback-adjustment API lives in a new `customers` module (above commerce, loyalty, appointments, notifications). `Customer`, `Device`, `Address`, `SmsCode` stay in `accounts`. | The card aggregates purchases, ledger, orders, appointments and segments, which `accounts` (a low layer) may not import. |
| T3 | Contract enums are defined once in `glowderma/core/enums.py` as `TextChoices`; `AppointmentStatus` carries an extra `cancelled` literal reserved for the M2 customer flow. | One source of truth for literals and for drf-spectacular `ENUM_NAME_OVERRIDES`. |
| T4 | `CashbackPolicy` has no mutable columns: `state` and `superseded_by` are derived (version *v* is superseded by the smallest *w > v* created before *v*'s `effective_from`). | The table is append-only at the database level; the contract only needs the derived values. |
| T5 | `WebhookInbox` rows are inserted after the synchronous processing of the callback, in the same transaction, with the outcome in `result`. | Keeps the inbox append-only while still recording what happened. |
| T6 | Retention deletes on append-only tables (`WebhookInbox`, `AuditEntry`) are allowed only inside a transaction that sets the local GUC `glowderma.allow_purge = on`; the trigger function checks it. Only the purge task sets it. | Compliance item C4 needs retention; everything else keeps the hard guarantee. |
| T7 | Django cache uses Redis DB 0 with `KEY_PREFIX=gd`; the Celery broker uses DB 1 (`CELERY_BROKER_URL`, default derived from `REDIS_URL`). | A cache flush must never drop queued tasks. |
| T8 | Media URLs are built from `PUBLIC_MEDIA_BASE_URL` (required in production); uploads are re-encoded by `core.images` (EXIF stripped, ≤2400 px) and stored under `MEDIA_ROOT/<kind>/<owner>/<sha256-prefix>.<ext>` on the shared media volume, served by the edge proxy. | Spec "Media"; production storage stays an open question (I1). |
| T9 | Throttle rates are configurable through `THROTTLE_RATES` (JSON) with the contract §1.9 values as defaults. | Contract says "defaults, configurable". |
| T10 | Admin appointment creation and rescheduling (`requested_via=admin`) are added to M1d so the three "not designed" buttons in the admin schedule can be wired. | User decision 2026-10-25 to build undesigned admin UI now. |
| T11 | All six integrations default to fake adapters (`INTEGRATIONS_MODE=fake`, per-provider overrides in M3); live adapters are written against the documented protocols with recorded-fixture tests only. | No provider credentials are available (A7 and the M3 gate). |
| T12 | Global database objects (`pg_trgm`, `btree_gist`, the ICU collation `glowderma_ci`) are created once in `core.0007`; every app migration depends on `core`, never on another app for them. | One place to audit; the customers list can order names under the same collation without a cross-app migration dependency. |
| T13 | M1 model design defaults adopted from `docs/plans/m1-models/*` open points: `registered_at` is kept on re-registration (`reactivated_at` records the return); customer social identities are a separate table; product activation also requires `price ≥ 1000`; `search_text` is a stored generated column; `WorkingHours` carries an optional recurring break; terms/privacy texts live on `SiteSettings` with a version; retention defaults `WebhookInbox` 180 d / `raw_body` 64 KiB / stale `Job` 15 min; `WorkingHoursOverride` is the seventh editable Django-admin model; reversals carry no actor while adjustments require one; `available` ignores `expires_at` (the 1-minute expiry job releases holds); tier non-overlap is enforced in the service. | Recorded so later slices do not re-open them. |
| T14 | `PyJWT[crypto]` (cryptography) is added to the pinned stack. | RS256 verification of Google ID tokens needs the backend; the alternative (Google's tokeninfo endpoint) was ruled out by the spec. |

## Decision log
_(move rows here with date and who decided)_
| Date | # | Decision | By |
|---|---|---|---|
| 2026-10-25 | A4 | BFF token model confirmed; frontend slices F1–F3 implement it | user |
| 2026-10-25 | — | Undesigned admin sections (categories, banners, customer list + groups, payments, settings, audit log) are built now in the existing design system | user |
| 2026-10-25 | — | Delivery as stacked slice PRs, gated locally; the user reviews and merges | user |
| 2026-10-25 | I2 | Production target is Docker Compose on a VPS behind an edge proxy; `docs/ops/deploy.md` documents it | user |
