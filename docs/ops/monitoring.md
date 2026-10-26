# Monitoring, alerts and the nightly consistency report (operator runbook)

Status: M3 operations package, 2026-10-27. Companion of `docs/ops/compliance.md` (personal data, hosting,
retention) and `docs/ops/deploy.md` (branch `feat/ops-deploy`: the Compose stack, logs, backups).

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
close it: `manage.py requeue_outbox --topic <topic> --dead --resolved --id <message id>` (dry run first; audited
`outbox.resolved`; the row becomes `done` and is purged with the others after `OUTBOX_RETENTION_DAYS`).

## 1. Alert webhook

| Setting | Default | Notes |
|---|---|---|
| `ALERT_WEBHOOK_URL` | empty = disabled | https only under the production settings. Treat it as a secret (it often embeds a token); it is never logged |
| `ALERT_WEBHOOK_SECRET` | empty | required (≥ 32 characters, no placeholder) under the production settings when the URL is set |
| `ALERT_DEDUPE_MINUTES` | 30 | one alert per event and scope per fixed window of this many minutes; the scope is the topic for `outbox.dead`, the order for the per-order Billz events (two orders → two alerts) |
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
dispatcher POSTs it with a 5 s timeout, no redirects: 2xx → done; a transport error, 408, 429 or 5xx → retried with
backoff (dead after 10 attempts, about 17 minutes); any other status → dead at once. A dead `alert.send` raises
no further alert. An alert raised inside a transaction that rolls back is rolled back with it — the ERROR line
stays, which is why log-based alerting is the primary path and the webhook the push channel.

**Compliance.** The receiver must be hosted in Uzbekistan or receive no personal data (`docs/ops/compliance.md`
§1.2). The alert fields are designed to carry none; keep it that way when adding alerts (ids and counts only).

## 2. Events to alert on

*Webhook* = also sent through §1. The others are ERROR (or, where noted, WARNING) lines only — alert on them from
the logs. Ids in the lines are order, payment, refund, purchase, message or account ids; phones appear only as
`pii_ref` hashes.

¹ These five are logged inside the Billz adapter (`glowderma/integrations/billz/{client,live}.py`), which the M3
operations package does not change (the adapter is owned by the Billz work and still moving). A ready change adds
one `alerts.alert(...)` call next to each ERROR line with the fields shown; until it lands they are log-only.

| Event | Webhook | Meaning | What to do |
|---|---|---|---|
| `outbox.dead {topic, message_id, attempts}` | yes (scope: topic) | an outbox message exhausted its retries or was refused for good; audit `outbox.dead` holds `last_error` | Read the audit row (`audit-log/?action=outbox.dead`). `bts.create_shipment` → the shipment is `failed`: *Retry shipment* on the order. `refund.submit` → the refund is `failed` (`dead_letter`): retry it on the Payments screen. `billz.push_sale` → see `billz.sale_dead`. `push.*`, `campaign.send_batch` → FCM: check the FCM project and credentials. `sms.send` → Eskiz: check balance and credentials. `apple.revoke` → revoke by hand in the Apple developer account if needed. Once the cause is fixed, replay with `manage.py requeue_outbox --topic <topic> --dead --dry-run`, then without `--dry-run` |
| `report.consistency_mismatch {counts}` | yes | the nightly report found rows to look at (§3) | `manage.py consistency_report` (or the `report.consistency` audit row) lists the ids; follow §3.2 per check |
| `loyalty.reconciliation_mismatch {mismatches, checks}` | yes | a cashback balance does not add up (never corrected automatically) | The `loyalty.reconciliation_mismatch` audit rows name the accounts and the check. A mismatch means a bug or a raw database write: escalate to the developers; staff correct a balance only through a cashback adjustment with a reason |
| `billz.sale_dead {order_id, purchase_id, message_id}` | yes (scope: order) | a paid online order's Billz sale was refused for good or failed ten times | The reason is in the ERROR line and the `outbox.dead` audit row. Fix the cause (a configuration error, a product missing in Billz, a closed shift that outlived `BILLZ_SHIFT_RETRY_MAX_HOURS`), then replay (`requeue_outbox --topic billz.push_sale --dead`, dry run first: never replay a sale someone entered in Billz by hand meanwhile) — or complete the draft / enter the sale in Billz by hand |
| `billz.configuration_error {case, http_status[, error_code]}` | pending¹ | Billz refused the integration key, a route or a setting (redirect, 403, unknown route, cash box of another shop, seller without access) | Check `BILLZ_SECRET_KEY`, `BILLZ_SHOP_ID`, `BILLZ_CASHBOX_ID`, `BILLZ_PAYMENT_TYPE_ID`, `BILLZ_BASE_URL` and the key's permissions in Billz. Sales keep retrying meanwhile; replay the dead ones after the fix |
| `billz.circuit_open {failures, open_seconds}` | pending¹ | five Billz failures in a row: calls fail fast for 90 s | Usually transient. If it repeats, check Billz's status and the network from the host; sales and syncs resume on their own |
| `billz.cache_unavailable {operation, error}` | pending¹ | Redis (the shared cache) is down: nothing is sent to Billz | Check the redis container (`docker compose … ps`, memory, disk for AOF); `health/?ready=1` shows `cache: error` |
| `billz.sale_state_unclear {order_id}` | pending¹ | a Billz sale reads both paid and open: nothing is paid | Open the sale in Billz (`billz_order_id` in the ERROR line) and make it consistent; the push retries and dies after ten attempts if it stays unclear (then `billz.sale_dead`) |
| `billz.sale_total_differs {order_id}` | pending¹ | Billz bills another amount than the customer paid (a Billz price or client discount): nothing is paid | Fix the draft in Billz (price, discount) and replay, or complete it by hand (decision D1) |
| `billz.refund_by_hand {order_id, purchase_id, pushed}` | yes (scope: order) | an order whose Billz sale exists was cancelled; Billz has no refund API | Refund the sale in Billz (`pushed=True`) or delete its draft (`pushed=False`) by hand |
| `billz.sale_of_cancelled_order {order_id, purchase_id, case}` | yes (scope: order) | the push met a cancelled order: `push_stopped`, `cancelled_during_push` (nothing paid) or `paid_after_cancel` | Delete the draft in Billz; for `paid_after_cancel` refund the sale in Billz by hand |
| `billz.sale_rejected` | no (followed by `billz.sale_dead`) | Billz refused a sale for good | as `billz.sale_dead` |
| `payment.late_capture` | no | money was captured for an order we could not honour; a refund was created | Follow the refund on the Payments screen (K5 checks it after 24 h) |
| `providers.fake_with_open_money` | no | a payment provider switched to fake still holds open money (worker start) | Reconcile in the provider's cabinet or switch it back to live (customer contract §6.14) |
| `otp.budget_tripped` | no | the global SMS budget is spent for this hour: sign-up and reset stop | Check the per-IP counters, block abusive networks at the edge, raise `SMS_GLOBAL_HOURLY_LIMIT` only for real traffic |
| `bts.poll_nothing_tracked` | no | a BTS polling run attempted parcels and tracked none | Check BTS credentials and status; the dashboard shows `bts_polling_failing` |
| `push.payload_invalid` | no | FCM refused a malformed message (a deploy bug) | Escalate to the developers; devices are never pruned for it |
| `apple.revoke_dead`, `apple.revoke_not_sent` | no | an Apple token revocation could not be done | Revoke in the Apple developer account if required |
| `report.consistency_check_failed` | no (the report's alert lists the check under `errors`) | one check of the nightly report failed (timeout, database error) | Read the traceback in the worker log; rerun `manage.py consistency_report` |
| `report.consistency_time_limit stopped=<checks>` | no (the report's alert lists them under `errors: time_limit`) | the report hit its Celery soft time limit; the listed checks did not run | Rerun `manage.py consistency_report` (no task time limit); if it repeats, find the slow check with `--json` and its plan (§3.1) |
| `alert.send_failed`, `alert.enqueue_failed` (WARNING) | no | the alert webhook is unreachable or refused, or an alert could not be queued | Check the receiver; dead `alert.send` messages are listed by `requeue_outbox --topic alert.send --dead --dry-run` |

## 3. Nightly consistency report

`reporting.consistency_report` runs at **03:40 Tashkent** (beat `reporting-consistency-report`) and on demand:

```bash
docker compose -f compose.prod.yaml exec api python manage.py consistency_report          # one line per check
docker compose -f compose.prod.yaml exec api python manage.py consistency_report --json   # the whole report
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
| `k4_shipment_missing` | delivery orders `processing` without a BTS parcel (no shipment, or `not_created` / `failed`) | paid more than `CONSISTENCY_SHIPMENT_MINUTES` (60) ago | order | `order_status_idx` on `processing`, shipment by unique `order_id` |
| `k4_parcel_untracked` | parcels created at BTS for orders `processing` / `handed_to_courier` without a new BTS status | last status (or creation) older than `CONSISTENCY_PARCEL_UNTRACKED_HOURS` (48) | order | `order_status_idx` on the two open statuses, shipment by unique `order_id` |
| `k5_late_capture_unrefunded` | refused (late) captures still `paid` with no succeeded refund | captured more than `CONSISTENCY_LATE_CAPTURE_HOURS` (24) ago | payment | `payment_late_capture_open_idx` (payments.0003) |
| `k6_outbox_dead` | outbox messages that went dead | in the last `CONSISTENCY_OUTBOX_DEAD_HOURS` (24) | message | `outbox_unfinished_idx`, filtered by status and `updated_at` |
| `k6_outbox_overdue` | open messages (pending, failed, in flight) due for longer than `CONSISTENCY_OUTBOX_OVERDUE_MINUTES` (60), deferred ones excluded | — | message | `outbox_unfinished_idx` range on `available_at`, filtered by status |
| `k6_outbox_deferred_past_window` | open deferred messages (`RetryLater`) deferred longer than their topic's window + the overdue grace | `billz.push_sale`: `BILLZ_SHIFT_RETRY_MAX_HOURS` (36); other topics `OUTBOX_DEFERRAL_WINDOW_HOURS` (24) | message | `outbox_unfinished_idx`, filtered by status and `deferred_since` |
| `loyalty_reconciliation` | ledger mismatches (checks C1–C3, C5, C6 of design 02 §1.9.3) and the count of negative balances | — | (see its audit rows) | reads the whole ledger by design; 240 s statement timeout (`LOYALTY_TIMEOUT_MS`) |

### 3.2 What to do

| Check | Action |
|---|---|
| `k1_billz_sale_unrecorded` | Look at the order's `billz.push_sale` message: deferred for a closed shift → it goes through when the shift opens; dead → `billz.sale_dead`; done without a sale → the order was paid while Billz was fake (`billz.sale_not_pushed`): enter the sale in Billz by hand. A sale entered by hand stays listed — after the look-back under `k1_billz_sale_unrecorded_backlog` — because nothing records it on our side yet (proposal: a "recorded by hand" mark) |
| `k1_billz_sale_unrecorded_backlog` | as `k1_billz_sale_unrecorded`; these are older than the look-back, so their push message is usually dead (§2 `billz.sale_dead`) or was done while Billz was fake |
| `k1_billz_sale_dead` | as `billz.sale_dead` (§2); a sale entered in Billz by hand → close its message with `requeue_outbox --topic billz.push_sale --dead --resolved --id <message id>` (the message id is in the `billz.sale_dead` line and the `outbox.dead` audit row) |
| `k2_payment_pending_stuck` | Check the transaction in the provider's cabinet. Payme keeps a state-1 transaction up to its 12 h timeout and then cancels it: a Payme row younger than ~13 h usually resolves itself. Money taken → follow the late-capture refund |
| `k2_click_paid_without_paydoc` | The Click Complete arrived without `click_paydoc_id` (see the webhook inbox row `payment:<id>`): a protocol issue for the developers; refunds of such payments are paid out by hand |
| `k3_refund_open` | Payments screen (`refund_state` filter): `manual_required` (Payme) → refund in the Payme cabinet, Payme confirms it; `unknown` → check the payment in the Click cabinet and *Resolve*; `requested` / `submitting` for a day → the outbox is stuck (K6) |
| `k3_refund_failed` | *Retry* the refund, or pay out by hand and *Resolve* it |
| `k4_shipment_missing` | *Retry shipment* on the order (dashboard `bts_failed`); a create request still pending for an hour → K6 |
| `k4_parcel_untracked` | Check the parcel in the BTS cabinet; the dashboard's `bts_polling_failing` and the BTS webhook settings |
| `k5_late_capture_unrefunded` | Follow the refund of that payment (K3 actions) |
| `k6_outbox_dead` | as `outbox.dead` (§2) |
| `k6_outbox_overdue` | The worker or the beat is down or stuck: `docker compose … ps`, `exec worker celery -A config inspect ping`, worker logs; `health/?ready=1` for the broker |
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
  `is_configured()` (Billz: key, shop, cash box and payment type; Payme: merchant id and key) says yes; a provider
  without a live adapter yet is `false`. `last_sync_at` is the last successful Billz catalog sync or BTS polling
  run (null for the other providers).

A live provider that is not configured makes `?ready=1` answer **503 for every caller**; outside the gate the body
has no reason (`status: error` with every check `ok`). To see which provider, ask from inside:

```bash
docker compose -f compose.prod.yaml exec api python manage.py shell -c \
  "from glowderma.core import readiness; print(readiness.live_configured(), readiness.last_syncs())"
```

Useful thresholds for an external monitor reading `providers` from inside the gate: Billz
`last_sync_age_seconds` above ~2 h (the delta sync runs hourly at :07), BTS above ~30 min while parcels are in
transit (polling every 10 min).

## 5. Beat schedule (`config/celery.py`, Asia/Tashkent)

| Entry | Task | When |
|---|---|---|
| `dispatch-outbox` | `core.dispatch_outbox` | every 10 s |
| `commerce-expire-reservations` | `commerce.expire_reservations` | every minute |
| `start-due-campaigns` | `notifications.start_due_campaigns` | every minute |
| `reap-stale-jobs` | `integrations.reap_stale_jobs` | every 5 minutes |
| `commerce-poll-bts` | `commerce.poll_bts` | every 10 minutes |
| `accounts-cleanup-auth` | `accounts.cleanup_auth` | hourly |
| `billz-sync-delta` | `catalog.billz_sync_delta` | hourly at :07 |
| `purge-idempotency-records` | `core.purge_idempotency_records` | 03:00 |
| `core-purge-outbox` | `core.purge_outbox` | 03:20 (M3) |
| `integrations-purge-webhook-inbox` | `integrations.purge_webhook_inbox` | 03:25 (M3) |
| `reporting-consistency-report` | `reporting.consistency_report` (incl. the loyalty reconciliation) | 03:40 (M3) |
| `billz-sync-full` | `catalog.billz_sync_full` | 04:23 |

The beat is a singleton (never scale it). The retention runs stop starting new batches after 5 minutes; a large
backlog is cleared over several nights.
