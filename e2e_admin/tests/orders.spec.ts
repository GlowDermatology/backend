/**
 * The order's console page and the ways to reach it (glowderma.commerce.admin, glowderma.commerce.diagnostics):
 * the «Diagnostika» tab first (status, payment, the shipment's last BTS error, Billz, the order's outbox
 * messages and webhooks as real filtered lists), the «Hodisalar» tab, no history button, the «Buyurtma №»
 * lookup on the Ops banner, and the customer page's masked contact.
 */
import { django } from "../lib/django";
import { expect, resultCount, test } from "../lib/fixtures";

interface ShipmentRow {
  order_id: number;
  last_error_code: string;
  last_error: string;
}

test("an order with a failed shipment: Diagnostika shows the error, the outbox link lists exactly its messages", async ({
  page,
  staff,
}) => {
  void staff;
  const shipment = await django<ShipmentRow | null>("row", {
    model: "delivery.shipment",
    filter: { state: "failed", last_error_code__gt: "" },
  });
  expect(shipment, "the demo seed has a failed shipment with an error").not.toBeNull();
  const orderId = shipment!.order_id;
  await page.goto(`/django-admin/commerce/order/${orderId}/change/`);
  await expect(page.locator("h1").first()).toContainText(`Buyurtma №${orderId}`);

  // the first tab, open on arrival
  const firstTab = page.locator('[x-data*="activeFieldsetTab"] nav a').first();
  await expect(firstTab).toHaveText("Diagnostika");
  await expect(firstTab).toHaveClass(/active/);
  const diag = page.locator(".gd-diag");
  await expect(diag).toBeVisible();
  await expect(diag).toContainText("Buyurtma holati");
  await expect(diag.locator(".gd-diag__error")).toContainText(`Oxirgi xato: ${shipment!.last_error_code} — ${shipment!.last_error}`);

  const { count: messages } = await django<{ count: number }>("count", {
    model: "core.outboxmessage",
    filter: { payload__order_id: orderId },
  });
  const outbox = diag.locator('a[href*="/core/outboxmessage/?order="]').first();
  if (messages > 0) {
    await expect(outbox).toHaveText(`${messages} ta xabar`);
    await outbox.click();
    await page.waitForLoadState("domcontentloaded");
    expect(new URL(page.url()).searchParams.get("order")).toBe(String(orderId));
    expect(await resultCount(page)).toBe(messages);
    // the drill-down is never invisible: the applied-filters bar names it
    const applied = page.locator(".gd-applied");
    await expect(applied).toContainText("Qo‘llangan filtrlar:");
    await expect(applied).toContainText(`№${orderId}`);
    await applied.getByRole("link", { name: "Filtrlarni tozalash" }).click();
    await expect(page).toHaveURL(/\/core\/outboxmessage\/$/);
    await expect(page.locator(".gd-applied")).toHaveCount(0);
  } else {
    await expect(outbox).toHaveCount(0);
    await expect(diag).toContainText("Bu buyurtma bo‘yicha xabar yo‘q");
  }
});

test("the order page: «Hodisalar» tab, no «Tarix» history button", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/commerce/order/");
  await page.locator("#result_list tbody tr a[href*='/change/']").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page.getByRole("link", { name: "Hodisalar", exact: true })).toHaveCount(1);
  await expect(page.locator('a[href$="/history/"]')).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Tarix", exact: true })).toHaveCount(0);
});

test("«Buyurtma №» on the Ops banner opens the order; an unknown or malformed number lands on the list", async ({
  page,
  staff,
}) => {
  void staff;
  const order = await django<{ id: number } | null>("row", { model: "commerce.order", filter: {} });
  expect(order).not.toBeNull();
  await page.goto("/django-admin/");
  const lookup = page.locator("form.gd-lookup");
  await lookup.getByLabel("Buyurtma №").fill(String(order!.id));
  await lookup.getByRole("button", { name: "Buyurtmani ochish" }).click();
  await expect(page).toHaveURL(new RegExp(`/commerce/order/${order!.id}/change/$`));

  await page.goto("/django-admin/");
  await lookup.getByLabel("Buyurtma №").fill("999999999");
  await lookup.getByRole("button", { name: "Buyurtmani ochish" }).click();
  await expect(page).toHaveURL(/\/commerce\/order\/$/);
  await expect(page.getByText("№999999999 raqamli buyurtma topilmadi.")).toBeVisible();

  // the input asks for digits; a hand-made URL with letters is refused politely
  await page.goto("/django-admin/commerce/order/lookup/?number=abc");
  await expect(page).toHaveURL(/\/commerce\/order\/$/);
  await expect(page.getByText(/Buyurtma raqamini kiriting/)).toBeVisible();
});

test("the customer page masks the phone like the list", async ({ page, staff }) => {
  void staff;
  const customer = await django<{ id: number; phone: string | null } | null>("row", {
    model: "accounts.customer",
    filter: { phone__startswith: "+998" },
  });
  expect(customer).not.toBeNull();
  const phone = customer!.phone!;
  const masked = `+998 ${phone.slice(4, 6)} *** ** ${phone.slice(-2)}`;
  await page.goto(`/django-admin/accounts/customer/${customer!.id}/change/`);
  await expect(page.locator("#content-main, #content").first()).toContainText(masked);
  expect(await page.content()).not.toContain(phone);
});
