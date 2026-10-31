/**
 * A sidebar badge follows the database: a new dead outbox message raises the «Outbox» badge once the
 * one-minute badge cache is gone (until then the cached number stays, by design); a requeue made in the
 * console clears that cache itself, so the badge drops at once.
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

test("a requeue from the console updates the dead-outbox badge at once (the badge cache is cleared)", async ({ page, staff }) => {
  void staff;
  const { id } = await django<{ id: number }>("dead-outbox");
  await django("clear-cache");
  const before = (await django<{ dead_outbox: number }>("badge-counts")).dead_outbox;
  expect(before).toBeGreaterThanOrEqual(1);
  await page.goto(`/django-admin/core/outboxmessage/${id}/change/`);
  const badge = sidebarItem(page, OUTBOX).locator(".sidebar-badge");
  await expect(badge).toHaveText(String(before)); // now cached for a minute

  await page.getByRole("link", { name: "Qayta navbatga qo‘yish" }).first().click();
  await page.locator("#dialog button[name=_ops_confirm]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page.getByText(`Xabar #${id} qayta navbatga qo‘yildi.`)).toBeVisible();
  // no clear-cache here: the action itself forgot the cached badge
  if (before > 1) await expect(badge).toHaveText(String(before - 1));
  else await expect(badge).toHaveCount(0);
  expect((await django<{ dead_outbox: number }>("badge-counts")).dead_outbox).toBe(before - 1);
});
