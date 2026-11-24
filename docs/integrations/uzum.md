# Uzum Bank Checkout

**Status:** fake by default (`UZUM_MODE=fake`). Going live needs a contract with Uzum, a terminal, credentials and the decisions in [Open questions](#open-questions).

**Spec:** Uzum Checkout API v1.10.3. Source: `https://developer.uzumbank.uz/en/redocusaurus/en_checkout.yaml`.

**Code:**
- Adapter: `glowderma/integrations/uzum/`
- Flows: `glowderma/commerce/uzum_flow.py`
- Callback: `glowderma/commerce/api/webhooks/uzum.py`

## Why Checkout and not the Merchant API

Uzum offers two products:

- **Checkout:** we register a payment and send the customer to Uzum's hosted card form. The form takes Uzcard, Humo, Visa and Mastercard. This works from our website and from our app, through the same redirect → `/pay/return` → poll flow that Click and Payme use.
- **Merchant API:** the customer starts the payment inside the Uzum Bank app and types an order code there. Uzum only calls our webhooks. We cannot start a payment, check a status or refund, and no deep link or prefill is documented.

We use **Checkout only**. The Merchant API could later become a separate channel with its own provider value.

## Flow

1. **Register the payment.** `POST orders/{id}/pay/ {"provider": "uzum"}` calls `POST /api/v1/payment/register` under the Order lock, with a 4 s bound.
   - The request is `ONE_STEP` with `viewType=REDIRECT`.
   - `amount` is in **tiyin**: `Payment.amount × 100`.
   - `orderNumber` is `<UZUM_ORDER_PREFIX>-<payment id>-<8 hex>`, fresh for every register call.
   - `clientId` is an HMAC of the customer that changes on a dormant reset or a re-registration. It is never the phone number.
   - `Content-Language` is `uz-UZ`, `ru-RU` or `en-EN`.
   - Both return URLs are our usual `return_url`.
   - Uzum's `orderId` becomes the payment's provider transaction id. `provider_state` stores the order number, the registration time, the session end and the form URL.
2. **Repeated taps.**
   - While the form's session lives, every further tap returns the stored form, so a second form is never created.
   - Once the session is over, the payment row fails `abandoned` and a new row registers a new form.
   - A form is only registered while `now + UZUM_SESSION_TIMEOUT_SECS + 120 s ≤ reservation_expires_at + PAYMENT_IN_FLIGHT_GRACE_SECONDS`; otherwise the answer is 409 `order_expired`. This keeps every form's end inside the expiry grace.
   - `pay_by` is the earlier of the reservation end and the form's end.
3. **Register failures.** If Uzum does not answer or refuses, the client gets 503 `provider_unavailable` and nothing is stored, because the request rolls back. A form whose answer was lost is held by nobody, and the next tap uses a new order number.
4. **Callbacks.** Uzum's callback (`POST webhooks/uzum/?token=…`) carries no amount and no signature, so it is only a hint. The view does this:
   1. Checks the token in constant time.
   2. Checks the optional IP allow-list.
   3. Matches our payment by order number (with this environment's prefix) and by Uzum's `orderId`.
   4. Stores an allow-listed inbox row.
   5. Enqueues `uzum.sync_order` and answers 200.

   No Uzum call runs inside the request.
5. **Status sync.** `uzum.sync_order` and the beat `commerce.reconcile_uzum` (every 2 min) call `getOrderStatus` with no lock held. `apply_status` then decides under the Order lock.

   The payment is honoured only if all of these hold:
   - the status is `COMPLETED`;
   - `merchantOrderId` is our order number;
   - `amount` and `completedAmount` equal the payment in tiyin **exactly**;
   - nothing has been refunded or reversed.

   If those hold, `late_capture_case` picks the outcome: `mark_paid`, or a late capture with an automatic refund.

   The other statuses:

   | Status | Effect |
   |---|---|
   | `DECLINED` | `payment_failed` with code `declined` |
   | `REGISTERED`, session over | `payment_failed` with code `session_expired` |
   | Anything else | Alert, no change |

6. **Reconcile.** The beat covers:
   - pending Uzum payments;
   - Uzum payments we failed in the last 24 h, which catches a capture whose five callbacks were all lost;
   - Uzum refunds still `submitting`.

## State rule

`commerce.selectors` and `payments.services.can_fail_unilaterally` apply these rules:

- **Attached:** an Uzum payment is attached at register (`registered_at`).
- **Open:** it stays open while pending.
- **In flight:** until `session_expires_at + 120 s`.
- While in flight, the payment blocks:
  - the customer cancel (409 `payment_in_progress`);
  - `DELETE me/`;
  - a provider switch;
  - a Click Prepare;
  - a Payme Create.
- **Unilateral failure** (expiry, admin cancel, switch): refused while the form may still take money, so the row stays `pending`. A capture that lands after an admin cancel becomes a late capture and is refunded automatically.

## Refunds

Refunds are automatic, through the `refund.submit` handler's Uzum branch:

- The request is `POST /api/v1/acquiring/refund` with `X-Operation-Id = uuid5(refund id)`. Every resend for a refund row uses the same id, and Uzum answers 3028 to a duplicate.
- `errorCode 0` (and 3028) means **accepted**. The refund stays `submitting` until `getOrderStatus.operations` shows the operation `SUCCESS` (→ `succeeded`) or `FAIL` (→ `failed`).
- If the answer is lost, the refund stays `submitting`. The reconciler resends it after 5 min with the same operation id.
- 3021 (wrong amount) is checked against `refundedAmount` first.
- Any other code fails the refund with `uzum_<code>` for staff (retry or resolve). 3022 and 3045 also raise an alert.
- With Uzum fake under the production settings, new refunds are `manual_required` with reason `uzum_fake`.

## Settings

| Variable | Default | Notes |
|---|---|---|
| `UZUM_MODE` | `fake` | Never inherits `INTEGRATIONS_MODE`. |
| `UZUM_CHECKOUT_BASE_URL` | — | https. Required when live. Uzum issues one per environment; the spec documents no host. |
| `UZUM_TERMINAL_ID` | — | Required when live. |
| `UZUM_API_KEY` | — | Required when live. Secret. |
| `UZUM_CALLBACK_TOKEN` | — | Required when live, at least 32 URL-safe characters. Secret. Part of the terminal's callback URL. |
| `UZUM_ALLOWED_IPS` | empty | Optional, CIDR list. |
| `UZUM_REDIRECT_HOSTS` | empty | Optional allow-list for the payment page host. |
| `UZUM_SESSION_TIMEOUT_SECS` | `600` | Must be between 600 and 1800. |
| `UZUM_ORDER_PREFIX` | `gd` | Use one prefix per environment (for example `stg` on staging). |

The keys are **not** in `PROVIDER_SECRETS`, so deployments without Uzum keys keep booting.

**Go-live order:**
1. Load all the keys into Vault.
2. Set `UZUM_MODE=live` last.
3. Run `migrate`.
4. Restart the API and the worker, and restart beat so it picks up `commerce-reconcile-uzum`.
5. Make one real low-value payment and one refund.

## Fiscal receipts

Not sent. Uzum's auto-fiscalization needs a cart with per-product IKPU, package code and VAT data; the catalog holds none of these, and the terminal must have the option switched on. Until the accountant decides who issues receipts for online sales (Uzum, the Fiscalization API, or Billz), no cart is sent. A cart sent while the terminal's auto-fiscalization is off is refused with 3058. Go-live waits on that decision.

## Open questions

These need answers from Uzum:
- Production and test base URLs, and test cards. The documented test cards expire 10/26 and 09/26.
- Can the callback URL carry our `?token=`? Does Uzum publish source IPs? What are the retry interval and timeout?
- With ONE_STEP, does a successful payment report `AUTHORIZE` or `COMPLETE`? (We never dispatch on it.)
- The refund window (error 3022).
- Does the hosted form show saved cards for a known `clientId`?
- Hosting location and legal entity, for `docs/ops/compliance.md`.
