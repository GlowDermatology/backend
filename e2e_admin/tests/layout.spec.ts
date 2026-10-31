/**
 * Theme and layout: the dark mode switch persists across pages and reloads, the dashboards and a list / change
 * page fit a 390 px phone and a 1440 px desktop without horizontal page scrolling, and the environment badge
 * says LOCAL.
 */
import { expectNoHorizontalOverflow, openUserMenu } from "../lib/console";
import { expect, test } from "../lib/fixtures";

// placeholder for the change page of the first order on the list (looked up in the test)
const ORDER_CHANGE_PAGE = "an order's change page";

const PAGES = [
  "/django-admin/",
  "/django-admin/dashboards/money/",
  "/django-admin/dashboards/sales/",
  "/django-admin/dashboards/customers/",
  "/django-admin/commerce/order/",
  "/django-admin/core/outboxmessage/",
  "/django-admin/accounts/customer/",
  ORDER_CHANGE_PAGE,
];

function themeSwitch(page: import("@playwright/test").Page, label: string) {
  return page.locator("[x-data*=openUserLinks] nav").getByText(label, { exact: true });
}

async function isDark(page: import("@playwright/test").Page): Promise<boolean> {
  return page.evaluate(() => document.documentElement.classList.contains("dark"));
}

test("dark mode: the switch applies at once and persists across pages and reloads", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  expect(await isDark(page)).toBe(false);
  await openUserMenu(page);
  await themeSwitch(page, "Qorong‘i").click();
  await expect.poll(() => isDark(page)).toBe(true);
  await page.reload();
  expect(await isDark(page)).toBe(true);
  await page.goto("/django-admin/core/outboxmessage/");
  expect(await isDark(page)).toBe(true);
  await page.goto("/django-admin/login/");
  expect(await isDark(page)).toBe(true);
  await page.goto("/django-admin/");
  await openUserMenu(page);
  await themeSwitch(page, "Yorug‘").click();
  await expect.poll(() => isDark(page)).toBe(false);
  await page.reload();
  expect(await isDark(page)).toBe(false);
});

test("dark mode follows the system setting in «Tizim» mode", async ({ page, staff }) => {
  void staff;
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/django-admin/");
  await openUserMenu(page);
  await themeSwitch(page, "Tizim").click();
  await expect.poll(() => isDark(page)).toBe(true);
  await page.emulateMedia({ colorScheme: "light" });
  await page.reload();
  expect(await isDark(page)).toBe(false);
});

for (const width of [390, 1440]) {
  for (const path of PAGES) {
    test(`${width} px: ${path} has no horizontal page overflow`, async ({ page, staff }) => {
      void staff;
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
      let target = path;
      if (path === ORDER_CHANGE_PAGE) {
        await page.goto("/django-admin/commerce/order/");
        target = (await page.locator("#result_list tbody tr a[href*='/change/']").first().getAttribute("href"))!;
      }
      const response = await page.goto(target);
      expect(response?.status()).toBe(200);
      await expectNoHorizontalOverflow(page);
    });
  }
}

test("390 px: the sidebar opens from the header and closes again", async ({ page, staff }) => {
  void staff;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/django-admin/");
  const orders = page.locator('a[href="/django-admin/commerce/order/"]:has(span.material-symbols-outlined)').first();
  await expect(orders).toBeHidden();
  await page.locator("span.material-symbols-outlined", { hasText: "dock_to_right" }).click();
  await expect(orders).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.locator("span.material-symbols-outlined", { hasText: /^\s*close\s*$/ }).first().click();
  await expect(orders).toBeHidden();
});

test("the environment badge says LOCAL and the tab title is prefixed", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  await expect(page.getByText("LOCAL", { exact: true }).first()).toBeVisible();
  expect(await page.title()).toMatch(/^\[LOCAL\]/);
  await page.goto("/django-admin/login/");
  expect(await page.title()).toMatch(/^\[LOCAL\]/);
});
