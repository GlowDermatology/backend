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
