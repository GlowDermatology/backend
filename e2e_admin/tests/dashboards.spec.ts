/**
 * The four dashboards (glowderma.reporting.dashboards) with the seeded demo data: each renders, every link on
 * it lands on a page that loads, every number that links to a list equals that list's row count, the
 * one-minute cache and «Yangilash» (`?refresh=1`), and the tab strip between them. The empty-database
 * rendering runs on the gate server (gate.spec.ts).
 */
import type { Page } from "@playwright/test";

import { expect, resultCount, test } from "../lib/fixtures";

const DASHBOARDS = [
  { path: "/django-admin/", title: "Ops holati" },
  { path: "/django-admin/dashboards/money/", title: "Pul solishtirish" },
  { path: "/django-admin/dashboards/sales/", title: "Savdo va katalog" },
  { path: "/django-admin/dashboards/customers/", title: "Mijozlar" },
];

const CHANGELIST = /^\/django-admin\/(?!dashboards\/)[a-z_]+\/[a-z]+\/(\?.*)?$/;

function asNumber(text: string): number | null {
  const clean = text.replace(/[\s  ]/g, "");
  return /^\d+$/.test(clean) ? Number(clean) : null;
}

async function dashboardLinks(page: Page): Promise<{ href: string; text: string; tile: string | null }[]> {
  return page.locator(".gd-dash a[href]").evaluateAll((anchors) =>
    anchors.map((anchor) => {
      const element = anchor as HTMLAnchorElement;
      const value = element.classList.contains("gd-tile") ? element.querySelector(".gd-tile__value")?.firstChild : null;
      return {
        href: element.getAttribute("href") ?? "",
        text: (element.textContent ?? "").trim(),
        tile: value ? (value.textContent ?? "").trim() : null,
      };
    }),
  );
}

/** A link's target as a path (relative links like "?days=7" resolve against the dashboard). */
function absolute(page: Page, href: string): string {
  const url = new URL(href, page.url());
  return url.pathname + url.search;
}

async function listCount(page: Page): Promise<number> {
  const header = page.locator("h1").filter({ hasText: "natija" });
  if ((await header.count()) > 0) return resultCount(page);
  return page.locator("#result_list tbody tr").count();
}

for (const dashboard of DASHBOARDS) {
  test.describe(dashboard.title, () => {
    // One worker, in order: «Yangilash» of one test must not rebuild the cache another test is reading.
    test.describe.configure({ mode: "default" });

    test("renders with the seeded data", async ({ page, staff }) => {
      void staff;
      const response = await page.goto(`${dashboard.path}?refresh=1`);
      expect(response?.status()).toBe(200);
      await expect(page.locator(".gd-dash")).toBeVisible();
      await expect(page.locator("h1").first()).toContainText(dashboard.title);
      await expect(page.locator("[data-gd-generated]")).toHaveAttribute("data-gd-generated", /^\d{4}-\d\d-\d\dT/);
      expect(await page.locator(".gd-dash a[href], .gd-dash .gd-tile").count()).toBeGreaterThan(3);
      // A section that failed to build shows "Xato" (money: a check that raised) — none with healthy data.
      await expect(page.locator(".gd-dash td.num", { hasText: /^Xato$/ })).toHaveCount(0);
    });

    test("every link lands on a page that loads", async ({ page, staff }) => {
      void staff;
      await page.goto(`${dashboard.path}?refresh=1`);
      const links = (await dashboardLinks(page)).filter((link) => link.href && !link.href.startsWith("#"));
      const unique = [...new Set(links.map((link) => absolute(page, link.href)))];
      expect(unique.length).toBeGreaterThan(0);
      for (const href of unique) {
        await test.step(href, async () => {
          const response = await page.goto(href);
          expect(response?.status(), href).toBe(200);
          const path = new URL(page.url()).pathname + new URL(page.url()).search;
          if (CHANGELIST.test(path)) {
            await expect(page.locator("#changelist-form, #result_list").first()).toBeVisible();
            // A filter the admin does not accept is dropped with "?e=1" (Django's invalid-lookup redirect).
            expect(new URL(page.url()).searchParams.get("e"), `${href} was refused as an invalid lookup`).toBeNull();
          }
        });
      }
    });

    test("every number that links to a list matches that list", async ({ page, staff }) => {
      void staff;
      await page.goto(`${dashboard.path}?refresh=1`);
      const claims = (await dashboardLinks(page))
        .map((link) => ({ ...link, href: absolute(page, link.href), number: asNumber(link.tile ?? link.text) }))
        .filter((link) => link.number !== null && CHANGELIST.test(link.href));
      for (const claim of claims) {
        await test.step(`${claim.number} → ${claim.href}`, async () => {
          await page.goto(claim.href);
          expect(await listCount(page), `${claim.href} lists a different number of rows`).toBe(claim.number);
        });
      }
    });

    test("is cached for a minute; «Yangilash» (?refresh=1) rebuilds it", async ({ page, staff }) => {
      void staff;
      await page.goto(`${dashboard.path}?refresh=1`);
      const first = await page.locator("[data-gd-generated]").getAttribute("data-gd-generated");
      await page.waitForTimeout(1100);
      await page.goto(dashboard.path);
      expect(await page.locator("[data-gd-generated]").getAttribute("data-gd-generated")).toBe(first);
      await page.getByRole("link", { name: /Yangilash/ }).click();
      await page.waitForLoadState("domcontentloaded");
      expect(new URL(page.url()).searchParams.get("refresh")).toBe("1");
      const rebuilt = await page.locator("[data-gd-generated]").getAttribute("data-gd-generated");
      expect(rebuilt).not.toBe(first);
      await page.goto(dashboard.path);
      expect(await page.locator("[data-gd-generated]").getAttribute("data-gd-generated")).toBe(rebuilt);
    });

    test("the tab strip reaches the other dashboards", async ({ page, staff }) => {
      void staff;
      await page.goto(dashboard.path);
      for (const other of DASHBOARDS) {
        await page.locator(".gd-bar").getByRole("link", { name: other.title }).first().click();
        await page.waitForLoadState("domcontentloaded");
        expect(new URL(page.url()).pathname).toBe(other.path);
        await expect(page.locator("h1").first()).toContainText(other.title);
      }
    });
  });
}

test("sales dashboard: the window switch and an invalid window", async ({ page, staff }) => {
  void staff;
  for (const days of ["7", "30", "90", "abc", "-5", "100000"]) {
    const response = await page.goto(`/django-admin/dashboards/sales/?days=${days}`);
    expect(response?.status(), `days=${days}`).toBe(200);
    await expect(page.locator(".gd-dash")).toBeVisible();
  }
});
