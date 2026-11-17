# Staff access (operator runbook)

Status: gap-closure staff package, 2026-10-27 (gap FND-4; admin contract §2.16; decisions GST-1..GST-9 in
`docs/decisions/open-questions.md`).

Day to day a **manager** manages staff from the admin panel (Xodimlar screen, admin API `staff/`). The commands
below run the same audited services from the server shell, for when no manager can sign in or the panel is not
at hand. Every command takes `--actor <email>` (the active staff member who asked for the change; default: the
system) and writes an audit entry with reason `manage.py <command>`. In the Compose stack prefix each with
`scripts/manage.sh <env> django` (infra repository, on the server).

## First account of an install

```bash
manage.py createstaff admin@glowderma.uz --manager --full-name "Administrator"
```

The first account of an install becomes a manager even without `--manager`; a superuser always is one.

## Inviting staff and forced password changes (feat/admin-ux)

In the panel a manager adds a person (or resets a password) with one of three deliveries:

- **Vaqtinchalik parol** — a generated `xxxx-xxxx-xxxx-xxxx` password shown once; hand it over in person.
- **E-pochta havolasi** — no usable password; the person gets a single-use link
  `{ADMIN_PANEL_URL}/set-password?token=…` by e-mail (valid `STAFF_INVITE_TTL_HOURS`, default 72 h, in the
  person's language). «Havolani qayta yuborish» (`staff/{id}/send-password-link/`) sends a new one and voids
  the old one.
- **Ikkalasi** — both.

Either way the account is marked **must change password**: after signing in (password or Google/Facebook) the
panel only lets the person set their own password (every other admin API call answers 403
`password_change_required`), and the ops console refuses the sign-in until then. A person who signed in with
Google and has no password asks for a link from the change screen (`auth/password-link/`). Setting the
password through a link signs every device out; existing accounts are not forced.

E-mail links need `ADMIN_PANEL_URL` (https in production, e.g. `https://glowderma.uz/admin`) and, under the
production settings, `EMAIL_MODE=smtp` with the `EMAIL_*` values; otherwise the panel shows the e-mail options
disabled. Check a stuck invite in the console's outbox (topic `email.staff_password_link`; the address and
the link are stored sealed and blanked once sent) and on the staff card (`invite.state`, `invite.sent_at`).

Operator commands: `createstaff … --must-change` and `resetstaffpassword … --password-stdin --must-change`
force the change for a typed password; `resetstaffpassword` without `--password-stdin` (a generated password)
always does.

## Who approves push campaigns

Test devices, test pushes, approval, cancel and the emergency stop of a campaign (`notifications/…`, admin
contract §2.8) are **superuser** actions (`StaffUser.is_superuser`); ordinary staff only prepare drafts. The
flag is not editable from the panel or the staff API: a new superuser account is `createstaff <email>
--superuser`, an existing account is promoted from the shell —

```bash
manage.py shell -c "from glowderma.accounts.models import StaffUser; StaffUser.objects.filter(email='owner@glowderma.uz').update(is_superuser=True, is_manager=True)"
```

An install whose demo admin predates the approval workflow has **no** superuser until one is promoted this
way; nothing can be approved before that.

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

- Prints a temporary password **once** (`xxxx-xxxx-xxxx-xxxx`); hand it over in person. The person must set
  their own password right after signing in (the panel allows nothing else; the console refuses the sign-in
  until then). `--password-stdin` sets a chosen password instead (not forced unless `--must-change`).
- Revokes every open session (reason `password_changed`) and ends a running login lockout, so the person can
  sign in at once.

## No manager can sign in

```bash
manage.py setstaffmanager successor@glowderma.uz --actor successor@glowderma.uz
manage.py resetstaffpassword successor@glowderma.uz      # if they also lost the password
```

`setstaffmanager <email> --off` removes the role; it is refused for the last active manager and for a superuser.

## Admin panel MFA (TOTP, feat/admin-ux)

Optional and personal: each staff member turns it on in the admin panel's Settings (next to the password
form) and nobody can require it. It is a **separate authenticator entry** ("Glow Derma Admin") from the ops
console's ("Glow Derma Ops"): staff with console access have two entries by design, and turning one on, off or
resetting it never touches the other (the console's enrolment window and four-eyes rule stay as below).

- **Turning it on**: re-enter the password → scan the QR code (or type the key) → type the code → ten recovery
  codes are shown **once** (`xxxxx-xxxxx`, each works once; only keyed hashes are stored). Turning it off and
  getting new recovery codes need the password plus a code.
- **Signing in**: after a correct password (or Google/Facebook) the panel asks for the 6-digit code or a recovery
  code. The step is valid 5 minutes, allows 5 attempts, works once and dies when the password changes, the
  account is deactivated or its MFA is reset. Five wrong codes a minute lock the account's code step for 1, 2,
  4, 8, then 15 minutes; whatever the pace, ten wrong codes in a row (no correct code between, none more than
  48 hours apart) lock it for 15 minutes, then 1, 4, 16 and 24 hours at each further ten (`auth.mfa_locked`,
  alerted — the ten-in-a-row lock with `failures` as soon as it starts). A correct code clears the count; so
  does a manager's MFA reset.
- **Superusers**: only another superuser can reset a superuser's panel MFA or be handed its temporary password
  (`reset-password/` with `temporary_password` / `both` → 409; `email_link` still works). The operator's
  `resetstaffmfa` / `resetstaffpassword` are the break-glass path.

**Lost phone** (in this order):

1. The person signs in with one of their recovery codes, then in Settings turns MFA off and on again with the
   new phone (or keeps it and asks for new recovery codes).
2. No recovery code: a manager uses **«MFA ni qayta o‘rnatish»** on the staff screen
   (`POST staff/{id}/reset-mfa/`; not for oneself). It removes the panel device and recovery codes, signs the
   person out everywhere and ends a running code lockout; the person signs in with the password and may turn
   MFA on again. Verify who is asking first (in person or a call back on a known number): together with a
   password reset it lets the manager sign in as the person. Both steps are audited and the reset is alerted.
3. No manager can do it (or it is the manager's own account): the operator on the server:

```bash
manage.py resetstaffmfa person@glowderma.uz --actor manager@glowderma.uz
```

`resetstaffmfa` is refused when the account has no panel MFA. The ops console's device is reset separately
(`ops_access reset-2fa`, below).

If a phone was **stolen** rather than lost, also run `resetstaffpassword` (the thief may know the password) and
check `GET audit-log/?object_type=staff&object_id=<id>` for `auth.mfa_succeeded` / `auth.login_succeeded`
entries you cannot explain.

## Checking what happened

Every change is in the admin audit log (`GET audit-log/?object_type=staff`): `staff.created`, `staff.updated`,
`staff.deactivated`, `staff.reactivated`, `staff.password_reset`, `staff.password_link_sent`,
`staff.password_set` (the person used a link), plus `auth.session_revoked` per revoked session and
`auth.password_changed` (`forced` when it ended a pending change). Passwords and links are never recorded.
The admin panel MFA adds `auth.mfa_enabled`, `auth.mfa_disabled`, `auth.mfa_recovery_codes_regenerated`,
`auth.mfa_challenged` (a correct first step), `auth.mfa_succeeded`, `auth.mfa_failed`, `auth.mfa_locked`,
`auth.mfa_recovery_used` (`remaining`) and `auth.mfa_reset` (`via` api or cli); codes and secrets are never
recorded.

## Ops console access (Unfold, `/django-admin/`, TOTP 2FA)

The console is separate from the admin panel: access is `is_staff`, granted per person from the server shell,
and every sign-in needs the password **and** a TOTP code (or a one-time recovery code).

Besides inspection, the dashboards and the professionals forms, the console is the **only** place to edit the
store settings, contacts and legal texts and to publish the terms: «Katalog» → «Sozlamalar»
(`/django-admin/settings/`; since 2026-11-07, the admin panel and its API no longer can). Every save is audited
(`settings.updated`, `settings.terms_published`; texts as digests). Staging and production ship with
`DJANGO_ADMIN_ENABLED=false` (the production default; overrides live in Vault at `secret/<env>/django`): until
ops sets it to `true` with `OPS_DOMAIN`
and `OPS_ALLOWED_IPS`, nobody can change the settings there.

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

### Languages (English, Russian, Uzbek)

The console has a language switcher: three buttons under every sign-in step and the same three entries in the
user menu (bottom of the sidebar) after sign-in. The choice is kept in the browser (cookie `gd_ops_language`,
scoped to `/django-admin/`, one year); a browser that has not chosen opens in `OPS_DEFAULT_LANGUAGE` (`en` unless
the environment sets `ru` or `uz`). Only the console switches by this cookie. The admin API (and so the admin
panel's error messages and CSV/XLSX exports) follows the panel's own language through `Accept-Language`
(`docs/api/admin-api-v1.md` §1.6, §1.8); the audit texts and everything a worker writes or sends stay Uzbek
whatever a browser chose, and records already stored (audit entries, notification texts, customer data) are
shown as they were written.

For developers — Uzbek is the source language (`glowderma/core/console_i18n.py`):

- Write a console text in Uzbek and mark it: `gettext_lazy` / `gettext` / `ngettext` in Python, `{% translate %}`
  / `{% blocktranslate %}` in templates. Model `verbose_name`, `help_text` and `choices` need no marking (they
  are made lazy at start-up); an enum label or a `core.labels` value shown by the console goes through
  `gettext(value)` where it is rendered.
- An admin API text a staff member reads (error messages, export headers, `export_name`) is an Uzbek literal
  marked with `glowderma.core.uzsource.N_` (a no-op, interpolated as `N_("… %(n)s …") % {...}`); the same
  catalogs translate it at the error envelope and the export renderer only (`AGENTS.md`).
- `python manage.py console_messages` rewrites `locale/en` and `locale/ru` (`django.po`): new messages appear
  with an empty `msgstr`, obsolete ones are dropped. Translate the empty ones, then
  `python manage.py compile_locale` and commit the `.po` and `.mo` together.
- `python manage.py console_messages --check` (also a test, `test_console_languages.py`) fails while a message
  has no English or Russian translation, a placeholder differs, or an entry is obsolete; another test opens
  every console page in English and Russian and fails on any text still spelled the Uzbek way.
- `locale/uz` and `locale/ru` also translate the English strings of Unfold and of Django's admin; a string
  added to `locale/uz` must be added to `locale/ru` (Django falls back from Russian to Uzbek).

### Accepted risks

- **TOTP seeds are stored in plain text** (`otp_totp_totpdevice.key`). django-otp reads `key` directly to compute
  codes; encrypting it would mean forking the device model. A database dump alone does not give console access
  (the password is Argon2-hashed and the console is reachable only from `OPS_ALLOWED_IPS` on `OPS_DOMAIN`), but
  whoever holds a dump can compute codes for a known password: after a suspected database leak, run
  `ops_access reset-2fa` for every console user. Recovery codes are stored as keyed hashes. The same holds
  for the admin panel MFA seeds (devices named `Admin panel`): after a suspected leak, `resetstaffmfa` every
  account that has it (the staff list shows `mfa_enabled`) and ask the people to turn it on again.
- **The console's CSP allows `'unsafe-eval'`** (`deploy/Caddyfile`, `OPS_DOMAIN` block). Unfold's bundled Alpine.js
  (standard build) evaluates its `x-data` expressions with `new Function`; the CSP build of Alpine would need
  Unfold's templates rewritten. No inline script runs (`script-src 'self'`, no `'unsafe-inline'`), every page is
  same-origin, and template output is auto-escaped, so an injection still needs a script source on the console's
  own origin.
