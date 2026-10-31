/**
 * The `gate` server: OPS_ADMIN_ALLOWED_NETS=10.0.0.0/8 behind the trusted proxy 127.0.0.1, the console bound
 * to the host 127.0.0.1 (OPS_DOMAIN) and a per-address failure budget of 5 (OPS_IP_FAILURE_LIMIT), on a freshly
 * migrated database without demo data.
 *
 * - The network gate: a browser outside the allowed networks gets an empty 404 for every console path (login,
 *   2FA steps, pages, static is not gated), whatever X-Forwarded-For it sends itself when no trusted proxy
 *   forwarded it; inside the allowed networks (as the trusted edge reports it) the console works.
 * - OPS_DOMAIN: another host name of the same server gets an empty 404 for the console, even from an allowed
 *   address; the API still answers there.
 * - The per-address budget: one address (as the edge forwards it) with too many failed passwords gets 429 and
 *   a `console.ip_locked` audit row, even for the right password; another address is not affected.
 * - An empty database: every dashboard renders, and the sidebar badges are absent at zero.
 */
import { passwordStep, TEXT } from "../lib/console";
import { django } from "../lib/django";
import { addSession, expect, GATE_URL, SIDEBAR_BADGES, sidebarItem, test } from "../lib/fixtures";

/** The same gate server under another host name (OPS_DOMAIN is 127.0.0.1 there). */
const OTHER_HOST_URL = GATE_URL.replace("127.0.0.1", "localhost");
const randomByte = () => 1 + Math.floor(Math.random() * 250);

const CONSOLE_PATHS = [
  "/django-admin/",
  "/django-admin/login/",
  "/django-admin/2fa/",
  "/django-admin/2fa/enrol/",
  "/django-admin/2fa/recovery/",
  "/django-admin/logout/",
  "/django-admin/core/outboxmessage/",
  "/django-admin/dashboards/money/",
  "/django-admin/jsi18n/",
  "/django-admin",
];

test.describe("outside OPS_ADMIN_ALLOWED_NETS", () => {
  test("every console path answers an empty 404", async ({ page, expected }) => {
    expected.status(404, /\/django-admin/);
    for (const path of CONSOLE_PATHS) {
      const response = await page.goto(`${GATE_URL}${path}`);
      expect(response?.status(), path).toBe(404);
      expect(await response?.text(), path).toBe("");
    }
  });

  test("a POST to the login form is refused the same way (no lockout, no audit)", async ({ page, newStaff }) => {
    const user = await newStaff({}, "gate");
    const before = (await django<{ id: number }>("audit-max", {}, "gate")).id;
    const response = await page.request.post(`${GATE_URL}/django-admin/login/`, {
      form: { username: user.email, password: user.password },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(404);
    expect(await response.text()).toBe("");
    expect((await django<{ id: number }>("audit-max", {}, "gate")).id).toBe(before);
  });

  test("a valid session cookie does not open the console either", async ({ page, newStaff, expected }) => {
    expected.status(404, /\/django-admin/);
    const user = await newStaff({}, "gate");
    const { session_key } = await django<{ session_key: string }>("session", { email: user.email }, "gate");
    await addSession(page.context(), session_key, "gate");
    const response = await page.goto(`${GATE_URL}/django-admin/`);
    expect(response?.status()).toBe(404);
  });

  test("an X-Forwarded-For sent by the browser itself is ignored without a trusted proxy hop", async ({ playwright }) => {
    // The gate server trusts 127.0.0.1 as its proxy, so a header from it counts; this checks the parsing of a
    // spoofed chain: only the hop the trusted proxy appended (the last one) decides.
    const context = await playwright.request.newContext({ extraHTTPHeaders: { "X-Forwarded-For": "10.0.0.5, 8.8.8.8" } });
    const spoofed = await context.get(`${GATE_URL}/django-admin/login/`, { maxRedirects: 0 });
    expect(spoofed.status()).toBe(404);
    await context.dispose();
  });

  test("static files and the public API are not gated", async ({ page }) => {
    const css = await page.request.get(`${GATE_URL}/static/ops/admin.css`);
    expect(css.status()).toBe(200);
    const health = await page.request.get(`${GATE_URL}/api/v1/health/`);
    expect(health.status()).toBe(200);
  });
});

test.describe("inside OPS_ADMIN_ALLOWED_NETS (forwarded by the trusted edge)", () => {
  test.use({ extraHTTPHeaders: { "X-Forwarded-For": "10.20.30.40" } });

  test("the login page loads", async ({ page }) => {
    const response = await page.goto(`${GATE_URL}/django-admin/login/`);
    expect(response?.status()).toBe(200);
    await expect(page.locator("#login-form")).toBeVisible();
  });

  test("an empty database: every dashboard renders", async ({ page, newStaff }) => {
    const user = await newStaff({ manager: true }, "gate");
    const { session_key } = await django<{ session_key: string }>("session", { email: user.email }, "gate");
    await addSession(page.context(), session_key, "gate");
    for (const path of ["/django-admin/", "/django-admin/dashboards/money/", "/django-admin/dashboards/sales/", "/django-admin/dashboards/customers/"]) {
      for (const query of ["", "?refresh=1"]) {
        const response = await page.goto(`${GATE_URL}${path}${query}`);
        expect(response?.status(), `${path}${query}`).toBe(200);
        await expect(page.locator(".gd-dash")).toBeVisible();
      }
    }
  });

  test("an empty database: the sidebar badges are absent at zero", async ({ page, newStaff }) => {
    const user = await newStaff({ manager: true }, "gate");
    const counts = await django<Record<string, number>>("badge-counts", {}, "gate");
    expect(counts).toEqual({ dead_outbox: 0, failed_webhooks: 0, refunds_needing_action: 0 });
    const { session_key } = await django<{ session_key: string }>("session", { email: user.email }, "gate");
    await addSession(page.context(), session_key, "gate");
    await django("clear-cache", {}, "gate");
    await page.goto(`${GATE_URL}/django-admin/`);
    for (const href of Object.keys(SIDEBAR_BADGES)) {
      const item = sidebarItem(page, href);
      await expect(item).toBeVisible();
      await expect(item.locator(".sidebar-badge")).toHaveCount(0);
    }
  });
});

test.describe("OPS_DOMAIN: the console answers only on its own host", () => {
  test.use({ extraHTTPHeaders: { "X-Forwarded-For": "10.20.30.41" } });

  test("another host name gets an empty 404 for every console path, from an allowed address", async ({ page, expected }) => {
    expected.console(/status of 404 .*@ http:\/\/localhost:8771\/django-admin\//);
    for (const path of ["/django-admin/", "/django-admin/login/", "/django-admin/2fa/", "/django-admin/core/outboxmessage/"]) {
      const other = await page.goto(`${OTHER_HOST_URL}${path}`);
      expect(other?.status(), `${OTHER_HOST_URL}${path}`).toBe(404);
      expect(await other?.text(), path).toBe("");
      const own = await page.goto(`${GATE_URL}${path}`);
      expect(own?.status(), `${GATE_URL}${path}`).toBe(200);
    }
    // the rest of the server (the API's health check) still answers on that host
    const health = await page.goto(`${OTHER_HOST_URL}/api/v1/health/`);
    expect(health?.status()).toBe(200);
  });
});

test("one address over its failure budget gets 429 and is audited; another address is not affected", async ({ browser, newStaff }) => {
  const user = await newStaff({}, "gate");
  const address = `10.${randomByte()}.${randomByte()}.${randomByte()}`;
  const since = (await django<{ id: number }>("audit-max", {}, "gate")).id;
  const context = await browser.newContext({ baseURL: GATE_URL, extraHTTPHeaders: { "X-Forwarded-For": address } });
  const page = await context.newPage();
  // five failures, each on another account: no account reaches its own lockout ladder
  for (let attempt = 1; attempt <= 5; attempt++) {
    await passwordStep(page, `nobody-${attempt}-${address}@e2e.uz`, "wrong-password");
    await expect(page.locator("#login-form")).toBeVisible();
    await expect(page.getByText(TEXT.lockout)).toHaveCount(0);
  }
  const answer = page.waitForResponse((response) => response.url().endsWith("/django-admin/login/") && response.request().method() === "POST");
  await passwordStep(page, user.email, user.password); // even the right password, from this address
  const refused = await answer;
  expect(refused.status()).toBe(429);
  expect(Number(refused.headers()["retry-after"])).toBeGreaterThan(0);
  await expect(page.getByText(TEXT.lockout)).toBeVisible();
  await expect(page).toHaveURL(/\/django-admin\/login\/$/);
  const rows = await django<{ action: string; ip: string | null; after: Record<string, unknown> | null }[]>(
    "audit",
    { after_id: since, action: "console.ip_locked" },
    "gate",
  );
  expect(rows).toHaveLength(1);
  expect(rows[0]).toMatchObject({ ip: address, after: { step: "password" } });
  await context.close();

  // another address signs in with the same password at once
  const other = await browser.newContext({
    baseURL: GATE_URL,
    extraHTTPHeaders: { "X-Forwarded-For": `10.${randomByte()}.${randomByte()}.${randomByte()}` },
  });
  const otherPage = await other.newPage();
  await passwordStep(otherPage, user.email, user.password);
  await expect(otherPage).toHaveURL(/\/django-admin\/2fa\/$/);
  await other.close();
});
