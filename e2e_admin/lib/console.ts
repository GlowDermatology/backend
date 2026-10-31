/** Page helpers of the ops console: the sign-in steps, the user menu, changelist bits. */
import { expect, type Page } from "@playwright/test";

import type { StaffMember } from "./fixtures";
import { freshCode } from "./totp";

export const LOGIN = "/django-admin/login/";
export const INDEX = "/django-admin/";

export const TEXT = {
  wrongCode: "Kod noto‘g‘ri yoki allaqachon ishlatilgan",
  wrongRecovery: "Zaxira kod noto‘g‘ri yoki allaqachon ishlatilgan.",
  signinExpired: "Kirish muddati tugadi. Email va parolni qaytadan kiriting.",
  sessionExpired: "Sessiya muddati tugadi. Qaytadan kiring.",
  loggedOut: "Siz konsoldan chiqdingiz.",
  lockout: /Juda ko‘p urinish\. \d+ soniyadan so‘ng qayta urinib ko‘ring\./,
};

export async function passwordStep(page: Page, email: string, password: string): Promise<void> {
  if (!page.url().includes(LOGIN)) await page.goto(LOGIN);
  await page.locator("#id_username").fill(email);
  await page.locator("#id_password").fill(password);
  await page.locator("#login-form button[type=submit]").click();
  await page.waitForLoadState("domcontentloaded");
}

export async function codeStep(page: Page, code: string, form = "#otp-form"): Promise<void> {
  await page.locator(`${form} input[name=code]`).fill(code);
  await page.locator(`${form} button[type=submit]`).click();
  await page.waitForLoadState("domcontentloaded");
}

/** The whole sign-in through the forms: password, then a fresh TOTP code of the user's key. */
export async function signInThroughForms(page: Page, user: StaffMember, next?: string): Promise<void> {
  await page.goto(next ? `${LOGIN}?next=${encodeURIComponent(next)}` : LOGIN);
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/django-admin\/2fa\/$/);
  await codeStep(page, await freshCode(user.key_hex!));
}

export async function openUserMenu(page: Page): Promise<void> {
  const logout = page.locator("#logout-form button[type=submit]");
  if (!(await logout.isVisible())) {
    await page.locator("[x-data*=openUserLinks] > div.cursor-pointer").click();
  }
  await expect(logout).toBeVisible();
}

export async function logOut(page: Page): Promise<void> {
  await openUserMenu(page);
  await page.locator("#logout-form button[type=submit]").click();
  await expect(page).toHaveURL(new RegExp(`${LOGIN}$`));
}

export async function sessionCookie(page: Page): Promise<string> {
  const cookie = (await page.context().cookies()).find((entry) => entry.name === "sessionid");
  if (!cookie) throw new Error("no session cookie");
  return cookie.value;
}

/** Assert the page is the console's login page (the redirect target of every refused console view). */
export async function expectLoginPage(page: Page): Promise<void> {
  await expect(page).toHaveURL(/\/django-admin\/login\/(\?.*)?$/);
  await expect(page.locator("#login-form")).toBeVisible();
}

/** No horizontal page overflow at the current viewport. */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const sizes = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    inner: window.innerWidth,
  }));
  expect(sizes.scroll, `page wider than the viewport (${sizes.scroll} > ${sizes.inner})`).toBeLessThanOrEqual(
    sizes.inner,
  );
}
