# FCM — live adapter (M3)

> **Not verified against a live sandbox.** No Firebase project credentials exist yet. The adapter follows the
> FCM HTTP v1 and Google service-account references and the customer contract (§2.14.2); its tests replay the
> documented answers through `httpx.MockTransport` and sign real RS256 assertions with a throwaway key.

Code: `glowderma/integrations/fcm/live.py` (`LiveFcm`, the service-account loader, the process token cache),
`glowderma/integrations/fcm/protocol.py` (message document, answer classification). Selected by
`fcm.registry.get_fcm_port()` when `FCM_MODE=live`; the fake is unchanged. No firebase SDK: httpx + PyJWT only.

## Sources (read 2026-10-26)

- firebase.google.com/docs/reference/fcm/rest/v1/projects.messages — message fields (`token`, `notification`,
  `data`, `android`, `apns`); Android priority "can take `normal` and `high` values"; data-only payloads to iOS
  allow only `apns-priority: 5`.
- firebase.google.com/docs/cloud-messaging/send/v1-api — `POST …/v1/projects/{id}/messages:send`.
- firebase.google.com/docs/reference/fcm/rest/v1/ErrorCode — `INVALID_ARGUMENT` (400, with a
  `google.rpc.BadRequest` naming the field), `UNREGISTERED` (404), `SENDER_ID_MISMATCH` (403),
  `QUOTA_EXCEEDED` (429, back off at least one minute), `UNAVAILABLE` (503), `INTERNAL` (500),
  `THIRD_PARTY_AUTH_ERROR` (401, APNs credentials).
- developers.google.com/identity/protocols/oauth2/service-account — JWT-bearer grant (`aud`
  `https://oauth2.googleapis.com/token`, `exp` at most one hour after `iat`).

## Protocol

1. **Access token.** A JWT signed RS256 with the service account's `private_key` (header `kid` =
   `private_key_id`): `iss` = `client_email`, `scope` = `https://www.googleapis.com/auth/firebase.messaging`,
   `aud` = `https://oauth2.googleapis.com/token`, `iat`, `exp` = `iat + 3600`; posted as
   `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=<jwt>` to the token endpoint (always the
   Google URI, never the file's `token_uri`). The access token is cached **in the worker process** until five
   minutes before `expires_in`, minted single-flight under a lock, and minted again once when FCM answers 401 for
   it. It never reaches Redis, the database or a log.
2. **Send.** `POST https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send` with
   `{"message": …}` — one request per message, at most eight in flight per batch, each bounded (3 s connect,
   10 s total) and the whole batch by 120 s (`BATCH_SECONDS`, far below the 300 s outbox lease): what is not
   sent by then is `not_sent`. The document is the contract's: `notification` (title, body, `image` for campaigns; omitted for
   data-only), string `data`, `android.priority` / `android.notification.channel_id` / `collapse_key`,
   `apns.headers` (`apns-priority`, `apns-push-type`, `apns-collapse-id`) and `apns.payload.aps`
   (`sound` + `mutable-content`, or `content-available` for a data-only background push).

## Answers → results

| FCM answer | `PushResult` / effect |
|---|---|
| 200 `{"name": "projects/…/messages/<id>"}` | `ok`, `message_id = <id>` |
| `UNREGISTERED` (404), `SENDER_ID_MISMATCH` (403), `INVALID_ARGUMENT` naming the token (field `message.token` or "registration token" in the message) | `error = "unregistered"` → the caller prunes the device |
| any other `INVALID_ARGUMENT` / 400 / unexpected 4xx | `error = "payload_invalid"` → message failed, ERROR `push.payload_invalid` (caller), device untouched |
| `THIRD_PARTY_AUTH_ERROR` (the project's APNs credentials) | `error = "third_party_auth_error"`, ERROR `fcm.third_party_auth_error`, device untouched |
| `QUOTA_EXCEEDED` (429: that device's or topic's rate) | `error = "not_sent"` for that message; the batch goes on |
| `UNAVAILABLE`, `INTERNAL`, 429, 5xx; a request that never left | `error = "not_sent"`; the batch stops (the rest `not_sent`) |
| 401 without an FCM code (our access token) | token re-minted once; still 401 → `not_sent`, the batch stops, ERROR `fcm.config_error` |
| 403 / 404 without a token code (wrong project, missing permission), unreadable service account, token endpoint refusing the grant | `not_sent`, the batch stops, ERROR `fcm.config_error` / `fcm.credentials_invalid` / `fcm.auth_failed` |
| token endpoint unreachable, 5xx or 429 | `not_sent`, the batch stops, WARNING `fcm.token_unavailable` (FCM unavailable, not a configuration error) |
| no answer after the request may have left (timeout, undecodable body), anything unexpected while sending | `error = "unavailable"` (unknown outcome: failed, **never re-sent blindly**); the batch stops |
| a batch of ≥ 5 messages that **all** answer `SENDER_ID_MISMATCH` | a configuration error (wrong `FCM_PROJECT_ID` / service account): ERROR `fcm.sender_id_mismatch`, nothing pruned, every message `not_sent` |

**Batch rule.** A call whose every message is `not_sent` raises `FcmUnavailable`: the callers give every claim back
(`push_state` queued again) and the outbox retries. Otherwise the results come back one per message: the callers
settle `ok` / `unregistered` / `payload_invalid` / `unavailable` / `third_party_auth_error` and give the `not_sent`
ones back — a campaign batch keeps its cursor and is delivered again after one minute (`outbox.RetryLater`, no
attempt spent, dead after six hours), a status push likewise. A device token is dropped **only** on a dead-token
answer — never on a transient, quota, configuration or payload error.

Logs carry counts and FCM's error code; never a device token, an access token, a title/body or the key file.

## Environment

| Variable | Required when | Example | Secret |
|---|---|---|---|
| `FCM_MODE` | — | `live` | no |
| `FCM_PROJECT_ID` | `FCM_MODE=live` (a Firebase project id: lower case, 6–30 chars) | `glow-derma` | no |
| `FCM_CREDENTIALS_FILE` | `FCM_MODE=live`: a readable service-account JSON key with an RSA `private_key` (checked at boot; never echoed) | `/run/secrets/fcm.json` | **yes** (the file) |

The service account needs the "Firebase Cloud Messaging API Admin" role (or `cloudmessaging.messages.create`) on
the project, and the FCM API (HTTP v1) must be enabled in Google Cloud.

## Verify M3

1. Error answers as documented (especially the `INVALID_ARGUMENT` detail naming the token and a 404 for an
   unregistered token) — record one of each from the real project.
2. APNs: `apns-collapse-id` for collapse keys, background pushes (`content-available`, priority 5) reach an
   iOS app in the background; campaign images show on iOS (may need `fcm_options.image` and the notification
   service extension).
3. Throughput: eight requests in flight per batch (500 recipients ≈ a few seconds); FCM's per-project quota.
4. `QUOTA_EXCEEDED` asks for a back-off of at least one minute: a message given back as `not_sent` is delivered
   again after one minute (`notifications.services.NOT_SENT_RETRY`); a whole batch FCM refused (`FcmUnavailable`)
   still follows the outbox backoff (2, 4, 8 … s, dead after ten attempts).
5. Data residency (compliance C1): only ids, the order number and texts without personal data are sent.

## Sandbox checklist (once credentials exist)

- [ ] Create the service account key, mount it read-only at `FCM_CREDENTIALS_FILE`, set `FCM_PROJECT_ID`,
      `FCM_MODE=live` on staging; the boot check passes.
- [ ] A test device registers through `PUT me/device/`; an order status push arrives with its `inbox_id`.
- [ ] Uninstall the app, send again → `unregistered`, the device row is pruned.
- [ ] A malformed message (temporarily) → `payload_invalid`, device untouched, ERROR logged.
- [ ] Revoke the key → ERROR `fcm.auth_failed`, outbox retries; restore → delivery resumes.
- [ ] A campaign to a small segment: one result per recipient, counters right.
- [ ] `session_replaced` (data-only) reaches a backgrounded iOS and Android app.
