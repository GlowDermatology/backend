# Click — live adapter (M3)

> **Not verified against a live sandbox.** No Click credentials exist yet. The adapter is written from Click's
> public documentation and the customer contract (§2.7.2, §2.13.0); its tests replay the documented answers
> through `httpx.MockTransport`. Every point marked **verify M3** must be checked with real credentials before
> `CLICK_MODE=live` goes to production.

Code: `glowderma/integrations/click/live.py` (`LiveClick`), protocol constants in
`glowderma/integrations/click/protocol.py` (Merchant API section), port in `click/ports.py`. Selected by
`click.registry.get_click()` when `CLICK_MODE=live`; the fake (`CLICK_MODE=fake`, the default) is unchanged.

## Scope

Outbound only. The SHOP API callbacks (Prepare / Complete) are the M2 webhook views
(`commerce/api/webhooks/click.py`); this adapter adds what the refund handler and the staff resolve need:

| Port method | Click call | Used by |
|---|---|---|
| `payment_status(click_paydoc_id)` | `GET {CLICK_MERCHANT_API_URL}/payment/status/{service_id}/{payment_id}` | `commerce.tasks.submit_refund` (before any reversal, after a refusal, after an unknown outcome), `commerce.services.resolve_refund` (fresh check of a failed refund) |
| `refund(click_paydoc_id, amount)` | `DELETE {CLICK_MERCHANT_API_URL}/payment/reversal/{service_id}/{payment_id}` | `commerce.tasks.submit_refund` |
| `pay_url(account_ref, amount, return_url)` | none (URL only): `{CLICK_PAY_URL}?service_id=…&merchant_id=…&amount=…&transaction_param=…&return_url=…` | `orders/{id}/pay/` (§2.7.2) |

`payment_id` is our `Payment.provider_state["click_paydoc_id"]` (the Click payment document id from Prepare),
never `click_trans_id` (contract §2.13.0). The reversal has no amount: it reverses the whole payment, which is
what every Click refund is (cancel or refused capture).

## Sources (read 2026-10-26)

- docs.click.uz — Merchant API → Requests (`/en/merchant-api/requests`): Auth header, required headers,
  payment status, status by `merchant_trans_id`, payment reversal and its conditions.
- docs.click.uz — Merchant API → Errors (`/en/merchant-api/errors`): HTTP statuses 200 and 201 OK, 400, 401, 403,
  404, 406, 410, 500, 502.
- docs.click.uz — Click Pass (`/en/merchant-api/click-pass`, "Payment Status Codes"): `< 0` error, `0` created,
  `1` processing, `2` successful.
- docs.click.uz — Click button (`/click-button`): the pay page URL and its parameters.
- github.com/click-llc/click-integration-php (Click's own library, `README.md`, `click/models/Payments.php`,
  `click/models/BasePayments.php`): one `secret_key` configured for both APIs; it reads `-99` as rejected
  (cancelled), and its README pairs `payment_id` 1111 with `click_trans_id` 1111 (see Verify M3 item 4b).

## Auth

Every request carries `Accept: application/json`, `Content-Type: application/json` and
`Auth: {CLICK_MERCHANT_USER_ID}:{sha1(timestamp + CLICK_SECRET_KEY)}:{timestamp}` (lower-case hex SHA-1,
10-digit UNIX seconds of the real clock). The secret is the service's `secret_key` — the same value that
signs the SHOP API callbacks (`CLICK_SECRET_KEY`). It is never put in a URL or a log line.

## Answer mapping

Any 2xx is an answer (Click documents 201 as well as 200).

| Click answer | `payment_status` | `refund` |
|---|---|---|
| the request never left (connect failure), or an answer that says it was **not processed**: a redirect (never followed), 408 / 425 / 429, 401 / 403 | `ClickNotProcessed` | `ClickNotProcessed`: the handler releases its claim (`submitting → requested`) and the outbox retries after a fresh status check |
| no usable answer after the request may have left (timeout, broken or undecodable body, anything unexpected), HTTP 5xx | `ClickUnavailable` | `ClickUnavailable` (outcome unknown) |
| another HTTP 4xx (unknown payment, bad request) | `ClickStatusUnknown` | `ClickRefundRejected` with Click's `error_code` when its body carries one, else `http_<status>` |
| 2xx that is not a JSON object with an integer `error_code` (strictly ASCII digits) | `ClickStatusUnknown` | `ClickUnavailable` (outcome unknown) |
| 2xx, `error_code ≠ 0` | `ClickStatusUnknown` | `ClickRefundRejected(str(error_code), error_note)` |
| 2xx, `error_code = 0` | `payment_status` `2` → `paid`; `-99` → `reversed` (verify M3); anything else (`1` processing included) `ClickStatusUnknown` | `RefundReceipt(provider_refund_id = payment_id)` |

`ClickNotProcessed` and `ClickStatusUnknown` are subclasses of `ClickUnavailable`, so a status check that fails
either way reads "Click's state is unknown": the refund handler leaves a `requested` refund for the outbox retry,
turns a `submitting` one `unknown` (staff alert, never retried automatically), and the staff resolve answers 503
`provider_unavailable`.
An unverified status code can therefore never close a refund (`reversed`) or send a payout (`paid`).

What the refund state machine gets from this (spec: "status check before any reversal retry; `unknown` is never
retried automatically"):

- `requested`: status check first — `reversed` (a reversal that went through before a crash) closes it
  `succeeded` without a second reversal; `paid` → claim → reversal.
- reversal refused (`error_code ≠ 0` or 4xx) → a second status check: `reversed` → `succeeded`, otherwise
  `failed` with Click's code (staff retry or pay out, admin §2.12).
- reversal outcome unknown (timeout, 5xx, unreadable answer) → status check: `reversed` → `succeeded`;
  `paid` or unknown → `unknown`.
- reversal not processed (`ClickNotProcessed`) → the claim is released (`submitting → requested`, fenced by the
  attempt) and the message raises: the outbox retries with a status check first; a message that dies finds the
  refund `requested` → `failed` (`dead_letter`).
- **any other exception after the claim** (a Celery soft time limit, a bug) → `unknown` at once and the exception
  propagates: never a refund left `submitting`, never a second automatic reversal after the lease.
- the adapter itself repeats a request only when it provably never left (connect failure), so a reversal is
  never sent twice by the adapter.

A 401/403 logs ERROR `click.auth_failed` (wrong `CLICK_MERCHANT_USER_ID` or secret, or a clock far off).
Provider messages pass through `httpclient.safe_text` (digit runs of seven or more — phones, card fragments —
are masked) before they reach a stored error or a log line.

## Environment

| Variable | Required when | Example | Secret |
|---|---|---|---|
| `CLICK_MODE` | — | `live` | no |
| `CLICK_SERVICE_ID` | `CLICK_MODE=live` (production: digits, not `00000`) | `36123` | no |
| `CLICK_MERCHANT_ID` | `CLICK_MODE=live` (same rule) | `20456` | no |
| `CLICK_MERCHANT_USER_ID` | `CLICK_MODE=live` (same rule) | `40789` | no |
| `CLICK_SECRET_KEY` | always in production (unchanged from M2) | issued by Click | **yes** |
| `CLICK_MERCHANT_API_URL` | has a default; production requires https | `https://api.click.uz/v2/merchant` | no |
| `CLICK_PAY_URL` | has a default; production with `CLICK_MODE=live` requires https | `https://my.click.uz/services/pay` | no |

Timeouts are not configurable on purpose: 3 s to connect and 10 s per call are normative for every port (the
refund handler relies on no reversal being in flight after the five-minute outbox lease).

## Verify M3

1. **`payment_status` codes.** The Click Pass page documents `< 0` error, `0` created, `1` processing, `2`
   successful; the adapter reads only `2` as paid. `-99` = reversed is a guess (it is the invoice status
   "Deleted" in Click's docs and what Click's PHP library treats as cancelled): confirm in the sandbox what a
   reversed payment reports, then adjust `protocol.MERCHANT_STATUS_REVERSED` / `merchant_payment_state`.
2. **Secret of the Merchant API.** One `secret_key` per service signs both APIs (docs list one; Click's library
   uses one). Confirm that a status call signed with `CLICK_SECRET_KEY` is accepted.
3. **Reversal error codes.** No list is documented. Record the codes the sandbox answers for a reversal of a
   previous-month payment and of an offline (Humo/UZCARD) card, and what a second reversal of the same payment
   answers (the handler double-checks the status either way).
4. **`provider_refund_id`.** The reversal answers only `payment_id`; it is stored as the refund's provider id.
   4b. **`payment_id` = `click_paydoc_id`.** Click's PHP README pairs `payment_id` 1111 with `click_trans_id` 1111;
   confirm in the sandbox which id of the Prepare callback the Merchant API's `payment_id` is.
5. **Pay URL.** The button docs use `https://my.click.uz/services/pay/` (trailing slash) and describe `amount` as
   `N.NN`, while their HTML example sends `1000`; the contract sends so'm without decimals. Check both against
   the sandbox page; `merchant_user_id` and `card_type` are optional and not sent.
6. **IP allow-list.** If Click requires our egress IP to be registered for the Merchant API, add it in the
   cabinet.

## Sandbox checklist (once credentials exist)

- [ ] Set `CLICK_MODE=live`, the three ids, `CLICK_SECRET_KEY` on a staging host (production settings).
- [ ] Pay a test order through the pay page; check that Prepare/Complete arrive and the payment stores
      `click_paydoc_id`.
- [ ] `payment_status` of that payment → note `payment_status` (expected positive).
- [ ] Cancel the order in admin → `refund.submit` → reversal `error_code 0` → refund `succeeded`; a new
      `payment_status` → note the code (expected `-99`).
- [ ] Reverse the same payment again → note the refusal code; the refund handler must end `succeeded` (status
      check) and never pay twice.
- [ ] Wrong secret → 401/403 → ERROR `click.auth_failed`, the refund goes back to `requested` and is retried
      (dead after ten attempts → `failed` `dead_letter`).
- [ ] A staff resolve of a `failed` Click refund performs a status check (both outcomes).
- [ ] Update `protocol.py` constants and this page with the observed codes; remove the "verify M3" marks that
      were confirmed.
