# Release note — hybrid identity (B-W2)

Customer accounts can be created by e-mail or a social identity and get their phone at the first checkout,
payment, booking or QR (customer contract §2.1, §2.2.10). What operators must know before deploying it.

## Before the upgrade
- **Old app builds and `phone: null`.** `Me.phone` (and the admin `CustomerListItem` / `CustomerCard` /
  `CustomerRef.phone`) is now nullable. A phoneless account exists only once someone signs up by e-mail or social
  without a phone, which today only the web storefront offers; but if an installed app build cannot decode a null
  `phone` and its customers might sign in to such an account, raise `MIN_APP_VERSION_IOS` /
  `MIN_APP_VERSION_ANDROID` to the first build that handles it (426 `app_update_required`) before the storefront
  goes live.
- **E-mail.** Set `EMAIL_MODE=smtp` with `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
  `EMAIL_USE_TLS` or `EMAIL_USE_SSL` and `EMAIL_FROM` (the production settings refuse an incomplete or unencrypted
  relay). Left at `fake`, nothing is delivered: e-mail sign-up, reset and verification cannot complete (startup
  WARNING, `email: fake` in `health/?ready=1`). Optional: `EMAIL_GLOBAL_HOURLY_LIMIT` (500),
  `EMAIL_OTP_IP_HOURLY_LIMIT` (20), `CUSTOMER_REAUTH_FRESH_SECONDS` (600). See `deploy/env.production.example`.
- **Take the pre-upgrade database dump** (`deploy/backup.sh`) and keep it until the release is confirmed.

## Migration `accounts.0010_hybrid_identity`
- Forward: `Customer.phone` nullable, `Customer.email` + `email_verified_at`, `SmsCode.email` (with a database
  default `''`, so workers of the previous release still insert SMS codes during the rolling deploy), new
  constraints and an index. No data is rewritten; it is quick on the current table sizes.
- **Reverse is refused once it would lose data** — as soon as one customer has no phone or has an e-mail, or an
  e-mail / attach code exists (`IrreversibleError`). After the storefront has been used, **roll back by restoring
  the pre-upgrade dump**, not by `migrate accounts 0009`.

## After the upgrade
- Watch `otp.email_budget_tripped` (ERROR + alert) and the `email.send` / `email.notice` outbox rows
  (`requeue_outbox`); a dead `email.send` means codes are not arriving.
- Security notices: attaching a phone or an e-mail sends `push.account_security` (`phone_attached` /
  `email_attached`; older app builds show the server-rendered text as is) and, for a replaced e-mail, a notice to
  the previous address.
