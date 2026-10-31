# Staff access (operator runbook)

Status: gap-closure staff package, 2026-10-27 (gap FND-4; admin contract §2.16; decisions GST-1..GST-9 in
`docs/decisions/open-questions.md`).

Day to day a **manager** manages staff from the admin panel (Xodimlar screen, admin API `staff/`). The commands
below run the same audited services from the server shell, for when no manager can sign in or the panel is not
at hand. Every command takes `--actor <email>` (the active staff member who asked for the change; default: the
system) and writes an audit entry with reason `manage.py <command>`. In the Compose stack prefix each with
`docker compose exec api python manage.py`.

## First account of an install

```bash
manage.py createstaff admin@glowderma.uz --manager --full-name "Administrator"
```

The first account of an install becomes a manager even without `--manager`; a superuser always is one.

## A staff member leaves

```bash
manage.py deactivatestaff leaver@glowderma.uz --actor manager@glowderma.uz
```

- Every open session is revoked at once (Redis tombstones: the person's access tokens get 401 on the next request
  on every worker) and the person can no longer sign in with the password or a linked Google/Facebook account.
- Linked identities and the manager role are kept; `reactivatestaff <email>` restores access (old sessions stay
  revoked).
- The last active manager cannot be deactivated: make a successor a manager first (below).
- If the person may have kept the password (shared device, written down), also run `resetstaffpassword` before
  any later reactivation.

## Someone cannot sign in / a password leaked

```bash
manage.py resetstaffpassword person@glowderma.uz --actor manager@glowderma.uz
```

- Prints a temporary password **once** (`xxxx-xxxx-xxxx-xxxx`); hand it over in person. The person changes it
  under "Parolni o‘zgartirish" after signing in. `--password-stdin` sets a chosen password instead.
- Revokes every open session (reason `password_changed`) and ends a running login lockout, so the person can
  sign in at once.

## No manager can sign in

```bash
manage.py setstaffmanager successor@glowderma.uz --actor successor@glowderma.uz
manage.py resetstaffpassword successor@glowderma.uz      # if they also lost the password
```

`setstaffmanager <email> --off` removes the role; it is refused for the last active manager and for a superuser.

## Checking what happened

Every change is in the admin audit log (`GET audit-log/?object_type=staff`): `staff.created`, `staff.updated`,
`staff.deactivated`, `staff.reactivated`, `staff.password_reset`, plus `auth.session_revoked` per revoked
session. Passwords are never recorded.

## Ops console access (Unfold, `/django-admin/`, TOTP 2FA)

The console is separate from the admin panel: access is `is_staff`, granted per person from the server shell,
and every sign-in needs the password **and** a TOTP code (or a one-time recovery code).

```bash
manage.py ops_access grant --email person@glowderma.uz --actor manager@glowderma.uz     # access + enrolment window
manage.py ops_access reset-2fa --email person@glowderma.uz --actor manager@glowderma.uz # lost phone
manage.py ops_access revoke --email person@glowderma.uz --actor manager@glowderma.uz    # no more console access
manage.py ops_access status --email person@glowderma.uz
```

- **Enrolment window.** A person without a TOTP device can set one up only within `OPS_ENROL_WINDOW_SECONDS`
  (default 24 h) after `grant`, `reset-2fa` or a manager's "2FA ni qayta o‘rnatish" on the console's staff list;
  the first successful enrolment closes it. Outside the window the login page says to ask a manager. Someone who
  already had access but never enrolled (or whose window expired) gets a new window with `ops_access grant` (it
  writes `ops_access.enrolment_opened`) or `reset-2fa`. People who already have a device are not affected.
- **A 2FA reset ends the person's console sessions** (logged in or half-way through a sign-in) as well as
  deleting the device and recovery codes. A manager's password reset in the admin panel (`staff.password_reset`)
  also ends them.
- **Four eyes.** If the manager who opened the window from the console also reset that person's password in the
  admin panel (before or after), the enrolment is refused (`console.enrol_refused`, reason `same_manager`):
  otherwise one manager could sign in as the person and enrol their own phone. Another manager resets 2FA, or the
  operator runs `ops_access reset-2fa`. A manager can never reset their own 2FA from the console.
- **Passwords** change only in the admin panel ("Parolni o‘zgartirish": lockout, other sessions revoked, audited).
  The console has no password page.
- **Rate limits.** Each step has the per-account lockout (5 failures a minute → 1, 2, 4, 8, then 15 minutes) and a
  per-address budget: `OPS_IP_FAILURE_LIMIT` failures (default 20) per `OPS_IP_FAILURE_WINDOW_SECONDS` (default
  900) for the password step and, separately, for the code steps (an IPv6 client counts by its /64).
- **Audit and alerts.** `ops_access.*`, `console.*` (see `glowderma/accounts/console/events.py`) in the audit log;
  `console.2fa_enrolled` names the window that allowed it (`window_audit_id`, `window_opened_by`, `window_via`).
  `console.2fa_enrolled`, `console.otp_locked` (right password, wrong codes) and `console.ip_locked` also go to the
  alert webhook (`docs/ops/monitoring.md`).
- **Secrets on console pages.** JSON documents (outbox payloads, provider state, jobs, webhook rows, audit
  before/after) show secret-like keys (tokens, signatures, sealed values, `Authorization`, keys and hashes) as
  `«yashirilgan»` (`glowderma/core/jsonmask.py`).

### Accepted risks

- **TOTP seeds are stored in plain text** (`otp_totp_totpdevice.key`). django-otp reads `key` directly to compute
  codes; encrypting it would mean forking the device model. A database dump alone does not give console access
  (the password is Argon2-hashed and the console is reachable only from `OPS_ALLOWED_IPS` on `OPS_DOMAIN`), but
  whoever holds a dump can compute codes for a known password: after a suspected database leak, run
  `ops_access reset-2fa` for every console user. Recovery codes are stored as keyed hashes.
- **The console's CSP allows `'unsafe-eval'`** (`deploy/Caddyfile`, `OPS_DOMAIN` block). Unfold's bundled Alpine.js
  (standard build) evaluates its `x-data` expressions with `new Function`; the CSP build of Alpine would need
  Unfold's templates rewritten. No inline script runs (`script-src 'self'`, no `'unsafe-inline'`), every page is
  same-origin, and template output is auto-escaped, so an injection still needs a script source on the console's
  own origin.
