# Payme — live adapter (M3)

> **Not verified against a live sandbox.** No Payme cash desk exists yet. The adapter follows Payme's public
> developer documentation and the customer contract (§2.7.2); tests decode the URLs it builds.

Code: `glowderma/integrations/payme/live.py` (`LivePayme`), the URL builder `payme/protocol.py`
(`checkout_url`). Selected by `payme.registry.get_payme()` when `PAYME_MODE=live`; the fake is unchanged.

## Scope

Payme's money flow is inbound: Payme calls our Merchant API (JSON-RPC, the M2 webhook view
`commerce/api/webhooks/payme.py`) and refunds are manual (`manual_required`, confirmed by Payme's
`CancelTransaction`). The live adapter therefore makes **no HTTP call**; it provides:

| Port member | What it does |
|---|---|
| `checkout_url(account_ref, amount_tiyin, lang, return_url)` | `{PAYME_CHECKOUT_URL}/{base64("m={PAYME_MERCHANT_ID};ac.{PAYME_ACCOUNT_FIELD}={account_ref};a={tiyin};l={lang};c={urlencoded return_url};ct=15000")}` |
| `is_configured()` | the merchant id and `PAYME_KEY` are set and not placeholders |
| `refunds_are_manual` | `True` |

## Sources (read 2026-10-26)

- developer.help.paycom.uz — "Инициализация платежей → Отправка чека по методу GET": URL format
  `<checkout_url>/base64(params)`, separator `;`, parameters `m` (merchant id or alias), `ac.<field>`, `a`
  (tiyin), `l` (`ru`, `uz`, `en`), `c` (return URL), `ct` (wait in ms before returning), `cr` (currency).
- developer.help.paycom.uz — "Песочница": checkout of the sandbox `https://test.paycom.uz`, of production
  `https://checkout.paycom.uz`; the cabinet issues `key` (production) and `TEST_KEY` (sandbox).

## Host by mode

`PAYME_TEST_MODE=true` makes the default checkout host `https://test.paycom.uz`; otherwise it is
`https://checkout.paycom.uz`. An explicit `PAYME_CHECKOUT_URL` overrides either. In the sandbox `PAYME_KEY` must be
the cabinet's `TEST_KEY` (the callbacks' Basic auth), in production the `key`.

## Environment

| Variable | Required when | Example | Secret |
|---|---|---|---|
| `PAYME_MODE` | — | `live` | no |
| `PAYME_MERCHANT_ID` | `PAYME_MODE=live` (production: letters, digits, `_`, `-` only, no placeholder) | `587f72c72cac0d162c722ae2` | no |
| `PAYME_KEY` | always in production (unchanged from M2) | cabinet `key` / `TEST_KEY` | **yes** |
| `PAYME_TEST_MODE` | optional | `true` on staging | no |
| `PAYME_CHECKOUT_URL` | optional override; production with `PAYME_MODE=live` requires https | `https://checkout.paycom.uz` | no |
| `PAYME_ACCOUNT_FIELD` | unchanged from M2 (must match the cabinet's account field) | `order_id` | no |

## Verify M3

1. **`c` encoding.** The contract URL-encodes the return URL inside the base64 parameters (so its `;` and `=`
   never split a parameter). Payme's page does not say whether it decodes `c`; check that the sandbox
   returns the customer to `https://glowderma.uz/pay/return?order_id=…` and not to an encoded string.
2. **`ct=15000`** (15 s wait before returning) and `l` values (`uz`, `ru`, `en`) as the page shows.
3. **Merchant id vs alias.** `m` accepts the id or an alias; we send the id of the cash desk.
4. The JSON-RPC callbacks (error codes such as `-31051`, open question M2-P10) are verified with the
   test.paycom.uz checklist; they are not part of this adapter.

## Sandbox checklist (once credentials exist)

- [ ] Staging: `PAYME_MODE=live`, `PAYME_TEST_MODE=true`, `PAYME_MERCHANT_ID`, `PAYME_KEY=<TEST_KEY>`.
- [ ] `orders/{id}/pay/` with `provider=payme` → open `web_url`; the sandbox page shows the order's amount and
      account reference.
- [ ] Run the test.paycom.uz scenarios (create/cancel, create/perform/cancel) against the webhook.
- [ ] After paying, the page returns to `PAYMENT_RETURN_URL?order_id=…` (check the `c` decoding).
- [ ] Production: `PAYME_TEST_MODE` unset, `PAYME_KEY=<key>`; one real low-value payment end to end.
