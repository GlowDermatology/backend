# Ops console — real-browser suite (Playwright)

Every feature of the Glow Derma ops console (`/django-admin/`, django-unfold) exercised in Chromium against
real Django servers: sign-in with TOTP 2FA and its lockouts and session limits, the network gate, every
registered model (list, search, each filter, sorting, pagination, change page, read-only guarantees, secret
columns), the editable professionals forms, every audited action, the four dashboards and their drill-downs,
the sidebar and its badges, theme and layouts, accessibility (axe) and the Uzbek UI.

## Run

```sh
npm --prefix e2e_admin ci          # once (@playwright/test and axe-core, pinned)
npm --prefix e2e_admin test        # the whole suite; report: npm --prefix e2e_admin run report
```

Needs `uv sync` done in the repository (the servers and `manage.py e2e_console` run with `.venv/bin/python`,
or `E2E_PYTHON`), PostgreSQL on `localhost:5433` and Redis on `localhost:6380` (`E2E_PG_URL`,
`E2E_REDIS_URL` to change them) and a Chromium for Playwright (`npx playwright install chromium`, or a
preinstalled one through `PLAYWRIGHT_BROWSERS_PATH`).

Playwright starts two servers with `scripts/serve.sh` (settings `config.settings.e2e`: DEBUG off, the
production static storage, `/media/` like the edge):

- **main** on `127.0.0.1:8770`: drops and recreates the database `glowderma_e2e`, migrates, runs
  `seed_demo`, `seed_ops_demo` and `e2e_console seed-coverage` (a row in every registered model), Redis
  databases 9/10;
- **gate** on `127.0.0.1:8771`: a copy of the freshly migrated database without demo data
  (`glowderma_e2e_empty`), `OPS_ADMIN_ALLOWED_NETS=10.0.0.0/8` behind the trusted proxy `127.0.0.1`:
  the network-gate and empty-database tests. Redis databases 11/12.

Only `glowderma_e2e*` databases are ever dropped. Locally a server already listening on those ports is
reused; `E2E_FRESH=1` (or `CI`) always starts fresh ones. Ports: `E2E_PORT`, `E2E_GATE_PORT`.

## How the tests work

- `lib/fixtures.ts`: every test fails on a `console.error`, an uncaught page error, a failed request or an
  HTTP ≥ 400 answer of the console's own origin, unless the test declares it (`expected.status(404, /…/)`).
  `staff` signs the page in as a manager of this worker; `newStaff()` makes a fresh staff member.
- `lib/django.ts` calls `manage.py e2e_console <action> --data <json>` for data setup and database
  checks (audit rows, sessions, lockouts, badge counts, model facts). The command refuses to run outside
  `config.settings.e2e` (`E2E_CONSOLE_ENABLED`).
- `lib/totp.ts` computes RFC 6238 codes from the staff member's secret (or the key read off the enrolment
  page), never reusing a time step.
- Projects: `console` (read-mostly specs, in parallel), then `mutating` (actions, editable forms, badge
  changes) one test at a time, so the dashboards' counts stay stable while compared.
- `tests/models.spec.ts` reads the model list from `admin.site._registry` at collection time: a newly
  registered model is covered without editing the suite.
