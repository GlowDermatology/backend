/**
 * The console's three languages (glowderma/core/console_i18n.py). The suite's server opens the console in
 * Uzbek (`OPS_DEFAULT_LANGUAGE = "uz"`, config/settings/e2e.py); here a browser switches it: on the sign-in
 * page before any session exists, and from the user menu afterwards. The choice lives in a cookie of the
 * console, so it survives the sign-in, a reload and a new page, and it never reaches the REST API.
 */
import type { Page } from "@playwright/test";

import { openUserMenu } from "../lib/console";
import { expect, MAIN_URL, sidebarItem, test } from "../lib/fixtures";

const COOKIE = "gd_ops_language";
const LANGUAGES = [
  { code: "en", name: "English", signIn: "Log in", dashboard: "Ops status", orders: "Orders" },
  { code: "ru", name: "Русский", signIn: "Войти", dashboard: "Состояние системы", orders: "Заказы" },
  { code: "uz", name: "O‘zbekcha", signIn: "Kirish", dashboard: "Ops holati", orders: "Buyurtmalar" },
] as const;

const ordersLink = (page: Page) => sidebarItem(page, "/django-admin/commerce/order/");

test("the sign-in page switches between the three languages", async ({ page }) => {
  await page.goto("/django-admin/login/");
  await expect(page.locator("html")).toHaveAttribute("lang", "uz");
  const switcher = page.locator("nav.ops-language");
  await expect(switcher.getByRole("button")).toHaveText(LANGUAGES.map((language) => language.name));
  for (const language of LANGUAGES) {
    await test.step(language.code, async () => {
      await switcher.getByRole("button", { name: language.name }).click();
      await expect(page).toHaveURL(/\/django-admin\/login\/$/);
      await expect(page.locator("html")).toHaveAttribute("lang", language.code);
      await expect(page.locator("button[type=submit]", { hasText: language.signIn })).toBeVisible();
      await expect(switcher.locator("[aria-current=true]")).toHaveText(language.name);
    });
  }
  const cookie = (await page.context().cookies(MAIN_URL)).find((entry) => entry.name === COOKIE);
  expect(cookie?.value).toBe("uz");
  expect(cookie?.path).toBe("/django-admin/");
  expect(cookie?.httpOnly).toBe(true);
});

test("the user menu switches the console and the choice stays", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  await expect(page.locator("html")).toHaveAttribute("lang", "uz");
  for (const language of LANGUAGES) {
    await test.step(language.code, async () => {
      await openUserMenu(page);
      await page.locator(`form:has(input[name=language][value=${language.code}]) button`).click();
      // back on the page the menu was opened from
      await expect(page).toHaveURL(/\/django-admin\/$/);
      await expect(page.locator("html")).toHaveAttribute("lang", language.code);
      await expect(page).toHaveTitle(new RegExp(language.dashboard));
      await expect(ordersLink(page)).toContainText(language.orders);
      // another page and a reload keep the language
      await page.goto("/django-admin/commerce/order/");
      await expect(page.locator("html")).toHaveAttribute("lang", language.code);
      await expect(ordersLink(page)).toContainText(language.orders);
      await page.reload();
      await expect(page.locator("html")).toHaveAttribute("lang", language.code);
      await page.goto("/django-admin/");
    });
  }
});

test("a console language never reaches the API", async ({ page, context }) => {
  await context.addCookies([{ name: COOKIE, value: "ru", url: `${MAIN_URL}/django-admin/` }]);
  await page.goto("/django-admin/login/");
  await expect(page.locator("html")).toHaveAttribute("lang", "ru");
  const health = await context.request.get("/api/v1/health/");
  expect(health.headers()["content-language"]).toBeUndefined();
});
