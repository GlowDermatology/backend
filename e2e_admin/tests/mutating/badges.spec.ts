/**
 * A sidebar badge follows the database: a new dead outbox message raises the «Outbox» badge once the
 * one-minute badge cache is gone (until then the cached number stays, by design).
 */
import { django } from "../../lib/django";
import { expect, sidebarItem, test } from "../../lib/fixtures";

const OUTBOX = "/django-admin/core/outboxmessage/";

test("the dead-outbox badge counts a new dead message after the badge cache expires", async ({ page, staff }) => {
  void staff;
  await django("clear-cache");
  const before = (await django<{ dead_outbox: number }>("badge-counts")).dead_outbox;
  await page.goto("/django-admin/");
  const badge = sidebarItem(page, OUTBOX).locator(".sidebar-badge");
  if (before > 0) await expect(badge).toHaveText(String(before));
  else await expect(badge).toHaveCount(0);

  await django("dead-outbox");
  await page.reload();
  // cached for a minute: still the old number
  if (before > 0) await expect(badge).toHaveText(String(before));
  else await expect(badge).toHaveCount(0);

  await django("clear-cache");
  await page.reload();
  await expect(badge).toHaveText(String(before + 1));
  expect((await django<{ dead_outbox: number }>("badge-counts")).dead_outbox).toBe(before + 1);
});
