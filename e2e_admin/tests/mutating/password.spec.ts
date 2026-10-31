/**
 * «Parolni o‘zgartirish» in the console's user menu (the admin site's password change page). A staff
 * password changed here must keep the invariants of the panel's own change-password endpoint
 * (accounts.api.admin ChangePasswordView): every open panel session of the staff member is revoked
 * (reason password_changed) and the change is audited (auth.password_changed); the console session that
 * made the change stays signed in, and the new password signs in.
 */
import { openUserMenu, passwordStep } from "../../lib/console";
import { django } from "../../lib/django";
import { expect, test } from "../../lib/fixtures";

test("changing the password in the console revokes the panel sessions and is audited", async ({ page, newStaff, signIn }) => {
  const user = await newStaff();
  await signIn(page.context(), user);
  const panel = await django<{ id: string }>("panel-session", { email: user.email });
  const since = (await django<{ id: number }>("audit-max")).id;

  await page.goto("/django-admin/");
  await openUserMenu(page);
  await page.getByRole("link", { name: "Parolni o‘zgartirish" }).click();
  await expect(page).toHaveURL(/\/django-admin\/password_change\/$/);
  const fresh = "Yangi-Parol-2026-Kuchli!";
  await page.locator("#id_old_password").fill(user.password);
  await page.locator("#id_new_password1").fill(fresh);
  await page.locator("#id_new_password2").fill(fresh);
  await page.locator("form button[type=submit], form input[type=submit]").filter({ hasText: /parol|o‘zgartir/i }).first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(/\/django-admin\/password_change\/done\/$/);

  const [session] = await django<{ revoked_at: string | null; revoke_reason: string }[]>("auth-sessions", { ids: [panel.id] });
  expect(session.revoked_at, "the panel session survived the password change").not.toBeNull();
  expect(session.revoke_reason).toBe("password_changed");
  const rows = await django<{ action: string; entity_id: string }[]>("audit", { after_id: since, action: "auth.password_changed" });
  expect(rows.map((row) => row.entity_id)).toContain(String(user.id));

  // still signed in here
  await page.goto("/django-admin/core/outboxmessage/");
  await expect(page).toHaveURL(/outboxmessage\/$/);
  // the new password passes the password step
  const other = await page.context().browser()!.newContext({ baseURL: page.url().split("/django-admin")[0] });
  const otherPage = await other.newPage();
  await passwordStep(otherPage, user.email, fresh);
  await expect(otherPage).toHaveURL(/\/2fa\/$/);
  await other.close();
});

test("a wrong current password is refused on the form", async ({ page, newStaff, signIn }) => {
  const user = await newStaff();
  await signIn(page.context(), user);
  await page.goto("/django-admin/password_change/");
  await page.locator("#id_old_password").fill("not-my-password");
  await page.locator("#id_new_password1").fill("Yangi-Parol-2026-Kuchli!");
  await page.locator("#id_new_password2").fill("Yangi-Parol-2026-Kuchli!");
  await page.locator("form button[type=submit], form input[type=submit]").filter({ hasText: /parol|o‘zgartir/i }).first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(/\/password_change\/$/);
  await expect(page.locator(".errorlist, [class*=text-red-600]").first()).toBeVisible();
});
