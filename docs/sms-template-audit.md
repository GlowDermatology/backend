# SMS template audit — Eskiz → DevSMS universal OTP (2026-11-24)

**Question:** can every SMS GlowDerma sends go out through DevSMS's four pre-approved universal OTP templates,
from the shared sender `4546`, without a custom template or an Alpha Sender ID?

**Answer: yes.** GlowDerma sends SMS for one-time codes only, and each of the five code purposes maps to one of
the four templates. Nothing transactional or promotional goes by SMS (orders, payments, deliveries, appointments,
loyalty and campaigns use FCM push and the in-app inbox; staff get e-mail). Custom templates are **not needed for
launch**; four smaller losses (below) are optional improvements.

Sources: the code at `glowderma/` (inspected 2026-11-24), DevSMS API docs https://devsms.uz/docs/api?lang=en,
its OpenAPI document https://devsms.uz/docs/api/v1.json, its integrator guide https://devsms.uz/docs/api/agents.md
and https://devsms.uz/pricing; USMS pages https://usms.uz/, https://usms.uz/ceny/, https://usms.uz/podklyuchenie/,
https://usms.uz/sms-api/ (read 2026-11-24).

## 1. Every SMS-producing workflow

The only SMS producer is the `sms.send` outbox handler (`accounts/tasks/otp_delivery.py`), fed by
`accounts/services/otp/issuance.py` (`send_code`, `issue_login_code`). It was the only caller of the Eskiz port.
Before the migration every code was the same text in the request language (the catalog OTP text), uz "Glow Derma:
tasdiqlash kodingiz {code}. Kodni hech kimga bermang." (ru, en likewise), plus an Android SMS Retriever hash line
when `ANDROID_SMS_APP_HASH` was set.

| Workflow | Trigger (route → stored purpose) | OTP required? | DevSMS template | Compatibility |
|---|---|---|---|---|
| Registration | `POST auth/send-code/` `purpose=register` (phone) → `signup` | yes | **3** registration | compatible |
| Login on a new device | `POST auth/login/` with the right password on a device without a device key → `issue_login_code` → `new_device` | yes | **4** login | compatible |
| Password reset (also the dormant-account reset) | `POST auth/send-code/` `purpose=reset_password` → `reset` | yes | **2** password reset | compatible |
| Social sign-in completed with the phone (a new account or a link to the phone's account) | `POST auth/send-code/` `purpose=link_social` + social ticket → `social_link` | yes | **1** action confirmation | compatible (the code confirms the phone for the social sign-in; whether it registers or signs in is decided only after it is verified) |
| Attaching a phone to a signed-in (e-mail or social) account | `POST me/phone/code/` → `attach_phone` | yes | **1** action confirmation | compatible |
| Signed-in re-authentication (delete the account, unlink a social identity, set a first password, attach a contact on a stale session) | the app requests a `reset_password` code for the customer's own phone (`customers/services/me/reauth.py`) | yes | **2** password reset | compatible in mechanism; the text says "parolni tiklash" (password reset) — see §4.4 |
| E-mail codes (`verify_email`, and every purpose when the identifier is an e-mail) | `email.send` | yes | — (e-mail, not SMS) | unaffected |
| Order placed / cancelled, payment received / refunded, courier dispatched | `push.order_status` | no | none | not SMS: FCM push + inbox |
| Appointment confirmed / rescheduled / cancelled | `push.appointment` | no | none | not SMS: FCM push + inbox |
| Account security notices (password changed, social linked, …) | `push.account_security` | no | none | not SMS: FCM push + inbox (+ e-mail on an address change) |
| Loyalty, marketing campaigns | `campaign.send_batch` | no | none | not SMS: FCM push + inbox |
| Staff invites and password links | `email.staff_password_link` | no | none | not SMS: e-mail |
| Admin-facing SMS | — | — | — | none exists (no admin sends SMS; the ops console only shows `sms.send` outbox rows and the `devsms` provider state) |

Mapping in code: `accounts/tasks/otp_delivery.SMS_PURPOSES` (stored purpose → `OtpPurpose`) and
`integrations/devsms/live.TEMPLATE_TYPES` (`OtpPurpose` → `template_type`). Business code never sees a template
number.

## 2. OTP verification stays GlowDerma's

DevSMS only delivers the code. Generation (`secrets`, 6 digits — DevSMS accepts 4–8), the HMAC at rest
(`SmsCode.code_hmac`; the plain code exists only sealed in Redis for the handler), the 2-minute expiry, 5 attempts,
the 60-second resend window, the per-phone / per-device / per-IP / daily and wrong-code caps, the global hourly
SMS budget (`SMS_GLOBAL_HOURLY_LIMIT`), the atomic single-use verification and the advisory locks are unchanged
(`accounts/services/otp/`). No code reaches an API response or a log line. A retried delivery sends the **same**
code with the same idempotency key; nothing is regenerated.

## 3. Semantic fit of the four templates

| Template | DevSMS text (Uzbek; `{service_name}` = `GlowDerma`) | Used for | Fit |
|---|---|---|---|
| 1 | "… xizmatida amaliyotni tasdiqlash kodi: {otp_code}" (code to confirm an action) | social completion, attaching a phone | good: both confirm a phone for an action |
| 2 | "… xizmatida parolni tiklash uchun tasdiqlash kodi: {otp_code}" | password reset; signed-in re-authentication | exact for the reset; imprecise for re-authentication (§4.4) |
| 3 | "… xizmatiga ro'yxatdan o'tish uchun tasdiqlash kodi: {otp_code}" | registration | exact |
| 4 | "… xizmatiga kirish uchun tasdiqlash kodi: {otp_code}" | new-device login | exact |

No message is disguised as an OTP: every SMS carries a real code that the app verifies.

## 4. What the migration loses (nothing critical)

1. **Russian and English SMS.** DevSMS documents its templates in Uzbek only, so a customer whose app language is
   Russian or English now gets the Uzbek text. The code itself is unaffected. *Impact: low–medium.*
2. **Our wording.** The brand prefix "Glow Derma:" becomes the template's own wording around `GlowDerma`, and the
   anti-phishing line "Kodni hech kimga bermang" (do not share the code) is gone. The documented templates start
   with "MyService tizimi:"; whether that prefix is DevSMS's own system name or a placeholder is a **question for
   DevSMS support**. *Impact: low.*
3. **Android SMS Retriever hash line.** It cannot be appended to an approved template, so the
   `ANDROID_SMS_APP_HASH` setting was removed. The mobile app does not use the SMS Retriever API: the code field
   only uses the OS one-time-code autofill (`AutofillHints.oneTimeCode`), which reads the template text. *Impact:
   none today.*
4. **Re-authentication wording.** Signed-in re-authentication reuses `reset_password` codes (contract §2.2), so
   the SMS now says "parolni tiklash" (password reset) when a customer, for example, deletes their account. Before,
   the text was the same generic "tasdiqlash kodingiz" for every purpose. A dedicated `reauth` purpose mapped to
   template 1 would fix it, but it changes the customer API and the apps, so it needs your approval. *Impact: low.*
5. **Sender identity.** The SMS comes from `4546`, DevSMS's shared numeric sender, not a branded nickname (Eskiz
   had none approved either; open question M3-S1 is closed). *Impact: low; recognition of the brand in the SMS list.*

There are no delivery reports, before or after: Eskiz had no receiving endpoint, and DevSMS callbacks are not
wired (see `docs/integrations/devsms.md`).

## 5. Custom template requirements

None for launch. Optional enhancements, each one custom template:

| Enhancement | Why | Essential? |
|---|---|---|
| Russian and English OTP texts (2 templates, maybe ×4 purposes) | §4.1 | optional |
| Uzbek text with the "do not share" warning | §4.2 | optional |
| Re-authentication text (or the `reauth` purpose with template 1, which needs no template at all) | §4.4 | optional |

Branded sender (Alpha Sender ID "GlowDerma"): optional; not requested.

## 6. Provider comparison for future custom templates

Marked **[D]** = stated in the provider's public documentation or price page; **[Q]** = not documented, ask the
provider; **[V]** = verified here (code, tests). Nothing below was verified against a live account.

| | DevSMS | USMS |
|---|---|---|
| Integration | [V] implemented (this migration) | none; a second adapter |
| Universal / ready OTP templates | [D] 4 pre-approved universal OTP templates, no approval | [D] "готовые шаблоны из базы" in the cabinet; whether they are pre-approved OTP templates: [Q] |
| Custom template approval | [D] 50 000 so'm one-time per template (all plans); Start: per account limit, Pro: up to 10, Business: unlimited + 1 free every 3 months; timeline: [Q] | [D] templates are submitted in the cabinet (a balance is needed); fee and timeline: [Q] |
| Custom templates from the shared sender 4546 | [Q] (the `from` field defaults to 4546; not stated for custom templates) | [Q] (the API example uses `"from": "USMS"`) |
| Alpha Sender ID | [D] `from` up to 11 characters, "a sender name registered for the account"; fee and approval: [Q] | [D] "СМС ник" registered in the cabinet; fee: [Q] |
| Monthly fees | [D] Start free; Pro 49 000 so'm/month; Business 199 000 so'm/month (cashback 5% / 10% of SMS spend, capped at the plan price) | [Q] |
| Per-SMS price | [D] service/OTP SMS 214 so'm, advertising 428 so'm, "the same for everyone" (no per-operator rate; the API docs' examples show 200 so'm — check the cabinet) | [D] "от 120 сум", "depends on the recipient's operator and the message type; the exact amount is shown in the cabinet"; per-operator rates: [Q] |
| REST API | [D] v1 JSON, Bearer key with abilities, `Idempotency-Key`, status `GET /sms/{id}`, documented error codes | [D] `POST https://usms.uz/api/v1/send`, `Bearer LOGIN:SECRET`, `template_id` + `params`; idempotency, errors, limits: [Q] |
| Delivery reports | [D] callbacks with HMAC-SHA256 signature, retries for 24 h | [D] "статусы доставки и webhook"; payload and signing: [Q] |
| Business registration | [Q] | [D] individuals, legal entities and non-residents; documents: [Q] (the offer PDF) |

**Recommendation.** Stay on DevSMS's universal templates for launch. If Russian/English or warning texts become a
requirement, ask DevSMS support first whether custom templates are delivered from 4546 (or what a nickname costs);
DevSMS keeps one integration and one invoice, at 50 000 so'm per approved template. Consider USMS only if its
cabinet shows a real per-SMS price well below 214 so'm for your operator mix — an adapter (~1 day, the port is
provider-neutral) plus its own template and nickname approvals. Do not implement either without approval.

**Monthly cost of the universal OTP** (one segment each: the Uzbek templates with a 6-digit code are well under
160 GSM-7 characters; 214 so'm per SMS per the price page, Start plan, no monthly fee):

| SMS per month | DevSMS (Start) | DevSMS (Pro: 49 000 fee, 5% cashback capped at 49 000) | USMS (from 120 so'm, unverified) |
|---|---|---|---|
| 1 000 | 214 000 so'm | 49 000 + 214 000 − 10 700 = 252 300 so'm | ≥ 120 000 so'm + fees [Q] |
| 5 000 | 1 070 000 so'm | 49 000 + 1 070 000 − 49 000 = 1 070 000 so'm | ≥ 600 000 so'm + fees [Q] |
| 10 000 | 2 140 000 so'm | 49 000 + 2 140 000 − 49 000 = 2 140 000 so'm | ≥ 1 200 000 so'm + fees [Q] |

Pro pays for itself from about 4 600 SMS a month (its cashback then covers the fee) and raises the API limit from
120 to 300 sends a minute; the Start plan is enough for launch. Confirm the per-SMS rate in the DevSMS cabinet
(`GET /api/v1/balance` returns `sms_price`): the docs' examples show 200 so'm, the price page 214 so'm.
