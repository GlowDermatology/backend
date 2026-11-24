# DevSMS — one-time codes by SMS (universal OTP)

> **Not verified against a live DevSMS account.** No DevSMS token exists yet. The adapter follows DevSMS's API v1
> documentation and its OpenAPI document; its tests replay answers shaped as documented through
> `httpx.MockTransport`. Run the activation checklist below before the first real customer.

Code: `glowderma/integrations/devsms/` — `ports.py` (the provider-neutral port: `OtpPurpose`, `OtpSms`,
`SmsDelivery`, `SmsRejected`, `SmsUnavailable`), `live.py` (`LiveDevsms`), `fake.py` (`FakeSms`), `registry.py`
(`get_sms_port()`, `new_code()`). Selected by `DEVSMS_MODE` (`fake` by default, like every provider). The
`sms.send` handler is `accounts/tasks/otp_delivery.py`. Why the four universal templates are enough, and what the
migration from Eskiz changed: [`docs/sms-template-audit.md`](../sms-template-audit.md).

## Sources (read 2026-11-24)

- API documentation: https://devsms.uz/docs/api?lang=en (and the Uzbek page https://devsms.uz/docs/api).
- OpenAPI document of API v1: https://devsms.uz/docs/api/v1.json (`POST /sms`, `SendSmsRequest`, `SmsResource`).
- Guide for integrators: https://devsms.uz/docs/api/agents.md (v1 error codes, idempotency, webhooks).
- Prices: https://devsms.uz/pricing.

## Protocol

| Step | Call | Notes |
|---|---|---|
| send | `POST {DEVSMS_BASE_URL}/sms` (`https://devsms.uz/api/v1/sms`), `Authorization: Bearer <DEVSMS_API_TOKEN>`, `Idempotency-Key: <uuid>`, JSON `{"phone": "998901234567", "otp": {"template_type": 1-4, "service_name": "GlowDerma", "code": "482913"}}` | → `200 {"success": true, "data": {"id": 81234, "status": "sent", …}}`; `data.id` goes to `SmsCode.provider_message_id` |
| status (not called yet) | `GET {DEVSMS_BASE_URL}/sms/{id}` | statuses `pending`, `sent`, `delivered`, `failed`, `blocked` |

- **Universal OTP.** DevSMS keeps four pre-approved templates; the request names one and DevSMS renders the text
  (Uzbek; DevSMS documents no other language). No `message`, no `from`: the shared default sender **4546** is used
  and no Alpha Sender ID is registered. Template per purpose (the only place numbers appear,
  `live.TEMPLATE_TYPES`):

  | `OtpPurpose` | `template_type` | DevSMS text (`{service_name}`, `{otp_code}`) |
  |---|---|---|
  | `ACTION_CONFIRMATION` | 1 | "… {service_name} xizmatida amaliyotni tasdiqlash kodi: {otp_code}" |
  | `PASSWORD_RESET` | 2 | "… {service_name} xizmatida parolni tiklash uchun tasdiqlash kodi: {otp_code}" |
  | `REGISTRATION` | 3 | "… {service_name} xizmatiga ro'yxatdan o'tish uchun tasdiqlash kodi: {otp_code}" |
  | `LOGIN` | 4 | "… {service_name} xizmatiga kirish uchun tasdiqlash kodi: {otp_code}" |

  The stored purposes map in `accounts/tasks/otp_delivery.SMS_PURPOSES` (sign-up → 3, new device → 4, reset → 2,
  social completion and attaching a phone → 1; `verify_email` is e-mail only).
- **Bounds.** The code is 4–8 digits (ours: 6, from `secrets`); `service_name` is 2–50 letters, digits, spaces,
  dots and hyphens, checked at start (`DEVSMS_SERVICE_NAME`) and moderated by DevSMS; the phone is the 12-digit
  `998XXXXXXXXX` form of our `+998…` E.164 numbers (the only numbers the app accepts).
- **Idempotency.** Every request carries the code's key (`otp_delivery.sms_idempotency_key`: a UUIDv5 of the row's
  id and creation instant, the same for every delivery attempt of that code). A repeat with the same key and body
  sends nothing and charges nothing: DevSMS returns the first answer (`Idempotent-Replayed: true`). Keys never
  expire and are shared by the whole DevSMS account, which is why the creation instant is part of the key (staging
  and production may share an account; a restored database reuses ids).
- **Moderation.** DevSMS moderates `service_name` automatically: a refusal sends and charges nothing
  (`moderation_rejected`, with `meta.remaining_attempts`); after **20 refusals in a row** universal OTP is
  suspended for 24 hours (`otp_suspended`, `meta.blocked_until`). Keep `DEVSMS_SERVICE_NAME=GlowDerma` (the brand,
  nothing promotional).

## Outcomes (the `SmsPort` contract)

| DevSMS answer | Result | What `sms.send` does |
|---|---|---|
| 200 with `data.id` | `SmsDelivery` | stores the id |
| no answer (connect failure, timeout, broken body), a redirect, 5xx other than 502 below, 429 `rate_limited`, 409 `idempotency_request_in_progress`, 2xx without an id | `SmsUnavailable(sent=False)` | un-claims; the outbox repeats the request with the **same key and code** (DevSMS sends at most once) |
| 401 `unauthenticated` / `token_expired`, every other 403 (`forbidden` / `account_blocked` / `token_locked_by_plan`, an unknown code, an access-denied page) | `SmsUnavailable(sent=False)`, ERROR `devsms.auth_failed` | as above (configuration: fix the token) |
| 502 `provider_outcome_unknown` (charged, operator outcome unknown) | `SmsUnavailable(sent=True)` with `meta.sms_id` | keeps the claim, stores the id, never resends (a repeat only replays the 502; the user resends after 60 s) |
| 402 `insufficient_balance` | `SmsRejected("insufficient_balance")`, ERROR `devsms.insufficient_balance` | records `IntegrationState(devsms)` failure, never retries |
| 403 `moderation_rejected` | `SmsRejected("moderation_rejected")`, ERROR `devsms.service_name_rejected remaining_attempts=…` | as above |
| 403 `otp_suspended` | `SmsRejected("otp_suspended")`, ERROR `devsms.otp_suspended blocked_until=…` | as above |
| 422 `validation_failed` / `provider_rejected` / `unsupported_country` / `international_disabled`, 400, other 4xx | `SmsRejected(<error_code>)` | as above |
| a purpose without a template, a non-Uzbek number, a code outside 4–8 digits | `SmsRejected` before anything is sent | as above |

No phone number, code, token or DevSMS text is logged or put into an exception or `IntegrationState`: log lines
carry `sms_code_id`, `pii_ref(phone)`, the HTTP status and DevSMS's `error_code`.

## Environment

| Variable | Required when | Default | Secret |
|---|---|---|---|
| `DEVSMS_MODE` | — | `INTEGRATIONS_MODE` (`fake`) | no |
| `DEVSMS_API_TOKEN` | `DEVSMS_MODE=live` (production refuses an empty or placeholder value) | — | **yes** (Vault `secret/<env>/django`) |
| `DEVSMS_BASE_URL` | has a default; production requires https | `https://devsms.uz/api/v1` | no |
| `DEVSMS_SERVICE_NAME` | has a default; checked at start | `GlowDerma` | no |
| `DEVSMS_TIMEOUT_SECONDS` | has a default; 1–60 | `10` | no |
| `OTP_FAKE_CODE` | local/test settings only (production refuses it) | `123456` | no |

The token is a DevSMS personal API key with the `sms:send` ability (add `sms:read` for status checks); create it
in the DevSMS cabinet (`/app/api-keys`). No callback URL is configured: delivery callbacks are not handled (see
below).

## Not implemented (deliberately)

- **Delivery callbacks** (`callback_url`, `X-DevSMS-Signature` HMAC-SHA256 over `"{timestamp}.{raw body}"` with
  the `whsec_…` secret). Nothing in the app reads delivery states today (Eskiz had no receiving endpoint either);
  a webhook view must verify the signature, reject timestamps older than 5 minutes, and keep delivery state apart
  from OTP verification. A separate task when ops wants delivery reports.
- **Status reconciliation** of `provider_outcome_unknown`: the id is stored; `GET /sms/{id}` answers the outcome.
  No polling task: the user resends after 60 s anyway.
- **Automatic fallback** to another provider, custom templates, an Alpha Sender ID, and USMS (see the audit).

## Activation checklist

1. Create a DevSMS account for the company, top up the balance, create a personal API key (`sms:send`, `sms:read`).
2. Put `DEVSMS_API_TOKEN` in Vault `secret/production/django` (and staging if it should send real SMS), set
   `DEVSMS_MODE=live` there, keep the defaults of the other `DEVSMS_*` keys unless an override is needed; Actions →
   **Restart**; `GET /api/v1/health/?ready=1` (admin network) lists `devsms` as configured.
3. From a staff phone: register (template 3), sign in on a new device (4), reset the password (2), attach a phone
   to a social-only account (1). Each SMS arrives from 4546 with "GlowDerma" in the text.
4. Watch the first refusals: ERROR `devsms.service_name_rejected` (moderation) or `devsms.otp_suspended` must not
   appear; `devsms.insufficient_balance` means a top-up.
5. Rollback: set `DEVSMS_MODE=fake` in Vault and restart (no SMS is delivered while fake; codes in flight expire in
   two minutes). There is no route back to Eskiz in this release.
