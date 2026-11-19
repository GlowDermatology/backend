/**
 * The AI assistant's console pages (glowderma/assistant/console, superusers only): rotating the API key
 * through the write-only form (Vault and the key test are the fakes; the test runs eagerly), a rejected key
 * that keeps the old one, the rollback confirm page, and a prompt version from draft to test to activation
 * and back (rollback). A manager is refused.
 */
import { django } from "../../lib/django";
import { expect, test } from "../../lib/fixtures";

type AuditRow = { id: number; action: string; before: unknown; after: unknown; reason: string };

const auditMax = async () => (await django<{ id: number }>("audit-max")).id;
const audit = (after_id: number, action: string) => django<AuditRow[]>("audit", { after_id, action });

const PROVIDER = "/django-admin/ai/providers/gemini/";
const KEY = `AIzaE2E-${Date.now()}-abcdefghijklmnop`;
const KEY_2 = `AIzaE2E-second-${Date.now()}-qrstuvwxyz`;

test("a manager cannot open the AI pages", async ({ page, staff, expected }) => {
  void staff;
  expected.status(403);
  const response = await page.goto("/django-admin/ai/");
  expect(response?.status()).toBe(403);
});

test("rotate the API key, reject a bad one, roll back", async ({ page, context, newStaff, signIn }) => {
  await signIn(context, await newStaff({ superuser: true, full_name: "E2E Superuser" }));
  const since = await auditMax();

  await page.goto(`${PROVIDER}key/`);
  const input = page.locator("input[name=api_key]");
  await expect(input).toHaveAttribute("type", "password");
  await expect(input).toHaveAttribute("autocomplete", "new-password");
  await input.fill(KEY);
  await page.getByRole("button", { name: "Saqlash va tekshirish" }).click();
  await expect(page).toHaveURL(new RegExp(`${PROVIDER}$`));
  await expect(page.getByTestId("ai-configured")).toHaveText("Ha");
  await expect(page.getByTestId("ai-test-status")).toHaveText("Ishlayapti");
  const first = Number(await page.getByTestId("ai-active-version").innerText());
  expect(await page.content()).not.toContain(KEY);

  // a key the provider refuses: the old one stays active
  await page.goto(`${PROVIDER}key/`);
  await page.locator("input[name=api_key]").fill("invalid-e2e-key-0123456789");
  await page.getByRole("button", { name: "Saqlash va tekshirish" }).click();
  await expect(page.getByTestId("ai-test-status")).toHaveText("Xato");
  await expect(page.getByTestId("ai-active-version")).toHaveText(String(first));
  await expect(page.getByTestId("ai-pending-version")).toHaveText("-");

  // a second good key, then back to the first one
  await page.goto(`${PROVIDER}key/`);
  await page.locator("input[name=api_key]").fill(KEY_2);
  await page.getByRole("button", { name: "Saqlash va tekshirish" }).click();
  const second = Number(await page.getByTestId("ai-active-version").innerText());
  expect(second).toBeGreaterThan(first);
  await page.getByTestId("ai-rollback-link").click();
  await page.getByTestId("ai-rollback-confirm").click();
  await expect(page.getByTestId("ai-active-version")).toHaveText(String(first));
  await expect(page.getByTestId("ai-previous-version")).toHaveText(String(second));

  const rows = [
    ...(await audit(since, "assistant.credential_rotated")),
    ...(await audit(since, "assistant.credential_rejected")),
    ...(await audit(since, "assistant.credential_rolled_back")),
  ];
  expect(rows.length).toBe(4);
  const stored = JSON.stringify(rows);
  expect(stored).not.toContain(KEY);
  expect(stored).not.toContain(KEY_2);
});

test("a prompt draft is tested on a sample product, activated and rolled back", async ({ page, context, newStaff, signIn }) => {
  await signIn(context, await newStaff({ superuser: true }));
  const product = await django<{ id: number }>("row", { model: "catalog.product" });
  await page.goto("/django-admin/ai/prompts/seo/");
  const activeBefore = await page.locator("[data-testid=ai-versions] tbody tr:has(.gd-ai-badge--active) a").innerText();
  await page.getByTestId("ai-new-draft").click();
  await expect(page.getByTestId("ai-version-status")).toHaveText("Qoralama");
  await page.locator("textarea[name=instructions]").fill("E2E: sarlavhalar qisqa, mahsulot nomi boshida.");
  await page.getByRole("button", { name: "Qoralamani saqlash" }).click();
  await expect(page.getByText("Saqlandi.")).toBeVisible();
  // not tested yet: no activation button
  await expect(page.getByTestId("ai-activate")).toHaveCount(0);
  await page.locator("input[name=product]").fill(String(product.id));
  await page.getByTestId("ai-run-test").click();
  await expect(page.getByText(/Sinov boshlandi/)).toBeVisible();
  // the fake model answers within the request (eager tasks): a reload shows the outcome
  await page.reload();
  await expect(page.getByTestId("ai-version-status")).toHaveText("Sinovdan o‘tgan");
  await expect(page.getByTestId("ai-last-test")).toContainText("Muvaffaqiyatli");
  await page.getByTestId("ai-activate").click();
  await expect(page.getByTestId("ai-version-status")).toHaveText("Faol");

  // rollback: the previous active version (now retired) becomes active again
  await page.goto("/django-admin/ai/prompts/seo/");
  await page.locator("[data-testid=ai-versions] a").getByText(activeBefore, { exact: true }).click();
  await expect(page.getByTestId("ai-version-status")).toHaveText("Arxivda");
  await page.getByTestId("ai-activate").click();
  await expect(page.getByTestId("ai-version-status")).toHaveText("Faol");
});
