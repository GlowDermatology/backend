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

## Decision log
_(move rows here with date and who decided)_
