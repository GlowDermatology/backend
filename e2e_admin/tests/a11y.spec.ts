/**
 * Accessibility (axe-core, WCAG 2.1 A/AA rules) of the sign-in steps, every dashboard, a changelist, a
 * change page and an action's confirm step. A serious or critical violation fails the test, except the
 * narrowly allow-listed ones below (Unfold's own markup we cannot change from this project, each with its
 * reason).
 */
import path from "node:path";

import type { Page } from "@playwright/test";

import { passwordStep } from "../lib/console";
import { expect, test } from "../lib/fixtures";

const AXE = path.join(__dirname, "..", "node_modules", "axe-core", "axe.min.js");

interface Violation {
  id: string;
  impact: string | null;
  help: string;
  nodes: { target: string[]; html: string; failureSummary?: string }[];
}

/** rule id → selectors (substring of the axe target) it is allowed on, with the reason. */
const ALLOWED: Record<string, { target: RegExp; reason: string }[]> = {
  "aria-input-field-name": [
    {
      target: /\.select2-selection|#select2-id_\w+-container/,
      reason:
        "Django admin's autocomplete widget (its vendored select2) renders the combobox <span> without " +
        "aria-labelledby; the <select> it enhances keeps its <label>. Vendor JavaScript, not project markup.",
    },
  ],
};

async function audit(page: Page): Promise<Violation[]> {
  await page.addScriptTag({ path: AXE });
  const result = await page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (context: unknown, options: unknown) => Promise<{ violations: Violation[] }> } }).axe;
    return axe.run(document, { runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] } });
  });
  return result.violations
    .filter((violation) => violation.impact === "serious" || violation.impact === "critical")
    .map((violation) => ({
      ...violation,
      nodes: violation.nodes.filter(
        (node) => !(ALLOWED[violation.id] ?? []).some((entry) => entry.target.test(node.target.join(" "))),
      ),
    }))
    .filter((violation) => violation.nodes.length > 0);
}

function report(violations: Violation[]): string {
  return violations
    .map(
      (violation) =>
        `${violation.id} (${violation.impact}): ${violation.help}\n` +
        violation.nodes
          .slice(0, 8)
          .map((node) => `   ${node.target.join(" ")}  ${node.html.slice(0, 140)}  [${(node.failureSummary ?? "").split("\n").slice(1).join(" ").trim().slice(0, 160)}]`)
          .join("\n"),
    )
    .join("\n");
}

async function expectAccessible(page: Page): Promise<void> {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.reload();
    const violations = await audit(page);
    expect(report(violations), `${scheme} mode`).toBe("");
  }
}

test("login page", async ({ page }) => {
  await page.goto("/django-admin/login/");
  await expectAccessible(page);
});

test("login page with an error", async ({ page, newStaff }) => {
  const user = await newStaff();
  await passwordStep(page, user.email, "wrong-password");
  expect(report(await audit(page))).toBe("");
});

test("2FA code step and recovery step", async ({ page, newStaff }) => {
  const user = await newStaff();
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/2fa\/$/);
  await expectAccessible(page);
  await page.goto("/django-admin/2fa/recovery/");
  await expectAccessible(page);
});

test("2FA enrolment step", async ({ page, newStaff }) => {
  const user = await newStaff({ totp: false });
  await passwordStep(page, user.email, user.password);
  await expect(page).toHaveURL(/\/2fa\/enrol\/$/);
  await expectAccessible(page);
});

for (const [name, url] of [
  ["Ops holati", "/django-admin/"],
  ["Pul solishtirish", "/django-admin/dashboards/money/"],
  ["Savdo va katalog", "/django-admin/dashboards/sales/"],
  ["Mijozlar va kirish", "/django-admin/dashboards/customers/"],
  ["changelist (orders, filters open)", "/django-admin/commerce/order/"],
  ["changelist (outbox)", "/django-admin/core/outboxmessage/"],
  ["editable form (new professional)", "/django-admin/professionals/professional/add/"],
] as const) {
  test(`${name}`, async ({ page, staff }) => {
    void staff;
    await page.goto(url);
    await expectAccessible(page);
  });
}

test("change page (an order, with tabs)", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/commerce/order/");
  await page.locator("#result_list tbody tr a[href*='/change/']").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expectAccessible(page);
});

test("an action's confirm step", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/integrations/job/bts-poll/");
  await expectAccessible(page);
});
