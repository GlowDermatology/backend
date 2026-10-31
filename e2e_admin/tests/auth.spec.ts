/**
 * The console sign-in, one feature per test: password step, TOTP enrolment, recovery codes, TOTP and
 * recovery sign-in, the two lockouts, pending sign-in expiry and cancel, the verified-session rule, non-staff
 * refusal, POST-only logout, and the idle / absolute session limits.
 */
import { django } from "../lib/django";
import {
  codeStep,
  expectLoginPage,
  INDEX,
  LOGIN,
  logOut,
  passwordStep,
  sessionCookie,
  signInThroughForms,
  TEXT,
} from "../lib/console";
import { addSession, expect, test } from "../lib/fixtures";
import { base32ToHex, freshCode, hotp, currentStep } from "../lib/totp";

type AuditRow = {
  id: number;
  action: string;
  entity_id: string;
  reason: string;
  after: Record<string, unknown> | null;
};

const auditMax = async () => (await django<{ id: number }>("audit-max")).id;
const audit = (after_id: number, action: string, entity_id?: number) =>
  django<AuditRow[]>("audit", { after_id, action, ...(entity_id ? { entity_id } : {}) });

/** A six-digit code that is not valid for `keyHex` in the current ±1 window. */
function wrongCode(keyHex: string): string {
  const valid = new Set([-1, 0, 1].map((offset) => hotp(keyHex, currentStep() + offset)));
  for (let n = 0; ; n++) {
    const candidate = String((123456 + n * 7919) % 1_000_000).padStart(6, "0");
    if (!valid.has(candidate)) return candidate;
  }
}

test("password step of a staff member with a device leads to the code step, not into the console", async ({
  page,
  newStaff,
}) => {
  const user = await newStaff();
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/django-admin\/2fa\/$/);
  await expect(page.locator("#otp-form input[name=code]")).toBeVisible();
  await expect(page.locator(".ops-signin-account")).toHaveText(user.email);
  const state = await django<{ pending: boolean; user_id: string | null }>("session-state", {
    session_key: await sessionCookie(page),
  });
  expect(state).toMatchObject({ pending: true, user_id: null });
});

test("wrong password is refused on the login page and audited", async ({ page, newStaff }) => {
  const user = await newStaff();
  const since = await auditMax();
  await passwordStep(page, user.email, "not-the-password");
  await expectLoginPage(page);
  await expect(page.locator("#login-form")).toContainText(/to‘g‘ri|noto‘g‘ri|email|parol/i);
  const rows = await audit(since, "console.login_failed");
  expect(rows.some((row) => row.after?.email === user.email)).toBe(true);
});

test("first sign-in enrols TOTP: QR and manual key, wrong code refused, right code accepted, recovery codes shown once", async ({
  page,
  newStaff,
}) => {
  const user = await newStaff({ totp: false });
  const since = await auditMax();
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/django-admin\/2fa\/enrol\/$/);

  await expect(page.locator(".ops-qr-frame svg.ops-qr")).toBeVisible();
  await page.locator(".ops-manual-key summary").click();
  const manual = (await page.locator("#manual-key").innerText()).trim();
  expect(manual).toMatch(/^([A-Z2-7]{4} ?)+$/);
  const keyHex = base32ToHex(manual);

  await codeStep(page, wrongCode(keyHex), "#enrol-form");
  await expect(page).toHaveURL(/\/2fa\/enrol\/$/);
  await expect(page.getByText(TEXT.wrongCode)).toBeVisible();
  // The same pending secret is shown again (one enrolment at a time).
  await page.locator(".ops-manual-key summary").click();
  await expect(page.locator("#manual-key")).toHaveText(manual);
  expect(await django("two-factor", { email: user.email })).toEqual({ confirmed: 0, unconfirmed: 1, recovery_codes: 0 });

  await codeStep(page, await freshCode(keyHex), "#enrol-form");
  await expect(page.getByText("Ikki bosqichli himoya yoqildi.")).toBeVisible();
  const codes = await page.locator("#recovery-codes code").allInnerTexts();
  expect(codes).toHaveLength(10);
  for (const code of codes) expect(code).toMatch(/^[a-z2-7]{5}-[a-z2-7]{5}$/);
  expect(new Set(codes).size).toBe(10);
  expect(await django("two-factor", { email: user.email })).toEqual({ confirmed: 1, unconfirmed: 0, recovery_codes: 10 });
  const actions = (await audit(since, "console.", user.id)).map((row) => row.action);
  expect(actions).toEqual(expect.arrayContaining(["console.password_verified", "console.otp_failed", "console.2fa_enrolled"]));
  expect(actions).toContain("console.login_succeeded");

  await page.getByRole("link", { name: /Kodlarni saqladim/ }).click();
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  await expect(page.locator("h1, h2").filter({ hasText: "Ops holati" }).first()).toBeVisible();

  // Shown once: the enrolment step is gone, and no page shows the codes again.
  await page.goto("/django-admin/2fa/enrol/");
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  const html = await page.content();
  for (const code of codes) expect(html).not.toContain(code);
});

test("sign out, then sign back in with a TOTP code", async ({ page, newStaff }) => {
  const user = await newStaff();
  await signInThroughForms(page, user);
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  await logOut(page);
  await expect(page.getByText(TEXT.loggedOut)).toBeVisible();
  await page.goto(INDEX);
  await expectLoginPage(page);
  await signInThroughForms(page, user);
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  await expect(page.locator("#logout-form")).toHaveCount(1);
});

test("the same TOTP code is never accepted twice", async ({ page, newStaff }) => {
  const user = await newStaff();
  const code = await freshCode(user.key_hex!);
  await passwordStep(page, user.email, user.password);
  await codeStep(page, code);
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  await logOut(page);
  await passwordStep(page, user.email, user.password);
  await codeStep(page, code);
  await expect(page).toHaveURL(/\/2fa\/$/);
  await expect(page.getByText(TEXT.wrongCode)).toBeVisible();
});

test("sign-in returns to the page that asked for it (next)", async ({ page, newStaff }) => {
  const user = await newStaff();
  await page.goto("/django-admin/core/outboxmessage/");
  await expect(page).toHaveURL(/\/django-admin\/login\/\?next=\/django-admin\/core\/outboxmessage\/$/);
  await passwordStep(page, user.email, user.password);
  await codeStep(page, await freshCode(user.key_hex!));
  await expect(page).toHaveURL(/\/django-admin\/core\/outboxmessage\/$/);
});

test("a recovery code signs in once; the second use is refused", async ({ page, newStaff }) => {
  const user = await newStaff({ recovery: true });
  const codes = user.recovery_codes!;
  const [code] = codes;
  const since = await auditMax();
  await passwordStep(page, user.email, user.password);
  await page.getByRole("link", { name: /Zaxira kod bilan kirish/ }).click();
  await expect(page).toHaveURL(/\/2fa\/recovery\/$/);
  await codeStep(page, code, "#recovery-form");
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
  const used = await audit(since, "console.recovery_code_used", user.id);
  expect(used).toHaveLength(1);
  expect(used[0].after?.remaining).toBe(9);
  expect((await django<{ recovery_codes: number }>("two-factor", { email: user.email })).recovery_codes).toBe(9);

  await logOut(page);
  await passwordStep(page, user.email, user.password);
  await page.getByRole("link", { name: /Zaxira kod bilan kirish/ }).click();
  await codeStep(page, code, "#recovery-form");
  await expect(page).toHaveURL(/\/2fa\/recovery\/$/);
  await expect(page.getByText(TEXT.wrongRecovery)).toBeVisible();
  // A code typed in capitals with spaces is the same code (normalised): the next unused one works.
  await codeStep(page, ` ${codes[1].toUpperCase()} `, "#recovery-form");
  await expect(page).toHaveURL(new RegExp(`${INDEX}$`));
});

test("five wrong passwords lock the account out of the password step", async ({ page, newStaff, expected }) => {
  expected.status(429, /\/django-admin\/login\/$/);
  const user = await newStaff();
  const since = await auditMax();
  for (let attempt = 1; attempt <= 5; attempt++) {
    await passwordStep(page, user.email, `wrong-password-${attempt}`);
    await expectLoginPage(page);
    await expect(page.getByText(TEXT.lockout)).toHaveCount(0);
  }
  expect(await django("lockout", { scope: "django_admin_login", email: user.email })).toMatchObject({ locked: true });
  // Even the right password is refused while locked.
  const answer = page.waitForResponse((response) => response.url().endsWith(LOGIN) && response.request().method() === "POST");
  await passwordStep(page, user.email, user.password);
  expect((await answer).status()).toBe(429);
  await expect(page.getByText(TEXT.lockout)).toBeVisible();
  await expectLoginPage(page);
  const failed = (await audit(since, "console.login_")).filter((row) => row.after?.email === user.email);
  expect(failed.filter((row) => row.action === "console.login_failed")).toHaveLength(5);
  expect(failed.filter((row) => row.action === "console.login_locked")).toHaveLength(1);
});

test("five wrong codes lock the code step, even for the right code", async ({ page, newStaff, expected }) => {
  expected.status(429, /\/django-admin\/2fa\/$/);
  const user = await newStaff();
  const since = await auditMax();
  await passwordStep(page, user.email, user.password);
  for (let attempt = 1; attempt <= 5; attempt++) {
    await codeStep(page, wrongCode(user.key_hex!));
    await expect(page.getByText(TEXT.wrongCode)).toBeVisible();
  }
  expect(await django("lockout", { scope: "django_admin_otp", email: user.email })).toMatchObject({ locked: true });
  const answer = page.waitForResponse((response) => response.url().endsWith("/2fa/") && response.request().method() === "POST");
  await codeStep(page, await freshCode(user.key_hex!));
  expect((await answer).status()).toBe(429);
  await expect(page.getByText(TEXT.lockout)).toBeVisible();
  await expect(page).toHaveURL(/\/2fa\/$/);
  const rows = await audit(since, "console.otp_", user.id);
  expect(rows.filter((row) => row.action === "console.otp_failed")).toHaveLength(5);
  expect(rows.filter((row) => row.action === "console.otp_locked")).toHaveLength(1);
  expect((await django<{ user_id: string | null }>("session-state", { session_key: await sessionCookie(page) })).user_id).toBeNull();
});

test("a pending sign-in expires after five minutes", async ({ page, newStaff }) => {
  const user = await newStaff();
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/2fa\/$/);
  await django("age-pending", { session_key: await sessionCookie(page), seconds: 301 });
  await codeStep(page, await freshCode(user.key_hex!));
  await expectLoginPage(page);
  await expect(page.getByText(TEXT.signinExpired)).toBeVisible();
  const state = await django<{ pending: boolean; user_id: string | null }>("session-state", {
    session_key: await sessionCookie(page),
  });
  expect(state.pending).toBe(false);
  expect(state.user_id).toBeNull();
});

test("«Boshqa hisob bilan kirish» cancels the pending sign-in", async ({ page, newStaff }) => {
  const user = await newStaff();
  await passwordStep(page, user.email, user.password);
  await page.getByRole("button", { name: "Boshqa hisob bilan kirish" }).click();
  await expectLoginPage(page);
  expect((await django<{ pending: boolean }>("session-state", { session_key: await sessionCookie(page) })).pending).toBe(false);
  await page.goto("/django-admin/2fa/");
  await expectLoginPage(page);
  await expect(page.getByText(TEXT.signinExpired)).toHaveCount(0);
});

test("/2fa/cancel/ answers only POST", async ({ page, expected }) => {
  expected.status(405, /\/2fa\/cancel\/$/);
  const response = await page.goto("/django-admin/2fa/cancel/");
  expect(response?.status()).toBe(405);
});

const GUARDED_PAGES = [
  INDEX,
  "/django-admin/dashboards/money/",
  "/django-admin/dashboards/sales/",
  "/django-admin/dashboards/customers/",
  "/django-admin/core/outboxmessage/",
  "/django-admin/accounts/staffuser/",
  "/django-admin/professionals/professional/add/",
  "/django-admin/core/auditentry/1/change/",
  "/django-admin/password_change/",
];

test("a sign-in waiting for its code reaches no console page", async ({ page, newStaff }) => {
  const user = await newStaff();
  await passwordStep(page, user.email, user.password);
  for (const path of GUARDED_PAGES) {
    await page.goto(path);
    await expectLoginPage(page);
  }
});

test("a password-only session (no second factor) reaches no console page", async ({ page, newStaff }) => {
  const user = await newStaff();
  const { session_key } = await django<{ session_key: string }>("session", { email: user.email, verified: false });
  await addSession(page.context(), session_key);
  for (const path of GUARDED_PAGES) {
    await page.goto(path);
    await expectLoginPage(page);
  }
  // …and its login page does not wave it through either.
  await page.goto(LOGIN);
  await expect(page.locator("#login-form")).toBeVisible();
});

const REFUSED = "Xodim hisobi uchun to‘g‘ri email va parolni kiriting.";

test("a user without console access (is_staff off) is refused at the password step", async ({ page, newStaff }) => {
  const user = await newStaff({ staff: false });
  await passwordStep(page, user.email, user.password);
  await expectLoginPage(page);
  await expect(page.getByText(REFUSED)).toBeVisible();
  // No sign-in was started: no session at all, so the code step sends the browser back.
  expect((await page.context().cookies()).some((cookie) => cookie.name === "sessionid")).toBe(false);
  await page.goto("/django-admin/2fa/");
  await expectLoginPage(page);
});

test("an inactive staff member is refused at the password step", async ({ page, newStaff }) => {
  const user = await newStaff({ active: false });
  await passwordStep(page, user.email, user.password);
  await expectLoginPage(page);
  await expect(page.getByText(REFUSED)).toBeVisible();
  await page.goto("/django-admin/2fa/");
  await expectLoginPage(page);
});

test("logout answers only POST, and the POST ends the session", async ({ page, newStaff, signIn, expected }) => {
  expected.status(405, /\/django-admin\/logout\/$/);
  const user = await newStaff();
  const key = await signIn(page.context(), user);
  const response = await page.goto("/django-admin/logout/");
  expect(response?.status()).toBe(405);
  expect((await django<{ exists: boolean }>("session-state", { session_key: key })).exists).toBe(true);
  const since = await auditMax();
  await page.goto(INDEX);
  await logOut(page);
  expect((await django<{ exists: boolean; user_id?: string }>("session-state", { session_key: key })).exists).toBe(false);
  expect(await audit(since, "console.logout", user.id)).toHaveLength(1);
  await page.goto("/django-admin/core/outboxmessage/");
  await expectLoginPage(page);
});

for (const limit of ["idle", "absolute"] as const) {
  test(`a console session ends after its ${limit} limit`, async ({ page, newStaff, signIn }) => {
    const user = await newStaff();
    const key = await signIn(page.context(), user);
    await page.goto("/django-admin/core/outboxmessage/");
    await expect(page).toHaveURL(/outboxmessage\/$/);
    const since = await auditMax();
    await django(
      "age-session",
      limit === "idle" ? { session_key: key, idle_seconds: 1801 } : { session_key: key, absolute_seconds: 12 * 3600 + 1, idle_seconds: 0 },
    );
    await page.goto("/django-admin/core/outboxmessage/?status__exact=dead");
    await expect(page).toHaveURL(/\/django-admin\/login\/\?next=%2Fdjango-admin%2Fcore%2Foutboxmessage%2F%3Fstatus__exact%3Ddead$/);
    await expect(page.getByText(TEXT.sessionExpired)).toBeVisible();
    expect((await django<{ exists: boolean }>("session-state", { session_key: key })).exists).toBe(false);
    const rows = await audit(since, "console.session_expired", user.id);
    expect(rows).toHaveLength(1);
    expect(rows[0].reason).toBe(limit);
  });
}

test("a session just inside the idle limit stays signed in", async ({ page, newStaff, signIn }) => {
  const user = await newStaff();
  const key = await signIn(page.context(), user);
  await django("age-session", { session_key: key, idle_seconds: 1700 });
  await page.goto("/django-admin/core/outboxmessage/");
  await expect(page).toHaveURL(/outboxmessage\/$/);
});
