# Monitoring, alerts and the nightly consistency report (operator runbook)

Status: M3 operations package, 2026-10-27. Companion of `docs/ops/compliance.md` (personal data, hosting,
retention) and `docs/ops/deploy.md` (the Compose stack, first deploy and upgrades, logs, backups).

The backend gives an operator four signals:

1. **ERROR log lines** with a stable event name first (`billz.sale_dead order_id=10248 …`), one line per event,
   key=value formatted with a `request_id` (§2). Alert on these from whatever reads the container logs.
2. **The alert webhook** (optional): the events of §2 marked *webhook* are also POSTed to `ALERT_WEBHOOK_URL`
   (§1).
3. **The nightly consistency report** (03:40 Tashkent): read-only checks K1–K6 and the loyalty ledger
   reconciliation, audited and alerted (§3).
4. **`GET /api/v1/health/`** and **`?ready=1`** for probes (§4).

The admin audit log (`GET audit-log/`) keeps the durable trail: `outbox.dead`, `report.consistency`,
`loyalty.reconciliation_*`, `outbox.purged`, `integrations.inbox_purged`, `outbox.requeued`, `outbox.resolved`.

**Dead messages are kept until resolved.** Retention purges only `done` outbox rows; a `dead` one stays (it is
what `requeue_outbox` replays and what K1/K6 list) until staff either replay it (`requeue_outbox --topic <topic>
--dead [--id N]`) or, when they handled it by hand (a sale entered in Billz, a refund paid out in a cabinet),
close it: first `manage.py requeue_outbox --topic <topic> --dead --resolved --id <message id> --dry-run` (check the
listed message: `--resolved` marks it done without running its handler), then the same without `--dry-run` (audited
`outbox.resolved`; the row becomes `done` and is purged with the others after `OUTBOX_RETENTION_DAYS`). A
`billz.push_sale` closes only once its sale is recorded with `billz_mark_recorded` (docs/integrations/billz.md §7).

## 1. Alert webhook

| Setting | Default | Notes |
|---|---|---|
| `ALERT_WEBHOOK_URL` | empty = disabled | https only under the production settings. Treat it as a secret (it often embeds a token); it is never logged |
| `ALERT_WEBHOOK_SECRET` | empty | required (≥ 32 characters, no placeholder) under the production settings when the URL is set |
| `ALERT_DEDUPE_MINUTES` | 30 | one alert per event and scope per fixed window of this many minutes; the scope is the topic for `outbox.dead`, the order for the per-order Billz events (`billz.sale_dead`, `billz.refund_by_hand`, `billz.sale_of_cancelled_order`, `billz.sale_state_unclear`, `billz.sale_total_differs`: two orders → two alerts) |
| `ALERT_ENV` | `production` (production settings), `local` otherwise | the `env` of every alert, e.g. `staging` |

**Request.** `POST <ALERT_WEBHOOK_URL>` with `Content-Type: application/json`, `User-Agent: glowderma-alerts/1`
and `X-GD-Signature: sha256=<hex>` = HMAC-SHA256 of the exact body bytes with `ALERT_WEBHOOK_SECRET`. The body is
compact JSON with sorted keys:

```json
{"at":"2026-10-27T03:40:12+05:00","env":"production","event":"report.consistency_mismatch","fields":{"counts":{"k3_refund_open":2}},"host":"worker-1"}
```

`at` is when the event happened (Tashkent), `host` the container that raised it. `fields` hold ids, counts and
short codes only, enforced by an **allow-list** (`core.alerts.scrub_fields`):

- a value is sent only when it is a bool, `null`, an integer of fewer than seven digits, a finite number below
  one million, or a short code (`^[A-Za-z0-9_.:-]{1,64}$` with no run of seven or more digits, separators
  included: `90_123_45_67` is masked). NaN and infinities become `null`; everything else (an e-mail, a phone, a
  long number, a date, free text, a name with a space) becomes `<redacted>`, inside lists and nested objects
  too (at most three levels);
- keys pass the same code rule: a key that is not a short code (`+998901234567`, `malika@gmail.com`) is dropped
  and the object gets `dropped_keys: <n>`;
- a key naming personal data or a credential keeps its name but its whole value (an object included) becomes
  `<redacted>`: it contains `auth`, `token`, `secret`, `password`, `credential`, `card`, `msisdn`, `phone`,
  `mobile`, `email`, `name`, `recipient`, `contact`, `address`, `passport`, `pinfl` or `birth`, or has one of
  the words `key`, `otp`, `pin`, `cvv`, `cvc`, `pan`, `tel`, `fio`, `ip`, `dob`, `inn` (`api_key`, `client_ip`,
  `pinCode`).

An id of seven or more digits (an outbox message id past 999 999) is therefore masked too: the ERROR line and
the audit rows keep it. Verify on the receiver:

```python
expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-GD-Signature"])
```

**Delivery.** `alert()` queues one outbox message `alert.send` (dedupe key `alert:<event>:<scope>:<window>`) and
never breaks the code that raised it: a failure to queue is logged at WARNING (`alert.enqueue_failed`). The
dispatcher POSTs it with a 5 s timeout per connect/read/write and a 15 s deadline for the whole request (far inside
the 5-minute outbox lease, so a slow receiver never gets the alert posted twice in parallel), no redirects: 2xx →
done; a transport error, a missed deadline, 408, 429 or 5xx → retried with
backoff (dead after 10 attempts, about 17 minutes); any other status → dead at once. A dead `alert.send` raises
no further alert. An alert raised inside a transaction that rolls back is rolled back with it — the ERROR line
stays, which is why log-based alerting is the primary path and the webhook the push channel.

**Compliance.** The receiver must be hosted in Uzbekistan or receive no personal data (`docs/ops/compliance.md`
§1.2). The alert fields are designed to carry none; keep it that way when adding alerts (ids and counts only).

**The receiver: the Telegram bot** (repository `bot`, `GlowDerma alert bot`). A one-way notifier the operator runs as
its own service: `POST /hooks/alerts` verifies `X-GD-Signature` with the same `ALERT_WEBHOOK_SECRET`, refuses stale
or replayed bodies, renders one Uzbek message per event (order number, kind, code, deep links to the admin panel)
and posts it to `TELEGRAM_CHAT_ID` with `sendMessage`; a Telegram failure answers 502, so the outbox retries.
Point `ALERT_WEBHOOK_URL` at `https://<bot host>/hooks/alerts`. It needs `TELEGRAM_BOT_TOKEN` (BotFather),
`TELEGRAM_CHAT_ID` (`python -m glowderma_bot chat-id` finds it) and optionally `ADMIN_BASE_URL`,
`BOT_ENV_ALLOWLIST`; its README lists its answers and how each maps to the outbox outcome. Telegram receives what
the alert carries: ids and short codes. With Yandex Delivery live and no `ALERT_WEBHOOK_URL`, every process logs a
startup WARNING (§2.1): urgent delivery issues then reach the admin panel only.

## 2. Events to alert on

*Webhook* = also sent through §1. The others are ERROR (or, where noted, WARNING) lines only — alert on them from
the logs. Ids in the lines are order, payment, refund, purchase, message or account ids; phones appear only as
`pii_ref` hashes.

| Event | Webhook | Meaning | What to do |
|---|---|---|---|
| `outbox.dead {topic, message_id, attempts}` | yes (scope: topic) | an outbox message exhausted its retries or was refused for good; audit `outbox.dead` holds `last_error` | Read the audit row (`audit-log/?action=outbox.dead`). `bts.create_shipment` → the shipment is `failed`: *Retry shipment* on the order. `refund.submit` → the refund is `failed` (`dead_letter`): retry it on the Payments screen. `billz.push_sale` → see `billz.sale_dead`. `push.*`, `campaign.send_batch` → FCM: check the FCM project and credentials. `sms.send` → Eskiz: check balance and credentials. `apple.revoke` → revoke by hand in the Apple developer account if needed. Once the cause is fixed, replay with `manage.py requeue_outbox --topic <topic> --dead --dry-run`, then without `--dry-run` |
| `report.consistency_mismatch {counts}` | yes | the nightly report found rows to look at (§3) | `manage.py consistency_report` (or the `report.consistency` audit row) lists the ids; follow §3.2 per check |
| `loyalty.reconciliation_mismatch {mismatches, checks}` | yes | a cashback balance does not add up (never corrected automatically) | The `loyalty.reconciliation_mismatch` audit rows name the accounts and the check. A mismatch means a bug or a raw database write: escalate to the developers; staff correct a balance only through a cashback adjustment with a reason |
| `outbox.dead` of `campaign.send_batch` (`notification.push_dead` + `notification.failed` audit rows) | yes (scope: topic) | a campaign batch died (ten failures, or deferred past the 6 h window): the campaign is closed `partially_sent` (some pushes went out) or `failed`, its pending recipients `stopped`, `last_error` on the campaign | Check the FCM project and credentials (§2 `outbox.dead`). The campaign is not retried: once FCM is back, a superuser creates a new draft for the recipients left out (the `stopped` recipients of the closed campaign name them), tests and approves it |
| `billz.sale_dead {order_id, purchase_id, message_id}` | yes (scope: order) | a paid online order's Billz sale was refused for good or failed ten times | The reason is in the ERROR line and the `outbox.dead` audit row. Fix the cause (a configuration error, a product missing in Billz, a closed shift that outlived `BILLZ_SHIFT_RETRY_MAX_HOURS`), then replay (`requeue_outbox --topic billz.push_sale --dead`, dry run first: never replay a sale someone entered in Billz by hand meanwhile) — or complete the draft / enter the sale in Billz by hand |
| `billz.configuration_error {case, http_status[, error_code]}` (`case`: `key_refused` — the login refused the integration key; `refused` — a route or setting) | yes | Billz refused the integration key, a route or a setting (redirect, 403, unknown route, cash box of another shop, seller without access) | Check `BILLZ_SECRET_KEY`, `BILLZ_SHOP_ID`, `BILLZ_CASHBOX_ID`, `BILLZ_PAYMENT_TYPE_ID`, `BILLZ_BASE_URL` and the key's permissions in Billz. Sales keep retrying meanwhile; replay the dead ones after the fix |
| `billz.circuit_open {failures, open_seconds}` | yes | five Billz failures in a row: calls fail fast for 90 s | Usually transient. If it repeats, check Billz's status and the network from the host; sales and syncs resume on their own |
| `billz.cache_unavailable {operation, error}` | yes (one per window however many calls fail) | Redis (the shared cache) is down: nothing is sent to Billz | Check the redis container (`scripts/manage.sh <env> ps`, memory, disk for AOF); `health/?ready=1` shows `cache: error` |
| `billz.sale_state_unclear {order_id}` | yes (scope: order) | a Billz sale reads both paid and open: nothing is paid | Open the sale in Billz (`billz_order_id` in the ERROR line) and make it consistent; the push retries and dies after ten attempts if it stays unclear (then `billz.sale_dead`) |
| `billz.sale_total_differs {order_id}` | yes (scope: order) | Billz bills another amount than the customer paid (a Billz price or client discount): nothing is paid | Fix the draft in Billz (price, discount) and replay, or complete it by hand (decision D1) |
| `billz.refund_by_hand {order_id, purchase_id, pushed}` | yes (scope: order) | an order whose Billz sale exists was cancelled; Billz has no refund API | Refund the sale in Billz (`pushed=True`) or delete its draft (`pushed=False`) by hand |
| `billz.sale_of_cancelled_order {order_id, purchase_id, case}` | yes (scope: order) | the push met a cancelled order: `push_stopped`, `cancelled_during_push` (nothing paid) or `paid_after_cancel` | Delete the draft in Billz; for `paid_after_cancel` refund the sale in Billz by hand |
| `billz.sale_rejected` | no (followed by `billz.sale_dead`) | Billz refused a sale for good | as `billz.sale_dead` |
| `payment.late_capture {order_id, payment_id, provider, case}` | yes (scope: payment; after the commit) | money was captured for an order we could not honour; a refund was created | Follow the refund on the Payments screen (K5 checks it after 24 h) |
| `providers.fake_with_open_money {provider, open_transactions, open_refunds, order_ids}` | yes (scope: provider) | a payment provider switched to fake still holds open money (worker start) | Reconcile in the provider's cabinet or switch it back to live (customer contract §6.14) |
| `otp.budget_tripped {limit}` | yes (once per window; from `auth/send-code/` — on `auth/login/` the alert rolls back with the refused request, the ERROR line stays) | the global SMS budget is spent for this hour: sign-up and reset stop | Check the per-IP counters, block abusive networks at the edge, raise `SMS_GLOBAL_HOURLY_LIMIT` only for real traffic |
| `console.2fa_enrolled {staff_id, window_audit_id, via}` (WARNING) | yes (scope: staff) | a staff member set up a new TOTP device for the ops console | Expected after `ops_access grant` / `reset-2fa` or a manager's console reset; otherwise treat as an account takeover: the audit row names who opened the window (`window_opened_by`), then `ops_access revoke` and `resetstaffpassword` (docs/ops/staff-access.md) |
| `console.otp_locked {staff_id, wait}` (WARNING) | yes (scope: staff) | the right console password followed by five wrong codes: a guessed or leaked password, or a lost phone | Ask the person; if it was not them, `resetstaffpassword` and `ops_access reset-2fa` |
| `auth.mfa_locked {staff_id, wait[, failures]}` (WARNING) | yes (scope: staff) | the admin panel's MFA step: a correct password (or social sign-in) followed by five wrong codes in a minute, or by ten wrong codes in a row at any pace (`failures`: the count, sent when that lock starts — a slow guesser), or wrong codes in the MFA settings — a guessed or leaked password, or a lost phone | Ask the person; if it was not them, `resetstaffpassword` (docs/ops/staff-access.md "Admin panel MFA") |
| `auth.mfa_reset {staff_id, via}` (WARNING) | yes (scope: staff) | a manager (`via` api) or the operator (`via` cli) removed a staff member's admin panel MFA | Expected after a lost-phone request; otherwise the `auth.mfa_reset` audit row names the actor: check for a `staff.password_reset` of the same account by the same person (they could sign in as them) and `resetstaffpassword` |
| `console.ip_locked {step, wait}` (WARNING) | yes (scope: step) | one address used up its console sign-in failure budget (`OPS_IP_FAILURE_LIMIT`) | The address is in the `console.ip_locked` audit row; block it at the edge if it is not an office/VPN address |
| `bts.poll_nothing_tracked` | no | a BTS polling run attempted parcels and tracked none | Check BTS credentials and status; the dashboard shows `bts_polling_failing` |
| `push.payload_invalid` | no | FCM refused a malformed message (a deploy bug) | Escalate to the developers; devices are never pruned for it |
| `apple.revoke_dead`, `apple.revoke_not_sent` | no | an Apple token revocation could not be done | Revoke in the Apple developer account if required |
| `report.consistency_check_failed` | no (the report's alert lists the check under `errors`) | one check of the nightly report failed (timeout, database error) | Read the traceback in the worker log; rerun `manage.py consistency_report` |
| `report.consistency_time_limit stopped=<checks>` | no (the report's alert lists them under `errors: time_limit`) | the report hit its Celery soft time limit; the listed checks did not run | Rerun `manage.py consistency_report` (no task time limit); if it repeats, find the slow check with `--json` and its plan (§3.1) |
| `alert.send_failed`, `alert.enqueue_failed` (WARNING) | no | the alert webhook is unreachable or refused, or an alert could not be queued | Check the receiver; dead `alert.send` messages are listed by `requeue_outbox --topic alert.send --dead --dry-run` |

### 2.1 Yandex Delivery (`docs/integrations/yandex-delivery.md`)

**Alerts** (webhook; ids and codes only — the bot renders exactly these fields):

| Event | Fields | Scope (dedupe) | Raised when | What to do |
|---|---|---|---|---|
| `delivery.issue_opened` | `order_id`, `kind`, `code`, `urgent`, `issue_id` | the issue id | a delivery issue **becomes urgent** (opened urgent, or an open one escalated): `parcel_unaccounted`, `cancel_incomplete`, `return_pending` `warehouse_closed`, `dispatch_failed` `previous_claim_active` / `courier_search_slow`, `dispatch_unknown` `not_found` (`claims/info` refuses a claim 15 min after our accept; it stays polled) / `dead_letter` (the dispatch died after the accept was sent) / `unexpected_acceptance` (a claim we no longer follow shows accepted), an urgent staff `incident`. (`accept_unconfirmed` is no longer an alert: an accept still not visible 15 min after it was sent — or, once the offer expired, 2 min after it — ends the dispatch, non-urgent `dispatch_failed`.) Queued in the issue's own transaction (never for a rolled-back issue) | Open the order (the bot links it) and the queue «Muammolar» (`GET admin/delivery/issues/`); the issue kinds and their resolution are in the integration doc §11 |
| `delivery.issue_reminder` | the same five | `"<issue id>:<reminder no>"` (each reminder is its own alert) | every open urgent issue nobody touched for 30 minutes (beat `commerce-review-delivery-issues`, every 5 minutes; no cap on the count) | Resolve it or act on it: any change (or the resolve endpoint) stops the reminders |
| `delivery.provider_failing` | `provider` (`yandex`), `code` (`unauthorized`, `forbidden`, a Yandex payment code such as `estimating.cargocorp_payment_failure`, `unavailable`, or Yandex's code) | the Tashkent date | the **third** Yandex account failure in a row: a failed status poll, or a checkout price check refused with 401 / 403 / a payment refusal (also ERROR `yandex.provider_failing {source, code, failures}`) | `unauthorized` / `forbidden`: the token (`YANDEX_DELIVERY_TOKEN`; a cabinet password change invalidates it). A payment code: the prepaid balance or the contract in the Yandex cabinet. `unavailable`: Yandex or the egress to `b2b.taxi.yandex.net`. The dashboard shows `attention.yandex_failing` until a poll or price check is answered |

A dead `yandex.dispatch` outbox message raises the usual `outbox.dead` (alert and audit row) and, through its dead
hook (`commerce.yandex_flow.on_dispatch_dead`):
- **the accept was sent** → urgent `dispatch_unknown` `dead_letter` (→ `delivery.issue_opened`); the claim stays open
  and polled, and `requeue_outbox --topic yandex.dispatch --dead --dry-run` (then without `--dry-run`) resumes the
  dispatch where it stopped — never a second claim, never a second accept;
- **no accept outstanding** (no Yandex id yet, or no / a released `accept_sent_at`) → the claim is closed (settled
  when it has no Yandex id; otherwise left polled until a status or the 24 h settlement settles it, keeping the
  code `dead_letter`), stage `failed`,
  non-urgent `dispatch_failed` `dead_letter`, WARNING `yandex.dispatch_stopped`: staff simply dispatch again (the next
  dispatch cancels the old claim at Yandex). A requeue then does nothing.

`outbox.dead` and K6 still list the message.

**Log keys** (logger `glowderma.*`, key first, then a dict or key=value of ids, codes and amounts — never a name,
phone, address or token):

| Level | Key | Fields | Meaning / action |
|---|---|---|---|
| ERROR | `yandex.auth_failed status=401` | — | Yandex refused the token: check `YANDEX_DELIVERY_TOKEN` |
| ERROR | `yandex.currency_mismatch path=… expected=UZS` | path | a price came in another currency (or none): the answer is refused as unreadable; ask the Yandex manager (open question YD-5) |
| ERROR | `yandex.provider_failing` | `source` (`poll` \| `check_price`), `code`, `failures` | the alert above |
| ERROR | `yandex.previous_claim_active` | `order_id`, `claim` | a new dispatch found the previous claim still alive: the new claim was closed, urgent `dispatch_failed` |
| ERROR | `yandex.foreign_claim` | `order_id`, `claim` | `claims/create` returned a claim whose echoed order reference (`external_order_id` or an item's `extra_id`) names another order: closed, `dispatch_unknown` `foreign_claim`; check the cabinet |
| ERROR | `yandex.carrier_marker_lost` | `order_id`, `claim_number`, `call` (`cancel` \| `return`) | Yandex answered (2xx) an admin cancel's `claims/cancel` / `claims/return`, or its outcome is unknown, but neither the Order row (15 s) nor, without it, the marker write (5 s: the claim row for a cancel, the insert of the `return_requested` event for a return) got its lock to record it: no "sent" marker, as after a crash (integration doc §12, residual window). The admin got 409 `conflict`; a repeated cancel sends the call again — check the claim in the Yandex cabinet first and find what held the rows |
| ERROR | `yandex.poll_apply_failed` (with traceback) | `order_id` | one claim snapshot could not be applied (a bug); the others went on |
| ERROR | `yandex.webhook failed; everything rolled back` (traceback) | — | the callback view crashed: answered 500 (Yandex retries); the poll catches up |
| WARNING | `yandex delivery is live without ALERT_WEBHOOK_URL: urgent delivery issues alert nobody` | — | startup (api, worker, beat): set the alert webhook (§1) |
| WARNING | `yandex.dispatch_stopped` | `order_id`, `claim`, `code` | a dispatch ended without a courier (the issue carries the code; also the dead hook's `dead_letter` close); not logged when a poll or callback saw the claim accepted or ended meanwhile |
| WARNING | `yandex.dispatch_review` | `order_id`, `claim`, `code`, `offer` | the offer went to staff review (`dispatch_review`) |
| WARNING | `yandex.accept_conflict` | `order_id`, `code` | `claims/accept` answered 409 (other than `offer_expired`): `old_version` → the stamp is released and the fresh version accepted on a later run; any other code → one re-read, still before acceptance → `dispatch_failed <code>` |
| WARNING | `yandex.accept_unanswered` | `order_id`, `unknown` | `claims/accept` got no usable answer: `unknown` false ("not processed": 429, 401/403, never sent) → the stamp is released and the accept sent once more on a later run; `unknown` true → never resent, the claim is re-read (still not visible 15 min after it, or 2 min after it once the offer expired → `dispatch_failed` `accept_unconfirmed`) |
| WARNING | `yandex.claim_ref_missing` | `order_id`, `claim` | the `claims/create` answer echoed neither `external_order_id` nor an item's `extra_id`: the claim (returned for our own `request_id`) is followed; if it is constant, ask Yandex whether `external_order_id` is permitted (YD-21) |
| WARNING | `yandex.carrier_record_busy` | `order_id`, `claim_number`, `call` (`cancel` \| `return`) | the Order row stayed locked more than 15 s after Yandex answered (2xx) an admin cancel / return, or its outcome turned unknown: the "sent" marker is then written without the Order lock (no audit row) and the admin gets 409 `conflict`; the next attempt only re-reads — unless ERROR `yandex.carrier_marker_lost` follows with the same fields (then no marker exists and the next attempt may send the call again). Find the long transaction that held the order |
| WARNING | `yandex.unknown_status` | `order_id`, `claim`, `status` | Yandex sent a status outside the documented 26: stored, nothing moved; tell the developers |
| WARNING | `yandex.unknown_claim` | `order_id`, `source` | a snapshot of a claim we do not hold for that order: ignored |
| WARNING | `yandex.accepted_without_us` | `order_id`, `claim` | Yandex shows a claim accepted although we never sent the accept (`auto_accept` on in the cabinet?): it must stay off |
| WARNING | `yandex.poll_failed` | `claims`, `error` | a `claims/bulk_info` run failed (counted on `IntegrationState(yandex)`) |
| WARNING | `yandex.sync_rejected` | `order_id`, `code` | a callback's `claims/info` was refused (unknown claim at Yandex) |
| WARNING | `yandex.webhook_foreign_claim` | `order_id` | a verified callback named a claim of another order: acknowledged, ignored |
| WARNING | `yandex.cost_unknown` | `order_id`, `claim` (the claim's row id) | a finished claim still has no `final_price` 24 h after its end: settled with code `cost_unknown` (check the cabinet's bill); a paid cancel whose quoted `cancel-info` price nobody had counted gets it counted as its fee then (the claim keeps its code) |
| WARNING | `yandex.claim_unanswered` | `order_id`, `claim` (the claim's row id) | a claim a dispatch closed unsettled (never accepted; kept polled in case Yandex took an accept we could not confirm) showed no new status for 24 h after its end: settled at cost 0 — `no_answer` only fills a blank code, and every path that closes a claim unsettled sets one (`accept_unconfirmed`, `not_found`, the 409's code, `dead_letter`, `cancel_requested`), so in practice the closing code stays; the poll stops reading it |
| WARNING | `yandex.unreadable_claim claim_id=… error=…` | claim id | one `bulk_info` entry could not be read: skipped, the others synced |
| WARNING | `yandex.tariffs_check_failed mode=… code=…` | mode, code | «Yandex bilan tekshirish» failed (nothing stored) |
| WARNING | `delivery.quote_unavailable provider=yandex method=… reason=down\|error` | method | a checkout price check failed: the option is `provider_unavailable` |
| INFO | `yandex.dispatch_requested` | `order_id`, `claim_number`, `method` | staff pressed «Kuryer chaqirish» |
| INFO | `yandex.claim_created`, `yandex.accept_sent`, `yandex.claim_accepted` | `order_id`, `claim` | dispatch progress |
| INFO | `yandex.accept_released` | `order_id`, `claim` | Yandex provably did not take the accept ("not processed", 409 `old_version`): `accept_sent_at` released, one accept is sent again on a later run after the offer is re-checked |
| INFO | `yandex.issue` | `order_id`, `kind`, `code`, `urgent` | an issue opened or refreshed by the dispatch / the status writer / the cancel |
| INFO | `yandex.carrier_cancel` | `order_id`, `outcome`, `claim_number`, codes / amounts | each carrier step of an admin cancel (also the audit `order.carrier_cancel`) |
| INFO | `yandex.carrier_reread_failed`, `yandex.cancel_unaccepted_failed`, `yandex.tracking_link_unavailable` | `order_id`, `error` | a best-effort read / cancel / link failed (nothing depends on it) |
| INFO | `yandex.tariffs_checked mode=… classes=…` | mode, classes | «Yandex bilan tekshirish» succeeded |
| INFO | `delivery.quote_refused provider=yandex method=… code=…` | method, Yandex's code | Yandex does not serve the address (`out_of_zone`) |
| INFO | `delivery.review` | `opened`, `resolved`, `reminded` | a review-beat run that changed something |

The live adapter's HTTP lines (`httpclient`) carry the path, the status and the duration only.

## 3. Nightly consistency report

`reporting.consistency_report` runs at **03:40 Tashkent** (beat `reporting-consistency-report`) and on demand:

```bash
scripts/manage.sh <env> django consistency_report          # one line per check
scripts/manage.sh <env> django consistency_report --json   # the whole report
```

Both write the audit row `report.consistency {checks, mismatches}` (actor system); on any non-zero count (or a
failed check) they log ERROR `report.consistency_mismatch` with the counts and sample ids and raise the alert of
the same name (its fields: `counts`, and `errors` naming the checks that failed or ran out of time). Each check
runs in its own savepoint with a 60 s statement timeout, put back to the previous value after the check; one
that fails is reported with `error: failed` and the others still run. The report also runs the loyalty ledger
reconciliation (formerly its own beat at 03:10), last and with a 240 s statement timeout of its own, which writes
its own audit rows. If the task reaches its Celery soft time limit (540 s), no further check starts: the running
check and the remaining ones are reported with `error: time_limit` (ERROR `report.consistency_time_limit
stopped=<checks>`), and the audit row, the ERROR line and the alert are still written.

### 3.1 Checks

Every check is one read-only statement returning a count and the first `CONSISTENCY_SAMPLE_SIZE` (20) ids; the
index named is what bounds it. `outbox_unfinished_idx` is `(available_at, id) WHERE NOT status = 'done'`: the
dispatcher's claim (`ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`) reads it in order and stops at the
first due row (no sort, no done row visited), and the outbox checks filter its few rows (open and dead messages).

| Check | Rows | Threshold (setting, default) | Ids | Plan |
|---|---|---|---|---|
| `k1_billz_sale_unrecorded` | orders paid and not cancelled whose online purchase has no `billz_pushed_at` or no Billz sale id; **only while `BILLZ_MODE=live`** (else `skipped: billz_not_live`) | paid more than `CONSISTENCY_BILLZ_SALE_MINUTES` (120) ago, within `CONSISTENCY_LOOKBACK_DAYS` (7) | order | `order_created_idx` range on the look-back (an order is paid within an hour of its creation), purchase by its unique `order_id` |
| `k1_billz_sale_unrecorded_backlog` | the same rows paid **before** the look-back, however old (the two checks never overlap, so an old unpushed sale never drops out of the report); **only while `BILLZ_MODE=live`** | paid before `CONSISTENCY_LOOKBACK_DAYS` (7) ago | order | `purchase_unpushed_online_idx` (loyalty.0005, partial: the unrecorded online purchases only), then the order by its primary key |
| `k1_billz_sale_dead` | dead `billz.push_sale` messages, however old (retention never purges a dead message) | — | order | `outbox_unfinished_idx` (core.0010: `(available_at, id)` over the rows that are not done), filtered by status and topic |
| `k2_payment_pending_stuck` | `pending` payments holding a provider transaction whose order's reservation ran out | `reservation_expires_at` + `PAYMENT_IN_FLIGHT_GRACE_SECONDS` + `CONSISTENCY_PAYMENT_PENDING_MINUTES` (60) | payment | `payment_status_created_idx` on `status = 'pending'` |
| `k2_click_paid_without_paydoc` | Click payments `paid` without `click_paydoc_id` (their refunds become manual) | confirmed within the look-back | payment | `payment_confirmed_idx` range on `confirmed_at` |
| `k3_refund_open` | refunds `requested`, `submitting`, `unknown` or `manual_required` | requested more than `CONSISTENCY_REFUND_OPEN_HOURS` (24) ago | refund | the partial unique index `refund_one_open_per_payment` (open refunds only) |
| `k3_refund_failed` | `failed` refunds still needing staff (the dashboard rule: not superseded by a later succeeded refund, payment not `refunded`) | — | refund | `refund_attention_idx` on `state = 'failed'` |
| `k4_shipment_missing` | delivery orders `processing` without a BTS parcel (no shipment, or `not_created` / `failed`); a Yandex Delivery order is never counted (it waits for staff to dispatch it; its delays are the `preparation_overdue` issue, §2.1) | paid more than `CONSISTENCY_SHIPMENT_MINUTES` (60) ago | order | `order_status_idx` on `processing`, shipment by unique `order_id` |
| `k4_parcel_untracked` | parcels created at BTS (BTS shipments only) for orders `processing` / `handed_to_courier` without a new BTS status | last status (or creation) older than `CONSISTENCY_PARCEL_UNTRACKED_HOURS` (48) | order | merge join: `order_status_idx` on the two open statuses against the shipments' unique `order_id` index walked in order (every shipment; ~45 ms per 120 000) |
| `k5_late_capture_unrefunded` | refused (late) captures still `paid` with no succeeded refund | captured more than `CONSISTENCY_LATE_CAPTURE_HOURS` (24) ago | payment | `payment_late_capture_open_idx` (payments.0003) |
| `k6_outbox_dead` | outbox messages that went dead | in the last `CONSISTENCY_OUTBOX_DEAD_HOURS` (24) | message | `outbox_unfinished_idx`, filtered by status and `updated_at` |
| `k6_outbox_overdue` | open messages (pending, failed, in flight) due for longer than `CONSISTENCY_OUTBOX_OVERDUE_MINUTES` (60), deferred ones excluded | — | message | `outbox_unfinished_idx` range on `available_at`, filtered by status |
| `k6_outbox_deferred_past_window` | open deferred messages (`RetryLater`) deferred longer than their topic's window + the overdue grace | `billz.push_sale`: `BILLZ_SHIFT_RETRY_MAX_HOURS` (36); `push.order_status`, `push.appointment`, `push.account_security`, `campaign.send_batch`: 6 h (FCM "not sent"); `yandex.dispatch`: 45 min (waiting for the estimate / the acceptance, Yandex's 429); other topics `OUTBOX_DEFERRAL_WINDOW_HOURS` (24) | message | `outbox_unfinished_idx`, filtered by status and `deferred_since` |
| `loyalty_reconciliation` | ledger mismatches (checks C1–C3, C5, C6 of design 02 §1.9.3) and the count of negative balances | — | (see its audit rows) | reads the whole ledger by design; 240 s statement timeout (`LOYALTY_TIMEOUT_MS`) |

### 3.2 What to do

| Check | Action |
|---|---|
| `k1_billz_sale_unrecorded` | Look at the order's `billz.push_sale` message: deferred for a closed shift → it goes through when the shift opens; dead → `billz.sale_dead`; done without a sale → the order was paid while Billz was fake (`billz.sale_not_pushed`): enter the sale in Billz by hand. Then record it with `manage.py billz_mark_recorded --order <id> --actor <staff email> [--billz-sale-id <uuid>]` (docs/integrations/billz.md §7): it sets the purchase's `billz_pushed_at` and Billz sale id, so the order leaves this check (and `_backlog`) at the next report; until then a sale entered by hand stays listed |
| `k1_billz_sale_unrecorded_backlog` | as `k1_billz_sale_unrecorded`; these are older than the look-back, so their push message is usually dead (§2 `billz.sale_dead`) or was done while Billz was fake |
| `k1_billz_sale_dead` | as `billz.sale_dead` (§2); a sale entered in Billz by hand → first `billz_mark_recorded` (above), then close its message with `requeue_outbox --topic billz.push_sale --dead --resolved --order <order id> --dry-run` and, once the listed message is that order's, the same without `--dry-run` (or `--id <message id>`, named in the `billz.sale_dead` line and the `outbox.dead` audit row); `--resolved` refuses a sale not recorded yet |
| `k2_payment_pending_stuck` | Check the transaction in the provider's cabinet. Payme keeps a state-1 transaction up to its 12 h timeout and then cancels it: a Payme row younger than ~13 h usually resolves itself. Money taken → follow the late-capture refund |
| `k2_click_paid_without_paydoc` | The Click Complete arrived without `click_paydoc_id` (see the webhook inbox row `payment:<id>`): a protocol issue for the developers; refunds of such payments are paid out by hand |
| `k3_refund_open` | Payments screen (`refund_state` filter): `manual_required` (Payme) → refund in the Payme cabinet, Payme confirms it; `unknown` → check the payment in the Click cabinet and *Resolve*; `requested` / `submitting` for a day → the outbox is stuck (K6) |
| `k3_refund_failed` | *Retry* the refund, or pay out by hand and *Resolve* it |
| `k4_shipment_missing` | *Retry shipment* on the order (dashboard `bts_failed`); a create request still pending for an hour → K6 |
| `k4_parcel_untracked` | Check the parcel in the BTS cabinet; the dashboard's `bts_polling_failing` and the BTS webhook settings |
| `k5_late_capture_unrefunded` | Follow the refund of that payment (K3 actions) |
| `k6_outbox_dead` | as `outbox.dead` (§2) |
| `k6_outbox_overdue` | The worker or the beat is down or stuck: `scripts/manage.sh <env> ps`, `scripts/manage.sh <env> exec backend-worker celery -A config inspect ping`, worker logs; `health/?ready=1` for the broker |
| `k6_outbox_deferred_past_window` | The handler should have given up; check the worker logs for that topic |
| `loyalty_reconciliation` | as `loyalty.reconciliation_mismatch` (§2) |

## 4. Health and readiness

| Request | Answer | Use |
|---|---|---|
| `GET /api/v1/health/` | always `200 {"status": "ok"}` while the process serves | liveness (the Compose healthcheck) |
| `GET /api/v1/health/?ready=1` | `200`/`503` `{"status", "checks": {"database", "cache", "broker"}, "version"}` | readiness: every check must be `ok` **and** every provider whose mode is `live` must be configured |

Callers that pass the admin network gate (peer in `ADMIN_API_ALLOWED_NETS` and, in production, the internal
secret) also get:

- `integrations`: the mode of every provider (`fake` / `live`);
- `providers`: one entry per **live** provider, `{"configured": bool, "last_sync_at": datetime | null,
  "last_sync_age_seconds": int | null}`. `configured` means the live adapter can be built and its
  `is_configured()` says yes (settings only: Billz key, shop, cash box and payment type; Payme merchant id and
  key; Click numeric ids and secret key; BTS login, password and sender city/branch code; Eskiz email, password
  and sender; FCM project id and key file; Yandex Delivery token, an https base URL, a three-letter currency and
  `ru`/`en`); a live adapter that refuses its settings when built is `false`. `last_sync_at` is the last successful
  Billz catalog sync, BTS polling run or Yandex `claims/bulk_info` poll (null for the other providers).
- `sandbox`: the live providers configured against a sandbox (`bts` test host, `payme` test mode or sandbox
  checkout, `eskiz` test sender 4546); informational, never fails readiness, and must be `[]` in production.

A live provider that is not configured makes `?ready=1` answer **503 for every caller**; outside the gate the body
has no reason (`status: error` with every check `ok`). To see which provider, ask from inside:

```bash
scripts/manage.sh <env> django shell -c \
  "from glowderma.core import readiness; print(readiness.live_configured(), readiness.last_syncs())"
```

Useful thresholds for an external monitor reading `providers` from inside the gate: Billz
`last_sync_age_seconds` above ~30 min (the delta sync runs every `BILLZ_DELTA_SYNC_SECONDS`, 300 s by default;
while Billz is unreachable for longer than `BILLZ_STOCK_STALE_REFUSE_SECONDS` checkouts of lines Billz cannot confirm
answer 503, open question GC-6), BTS above ~30 min while parcels are in transit (polling every 10 min), Yandex above
~5 min while claims are open (polling every 60 s; a run with no open claim writes nothing, so `last_sync_at` ages
harmlessly when nothing is in delivery).

**`IntegrationState(yandex)`** (ops console «Integratsiyalar», or `integrations.selectors.provider_state("yandex")`):
`last_sync_at` / `last_success_at` = the last answered poll; `consecutive_failures` = the current run of failed polls
and account failures of the checkout price check (timeouts of a price check are not counted); `last_error` =
`"poll: <code>"` or `"check_price: <code>"`; `data` = the classes of the last `yandex_delivery_check` /
«Yandex bilan tekshirish» (`tariffs`, `tariffs_checked_at`, `tariffs_mode`, `tariffs_point`). The admin dashboard's
`attention.yandex_failing` is `consecutive_failures > 0` while Yandex is live; the third failure in a row alerts
`delivery.provider_failing` (§2.1).

## 5. Beat schedule (`config/celery.py`, Asia/Tashkent)

| Entry | Task | When |
|---|---|---|
| `dispatch-outbox` | `core.dispatch_outbox` | every 10 s |
| `commerce-expire-reservations` | `commerce.expire_reservations` | every minute |
| `start-due-campaigns` | `notifications.start_due_campaigns` | every minute |
| `reap-stale-jobs` | `integrations.reap_stale_jobs` | every 5 minutes |
| `commerce-poll-bts` | `commerce.poll_bts` | every 10 minutes |
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | hourly |
| `notifications-finalise-stopping` | `notifications.finalise_stopping_campaigns` | every minute |
| `commerce-poll-yandex` | `commerce.poll_yandex` | every 60 s (Yandex Delivery `claims/bulk_info`; single flight; no-op while Yandex is fake under the production settings) |
| `commerce-review-delivery-issues` | `commerce.review_delivery_issues` | every 5 minutes (Yandex Delivery exception queue and urgent-issue reminders, §2.1) |
| `delivery-refresh-bts-regions` | `delivery.refresh_bts_regions` | every 6 hours (M3; BTS's region list, served stale up to 24 h) |
| `billz-sync-delta` | `catalog.billz_sync_delta` | every `BILLZ_DELTA_SYNC_SECONDS` (300 s; gap X-STOCK-FRESHNESS) |
| `purge-idempotency-records` | `core.purge_idempotency_records` | 03:00 |
| `core-purge-outbox` | `core.purge_outbox` | 03:20 (M3) |
| `integrations-purge-webhook-inbox` | `integrations.purge_webhook_inbox` | 03:25 (M3) |
| `delivery-purge-quotes` | `delivery.purge_quotes` | 03:35 (Yandex Delivery: unused checkout quotes a day after they expired) |
| `delivery-blank-courier-details` | `delivery.blank_courier_details` | 03:37 (Yandex Delivery: courier name, vehicle and tracking link 7 days after a claim ended) |
| `reporting-consistency-report` | `reporting.consistency_report` (incl. the loyalty reconciliation) | 03:40 (M3) |
| `billz-sync-full` | `catalog.billz_sync_full` | 04:23 |

The beat is a singleton (never scale it). The retention runs stop starting new batches after 5 minutes; a large
backlog is cleared over several nights.

## 6. Indexes built concurrently (recovering a failed migration)

Three M3 migrations build their index with `CREATE INDEX CONCURRENTLY` (non-atomic migrations, so a rolling deploy
never blocks writes): `core.0010` (`outbox_unfinished_idx`), `payments.0003` (`payment_late_capture_open_idx`) and
`loyalty.0005` (`purchase_unpushed_online_idx`). If such a build fails or is interrupted (a deadlock, a cancelled
`migrate`, the container killed, a lock timeout), PostgreSQL leaves an **INVALID** index behind and Django does not
record the migration; re-running `migrate` then fails with `relation "…" already exists`. Recover:

```bash
# 1. which of the three is INVALID, and that no CREATE INDEX is still running (a second migrate, a killed client
#    whose backend still builds): an index being built is INVALID until it finishes
scripts/manage.sh <env> exec postgres psql -U glowderma -d glowderma -c \
  "SELECT indexrelid::regclass AS index FROM pg_index WHERE NOT indisvalid AND indexrelid::regclass::text IN ('outbox_unfinished_idx', 'purchase_unpushed_online_idx', 'payment_late_capture_open_idx');" \
  -c "SELECT pid, state, query FROM pg_stat_activity WHERE query ILIKE 'CREATE INDEX%' AND pid <> pg_backend_pid();"
# 2. only when step 1 lists it and no build runs: drop it (never inside a transaction block)
scripts/manage.sh <env> exec postgres psql -U glowderma -d glowderma -c \
  "DROP INDEX CONCURRENTLY IF EXISTS outbox_unfinished_idx;"
# 3. re-run the migrations with the NEW release's image: `run` starts a container of the image .env names
#    (deploy/pin-release.sh); `exec api` would run the previous release, which has no such migration
scripts/deploy.sh <env>  # migrations run once per release
```

Check afterwards that the first query of step 1 returns no row. Never drop another INVALID index this way: find out
what builds it first. A development database that applied an earlier draft of
`core.0010` (keyed `(status, available_at)`) gets the current index with `manage.py migrate core 0009` followed by
`manage.py migrate`.
