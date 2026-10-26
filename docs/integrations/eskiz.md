# Eskiz — live adapter (M3)

> **Not verified against a live sandbox.** No Eskiz account exists yet. The adapter follows Eskiz's published
> Postman collection and the customer contract (§2.14.1); its tests replay the documented answers through
> `httpx.MockTransport`. Eskiz's test account only delivers its own test text, so the OTP template must be
> approved before a real code can be sent.

Code: `glowderma/integrations/eskiz/live.py` (`LiveEskiz`), the shared token cache
`glowderma/integrations/tokens.py`. Selected by `eskiz.registry.get_eskiz_port()` when `ESKIZ_MODE=live`; the
fake (`ESKIZ_MODE=fake`, default) is unchanged and `ESKIZ_FAKE_CODE` stays a fake-mode, local/test-only switch.

## Sources (read 2026-10-26)

- Eskiz's Postman collection "СМС шлюз от Eskiz.uz" (documenter.getpostman.com/view/663428/RzfmES4z; its JSON at
  `documenter.getpostman.com/api/collections/663428/RzfmES4z`): `auth/login`, `auth/refresh`,
  `message/sms/send` (fields, `callback_url` payload), `message/sms/status_by_id`.

## Protocol

| Step | Call | Notes |
|---|---|---|
| login | `POST {ESKIZ_BASE_URL}/auth/login`, form `email`, `password` → `{"data": {"token"}}` | token kept only in the Django cache `gd:eskiz:token` for 29 days; fetched single-flight across processes (`cache.add` lock) |
| refresh | `PATCH {ESKIZ_BASE_URL}/auth/refresh`, `Authorization: Bearer <old token>` → `{"data": {"token"}}` | only after a 401; a refused refresh falls back to a login |
| send | `POST {ESKIZ_BASE_URL}/message/sms/send`, form `mobile_phone` (digits, no `+`), `message`, `from` = `ESKIZ_SENDER`, `callback_url` = `ESKIZ_CALLBACK_URL` (omitted when empty) → `{"id": "<uuid>", "status": "waiting"}` | the id goes to `SmsCode.provider_message_id` |

Forms are `multipart/form-data`, as in the collection. A 401 on send renews the token once and repeats the send
(a 401 means Eskiz did not accept the message); a second 401 is a rejection, never a loop.

The text is sent exactly as `accounts.tasks.sms_text` renders it (catalog `sms.otp` in the request language, plus
the Android SMS Retriever hash line when configured). Eskiz delivers only texts that match an approved template
character for character — each language, and the variant with the hash line, needs its own approval.

## Outcomes (the `EskizPort` contract)

| Eskiz answer | Result | What `sms.send` does |
|---|---|---|
| 2xx with `id` | the id | stores it |
| HTTP 4xx (invalid number, no balance, unapproved text) | `EskizRejected` | logs, records `IntegrationState(eskiz)` failure, never retries |
| login refused (4xx) | `EskizRejected` + ERROR `eskiz.auth_failed` | as above |
| connect failure (nothing left the process; also a login that could not be reached) | `EskizUnavailable(sent=False)` | un-claims, the outbox retries |
| timeout, broken answer, HTTP 5xx, 2xx without `id` after sending | `EskizUnavailable(sent=True)` | keeps the claim, never resends (the user resends after 60 s) |

The adapter repeats a request only when it provably never left (connect failure), so a code is never sent twice
by it. No phone number, text, code, password or token is logged or put into an exception: log lines carry
`sms_code_id` and `pii_ref(phone)`, and Eskiz's own messages pass through `httpclient.safe_text`.

## Environment

| Variable | Required when | Example | Secret |
|---|---|---|---|
| `ESKIZ_MODE` | — | `live` | no |
| `ESKIZ_EMAIL` | `ESKIZ_MODE=live` | cabinet e-mail | no (an identifier) |
| `ESKIZ_PASSWORD` | `ESKIZ_MODE=live` (no placeholder) | cabinet secret | **yes** |
| `ESKIZ_SENDER` | `ESKIZ_MODE=live` (set explicitly) | approved nickname; `4546` is Eskiz's shared test sender | no |
| `ESKIZ_BASE_URL` | has a default; production requires https | `https://notify.eskiz.uz/api` | no |
| `ESKIZ_CALLBACK_URL` | optional; https when set | empty (no receiving endpoint exists yet) | no |

## Verify M3

1. **Error answers.** The collection shows no error example; the adapter maps by HTTP status only. Record what
   Eskiz answers for an invalid number, an empty balance and an unapproved text (status and body).
2. **Token lifetime and refresh.** The contract caches the token for 29 days; confirm the lifetime and that
   `PATCH /auth/refresh` accepts an expired token (else the fallback login covers it).
3. **Form encoding.** Multipart as in the collection; confirm that Eskiz accepts it for `send` and `login`.
4. **Templates.** Approve `sms.otp` in uz/ru/en (and the Android hash variants) exactly as rendered.
5. **Delivery reports.** `callback_url` is sent only when configured; no `webhooks/eskiz/` view exists (M2 left it
   optional). Adding one is a separate task.

## Sandbox checklist (once credentials exist)

- [ ] Staging: `ESKIZ_MODE=live`, e-mail, password, sender `4546` (test) — send the Eskiz test text through a
      shell call of `LiveEskiz.send` and check the id comes back.
- [ ] Wrong password → `EskizRejected` + ERROR `eskiz.auth_failed`; `IntegrationState(eskiz)` shows the failure.
- [ ] Delete `gd:eskiz:token` while sends run → one login only (single flight).
- [ ] Production nickname and approved templates: register a test phone through `auth/send-code/`; the SMS
      arrives with the exact catalog text.
- [ ] An invalid number → rejected, logged with `pii_ref` only.
