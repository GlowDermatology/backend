/**
 * Accessibility (axe-core, WCAG 2.1 A/AA rules) of the sign-in steps (the login page with its side image, the
 * recovery codes page, the Uzbek CSRF page), every dashboard, a changelist with its applied-filters bar, a
 * change page with its Diagnostika tab, and the confirm steps of actions (one with a real warning), in light
 * and dark mode. A serious or critical violation fails the test, except the
 * narrowly allow-listed ones below (Unfold's own markup we cannot change from this project, each with its
 * reason).
 */
import path from "node:path";

import type { Page } from "@playwright/test";

import { codeStep, passwordStep } from "../lib/console";
import { django } from "../lib/django";
import { expect, test } from "../lib/fixtures";
import { base32ToHex, freshCode } from "../lib/totp";

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
  const user = await newStaff({ totp: false, enrol_window: true });
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

test("change page (an order with a failed shipment: the Diagnostika tab, the other tabs)", async ({ page, staff }) => {
  void staff;
  const shipment = await django<{ order_id: number } | null>("row", {
    model: "delivery.shipment",
    filter: { state: "failed", last_error_code__gt: "" },
  });
  await page.goto(`/django-admin/commerce/order/${shipment!.order_id}/change/`);
  await expect(page.locator(".gd-diag__error")).toBeVisible();
  await expectAccessible(page);
});

test("an action's confirm step", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/integrations/job/bts-poll/");
  await expectAccessible(page);
});

/** A page that cannot be reloaded (a POST answer, a page shown once): one fresh run per colour scheme. */
async function expectAccessibleOnce(page: Page, scheme: "light" | "dark", open: () => Promise<void>): Promise<void> {
  await page.emulateMedia({ colorScheme: scheme });
  await open();
  expect(report(await audit(page)), `${scheme} mode`).toBe("");
}

for (const scheme of ["light", "dark"] as const) {
  test(`recovery codes page (${scheme})`, async ({ page, newStaff }) => {
    const user = await newStaff({ totp: false, enrol_window: true });
    await expectAccessibleOnce(page, scheme, async () => {
      await passwordStep(page, user.email, user.password);
      await page.locator(".ops-manual-key summary").click();
      const keyHex = base32ToHex((await page.locator("#manual-key").innerText()).trim());
      await codeStep(page, await freshCode(keyHex), "#enrol-form");
      await expect(page.locator("#recovery-codes code")).toHaveCount(10);
    });
    // and the «shown once» notice of a reload
    await page.reload();
    expect(report(await audit(page)), `${scheme} mode, shown once`).toBe("");
  });

  test(`CSRF failure page (${scheme})`, async ({ page, newStaff, expected }) => {
    expected.status(403, /\/django-admin\/login\/$/);
    const user = await newStaff();
    await expectAccessibleOnce(page, scheme, async () => {
      await page.goto("/django-admin/login/");
      await page.context().clearCookies({ name: "csrftoken" });
      await page.locator("#id_username").fill(user.email);
      await page.locator("#id_password").fill(user.password);
      await page.locator("#login-form button[type=submit]").click();
      await expect(page.getByText(/Sahifa eskirgan/)).toBeVisible();
    });
  });
}

test("changelist with the applied-filters bar (a dashboard drill-down)", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/core/outboxmessage/?status__exact=dead");
  await expect(page.locator(".gd-applied")).toBeVisible();
  await expectAccessible(page);
});

test("an action's confirm step with a real warning (requeue a Billz sale)", async ({ page, staff }) => {
  void staff;
  const message = await django<{ id: number } | null>("row", {
    model: "core.outboxmessage",
    filter: { status: "dead", topic: "billz.push_sale" },
  });
  expect(message, "the demo seed has a dead billz.push_sale message").not.toBeNull();
  await page.goto(`/django-admin/core/outboxmessage/${message!.id}/requeue/`);
  await expect(page.locator(".gd-ops-note--warning").first()).toBeVisible();
  await expectAccessible(page);
});
