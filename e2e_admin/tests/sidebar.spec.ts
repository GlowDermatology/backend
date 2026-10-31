/**
 * The console's navigation: every sidebar link loads, the live badges show the database's numbers, the
 * sidebar search box opens the palette, and the command palette (⌘K / Ctrl+K) finds a model. The badges'
 * absence at zero runs on the empty gate server (gate.spec.ts); a badge that follows a new dead message is
 * in mutating/badges.spec.ts.
 */
import { django } from "../lib/django";
import { expect, SIDEBAR_BADGES, sidebarItem, test } from "../lib/fixtures";

test("every sidebar link loads", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  const hrefs = await page
    .locator("a[href^='/django-admin/']:has(span.material-symbols-outlined)")
    .evaluateAll((anchors) => [...new Set(anchors.map((anchor) => anchor.getAttribute("href") ?? ""))]);
  // 4 dashboards + the domain groups of config/settings/ops_admin.py
  expect(hrefs.length).toBeGreaterThanOrEqual(50);
  for (const href of hrefs.filter((entry) => !entry.includes("/logout/"))) {
    await test.step(href, async () => {
      const response = await page.goto(href);
      expect(response?.status(), href).toBe(200);
      await expect(page.locator("#main, main").first()).toBeVisible();
    });
  }
});

test("the «all applications» list links every registered model", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  const links = page.locator("a.block.truncate[href^='/django-admin/']");
  expect(await links.count()).toBeGreaterThanOrEqual(50);
});

test("badges show the database's numbers", async ({ page, staff }) => {
  void staff;
  await django("clear-cache");
  const counts = await django<Record<string, number>>("badge-counts");
  expect(counts.dead_outbox, "the demo data has dead outbox messages").toBeGreaterThan(0);
  expect(counts.failed_webhooks, "the demo data has failed callbacks").toBeGreaterThan(0);
  await page.goto("/django-admin/");
  for (const [href, key] of Object.entries(SIDEBAR_BADGES)) {
    const badge = sidebarItem(page, href).locator(".sidebar-badge");
    if (counts[key] > 0) await expect(badge, key).toHaveText(String(counts[key]));
    else await expect(badge, key).toHaveCount(0);
  }
});

test("the sidebar search box opens the palette and finds a model in Uzbek", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  await page.getByText("Bo‘lim va modellarni qidirish...").click();
  const input = page.locator("#search-input-command");
  await expect(input).toBeVisible();
  await input.pressSequentially("Buyurtma");
  await expect(page.locator('#command-results a[href="/django-admin/commerce/order/"]')).toBeVisible();
  await input.press("Escape");
});

test("the command palette (Ctrl+K) finds a model and opens it", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/");
  await page.keyboard.press("Control+k");
  const input = page.locator("#search-input-command");
  await expect(input).toBeVisible();
  await input.pressSequentially("Outbox");
  const result = page.locator('#command-results a[href="/django-admin/core/outboxmessage/"]');
  await expect(result).toBeVisible();
  await result.click();
  await expect(page).toHaveURL(/\/django-admin\/core\/outboxmessage\/$/);
});
