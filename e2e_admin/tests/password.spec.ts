/**
 * No password change in the console (glowderma.core.admin_site): staff change their password in the Next
 * admin panel only (lockout, session revoke, audit). The admin site's stock password_change/ and
 * password_change/done/ pages answer 404 to a verified session — a GET and a POST alike, and the password
 * stays as it was — and the user menu links to the user's own staff record («Mening hisobim») instead of a
 * «Parolni o‘zgartirish» link.
 */
import { openUserMenu, passwordStep } from "../lib/console";
import { expect, MAIN_URL, test } from "../lib/fixtures";

const CHANGE = "/django-admin/password_change/";
const DONE = "/django-admin/password_change/done/";

test("the console's password change pages answer 404 to a verified session", async ({ page, newStaff, signIn, expected }) => {
  expected.status(404, /\/django-admin\/password_change\//);
  const user = await newStaff();
  await signIn(page.context(), user);
  for (const path of [CHANGE, DONE]) {
    const response = await page.goto(path);
    expect(response?.status(), path).toBe(404);
    await expect(page.locator("#id_old_password")).toHaveCount(0);
  }

  // A POST with a valid CSRF token is refused too, and the password does not change.
  await page.goto("/django-admin/");
  const token = await page.locator("#logout-form input[name=csrfmiddlewaretoken]").inputValue();
  const fresh = "Yangi-Parol-2026-Kuchli!";
  const answer = await page.request.post(CHANGE, {
    form: { csrfmiddlewaretoken: token, old_password: user.password, new_password1: fresh, new_password2: fresh },
    headers: { Referer: `${MAIN_URL}${CHANGE}` },
  });
  expect(answer.status()).toBe(404);
  const other = await page.context().browser()!.newContext({ baseURL: MAIN_URL });
  const otherPage = await other.newPage();
  await passwordStep(otherPage, user.email, user.password);
  await expect(otherPage, "the old password still passes the password step").toHaveURL(/\/2fa\/$/);
  await other.close();
});

test("the user menu has no change-password link, only the user's own record", async ({ page, newStaff, signIn }) => {
  const user = await newStaff();
  await signIn(page.context(), user);
  await page.goto("/django-admin/");
  await openUserMenu(page);
  await expect(page.locator('a[href*="password_change"]')).toHaveCount(0);
  await expect(page.getByText(/Parolni o‘zgartirish|Change password/)).toHaveCount(0);
  const own = page.getByRole("link", { name: "Mening hisobim" });
  await expect(own).toHaveAttribute("href", `/django-admin/accounts/staffuser/${user.id}/change/`);
  await own.click();
  await expect(page).toHaveURL(new RegExp(`/accounts/staffuser/${user.id}/change/$`));
  await expect(page.locator("#content, main").first()).toContainText(user.email);
});
