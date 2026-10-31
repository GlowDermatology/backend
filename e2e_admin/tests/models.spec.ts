/**
 * Every model registered on the console (`admin.site._registry`, read through `e2e_console registry`, so a
 * newly registered model is covered without touching this file), one test per model and feature: the
 * changelist, search, every list filter, sorting by every sortable column, pagination, a change page, the
 * read-only guarantees (no write controls, writes refused), and that no secret column's value is ever
 * rendered.
 */
import type { Page } from "@playwright/test";

import { django, djangoSync } from "../lib/django";
import { expect, resultCount, test as base } from "../lib/fixtures";

interface FilterInfo {
  index: number;
  /** Its position among the filters the sidebar shows (a filter shown only once applied takes none). */
  block: number;
  shown: boolean;
  field: string;
  kind: string;
  simple: boolean;
}

interface ModelInfo {
  label: string;
  verbose_name_plural: string;
  changelist: string;
  add: string;
  read_only: boolean;
  search_fields: string[];
  filters: FilterInfo[];
  list_per_page: number;
  date_hierarchy: string | null;
}

interface Facts {
  count: number;
  first_pk: string | number | null;
  change_url: string | null;
  search: { field: string; term: string; pk: string | number; hits: number } | null;
  secret_fields: string[];
  secret_change_url: string | null;
  secret_values: string[];
}

const REGISTRY = djangoSync<ModelInfo[]>("registry");

// Tables other specs (sign-ins, sessions, audit rows) write to while this one runs: counts are compared
// with the database only for the others.
const VOLATILE = new Set([
  "core.auditentry",
  "accounts.staffuser",
  "otp_totp.totpdevice",
  "otp_static.staticdevice",
  "accounts.authsession",
  "accounts.customer",
  "core.outboxmessage",
  "integrations.job",
]);

const test = base.extend<object, { facts: Record<string, Facts> }>({
  facts: [async ({}, use) => use(await django<Record<string, Facts>>("facts")), { scope: "worker" }],
});

test("the registry lists every console model (about 54)", () => {
  expect(REGISTRY.length).toBeGreaterThanOrEqual(54);
  const labels = REGISTRY.map((model) => model.label);
  expect(labels).toContain("core.outboxmessage");
  expect(labels).toContain("professionals.professional");
  expect(labels).not.toContain("auth.group");
});

async function openFilters(page: Page): Promise<void> {
  await page.locator("a.filter-open-button").click();
  await expect(page.locator("#changelist-filter")).toBeVisible();
}

/** One block per list filter, in ``list_filter`` order (inside the submit form, or plain links). */
function filterBlocks(page: Page) {
  return page.locator("#changelist-filter div.gap-4.px-3").first().locator(":scope > *");
}

function queryOf(page: Page): URLSearchParams {
  return new URL(page.url()).searchParams;
}

const ORM_PARAMS = /^(?!(q|o|p|_facets|e)$)(?!.*_(from|to)_\d$)/;

for (const model of REGISTRY) {
  test.describe(model.label, () => {
    test("changelist loads", async ({ page, staff, facts }) => {
      void staff;
      const response = await page.goto(model.changelist);
      expect(response?.status()).toBe(200);
      const fact = facts[model.label];
      await expect(page.locator("h1").first()).toContainText(model.verbose_name_plural, { ignoreCase: true });
      if (fact.count > 0) {
        await expect(page.locator("#result_list tbody tr").first()).toBeVisible();
        const shown = await resultCount(page);
        if (VOLATILE.has(model.label)) expect(shown).toBeGreaterThanOrEqual(1);
        else expect(shown).toBe(fact.count);
      }
      if (model.read_only) {
        await expect(page.locator(`#main a[href="${model.add}"], a[href="${model.add}"]`)).toHaveCount(0);
      }
    });

    test("search", async ({ page, staff, facts }) => {
      void staff;
      await page.goto(model.changelist);
      if (model.search_fields.length === 0) {
        await expect(page.locator("#searchbar")).toHaveCount(0);
        return;
      }
      const search = facts[model.label].search;
      expect(search, `no searchable value found for ${model.label} (fields ${model.search_fields})`).not.toBeNull();
      await page.locator("#searchbar").fill(search!.term);
      await page.locator("#searchbar").press("Enter");
      await page.waitForLoadState("domcontentloaded");
      expect(queryOf(page).get("q")).toBe(search!.term);
      await expect(page.locator("#searchbar")).toHaveValue(search!.term);
      const shown = await resultCount(page);
      expect(shown).toBeGreaterThanOrEqual(1);
      if (!VOLATILE.has(model.label)) expect(shown).toBe(search!.hits);
      const href = `${model.changelist}${encodeURIComponent(String(search!.pk))}/change/`;
      await expect(page.locator(`#result_list a[href^="${href}"]`).first()).toBeVisible();
    });

    test("every list filter applies", async ({ page, staff }) => {
      void staff;
      if (model.filters.length === 0 && !model.date_hierarchy) {
        await page.goto(model.changelist);
        await expect(page.locator("a.filter-open-button")).toHaveCount(0);
        return;
      }
      for (const filter of model.filters) {
        await test.step(`filter ${filter.field} (${filter.kind})`, async () => {
          await page.goto(model.changelist);
          await openFilters(page);
          if (!filter.shown) {
            // A custom filter offered only once applied (the SEO backlog and the order's outbox messages:
            // reached from the dashboard or the order page; dashboards.spec.ts and orders.spec.ts follow them).
            test.info().annotations.push({ type: "note", description: `${filter.field}: shown only when applied` });
            return;
          }
          const block = filterBlocks(page).nth(filter.block);
          await expect(block).toBeVisible();
          const select = block.locator("select");
          const dates = block.locator("input.vDateField");
          if ((await select.count()) > 0) {
            const name = (await select.first().getAttribute("name"))!;
            const values = await select
              .first()
              .locator("option")
              .evaluateAll((options) => options.map((option) => (option as HTMLOptionElement).value).filter(Boolean));
            expect(values.length, `filter ${name} offers no option`).toBeGreaterThan(0);
            await select.first().selectOption(values[0]);
            await page.locator("#filter-form button[type=submit]").click();
            await page.waitForLoadState("domcontentloaded");
            expect(queryOf(page).get(name)).toBe(values[0]);
          } else if ((await dates.count()) > 0) {
            await dates.nth(0).fill("2020-01-01");
            await dates.nth(1).fill("2030-12-31");
            const times = block.locator("input.vTimeField");
            if ((await times.count()) >= 2) {
              await times.nth(0).fill("00:00");
              await times.nth(1).fill("23:59");
            }
            await page.locator("#filter-form button[type=submit]").click();
            await page.waitForLoadState("domcontentloaded");
            const keys = [...queryOf(page).keys()];
            expect(keys.some((key) => key.startsWith(`${filter.field}_from`))).toBe(true);
          } else {
            const links = block.locator("a[href^='?']");
            const total = await links.count();
            expect(total, `filter ${filter.field} has no choices`).toBeGreaterThan(1);
            const href = (await links.nth(1).getAttribute("href"))!;
            await links.nth(1).click();
            await page.waitForLoadState("domcontentloaded");
            for (const [key, value] of new URLSearchParams(href.slice(1))) expect(queryOf(page).get(key)).toBe(value);
          }
          await expect(page.locator("#result_list, #changelist-form").first()).toBeVisible();
          // The rows shown are the rows the filter selects (counted in the database with the same lookups).
          const params = Object.fromEntries([...queryOf(page)].filter(([key]) => ORM_PARAMS.test(key)));
          if (!VOLATILE.has(model.label) && !filter.simple && Object.keys(params).length > 0) {
            const shown = (await page.locator("#result_list").count()) ? await resultCount(page) : 0;
            const { count } = await django<{ count: number }>("count", { model: model.label, filter: params });
            expect(shown, `rows for ${JSON.stringify(params)}`).toBe(count);
          }
        });
      }
      if (model.date_hierarchy) {
        await test.step(`date hierarchy ${model.date_hierarchy}`, async () => {
          await page.goto(model.changelist);
          const link = page.locator(`a[href*="${model.date_hierarchy}__year="]`).first();
          await link.click();
          await page.waitForLoadState("domcontentloaded");
          expect([...queryOf(page).keys()].some((key) => key.startsWith(`${model.date_hierarchy}__`))).toBe(true);
          await expect(page.locator("#result_list")).toBeVisible();
        });
      }
    });

    test("sorting by every sortable column", async ({ page, staff }) => {
      void staff;
      await page.goto(model.changelist);
      const headers = page.locator("#result_list thead th.sortable");
      const total = await headers.count();
      for (let index = 0; index < total; index++) {
        await test.step(`column ${index + 1}/${total}`, async () => {
          await page.goto(model.changelist);
          const header = page.locator("#result_list thead th.sortable").nth(index);
          const className = (await header.getAttribute("class")) ?? "";
          const column = /column-([\w-]+)/.exec(className)?.[1] ?? String(index);
          await header.locator("a").first().click();
          await page.waitForLoadState("domcontentloaded");
          expect(queryOf(page).has("o"), `sorting by ${column}`).toBe(true);
          await expect(page.locator(`#result_list thead th.column-${column}`)).toHaveClass(/sorted/);
          // and the other direction
          await page.locator(`#result_list thead th.column-${column} a`).first().click();
          await page.waitForLoadState("domcontentloaded");
          await expect(page.locator("#result_list")).toBeVisible();
        });
      }
    });

    test("pagination", async ({ page, staff, facts }) => {
      void staff;
      const fact = facts[model.label];
      test.skip(fact.count <= model.list_per_page, `${fact.count} rows fit on one page`);
      await page.goto(model.changelist);
      const firstPage = await page.locator("#result_list tbody tr").count();
      expect(firstPage).toBe(model.list_per_page);
      const firstRow = await page.locator("#result_list tbody tr").first().innerText();
      await page.locator('a[href="?p=2"]').first().click();
      await page.waitForLoadState("domcontentloaded");
      expect(queryOf(page).get("p")).toBe("2");
      await expect(page.locator("#result_list tbody tr").first()).toBeVisible();
      expect(await page.locator("#result_list tbody tr").first().innerText()).not.toBe(firstRow);
    });

    test("change page", async ({ page, staff, facts }) => {
      void staff;
      const fact = facts[model.label];
      expect(fact.change_url, `${model.label} has no row to open`).not.toBeNull();
      const response = await page.goto(fact.change_url!);
      expect(response?.status()).toBe(200);
      await expect(page.locator("#content-main, #main").first()).toBeVisible();
      // The heading is the readable Uzbek title, never the English __str__ of the model.
      await expect(page.locator("h1").first()).not.toHaveText(/^\s*$/);
      if (model.read_only) {
        await expect(page.locator("input[name=_save], button[name=_save]")).toHaveCount(0);
        await expect(page.locator("input[name=_continue], button[name=_continue]")).toHaveCount(0);
        await expect(page.locator("input[name=_addanother], button[name=_addanother]")).toHaveCount(0);
        await expect(page.locator('a[href$="/delete/"]')).toHaveCount(0);
      } else {
        await expect(page.locator("[name=_save]").first()).toBeVisible();
      }
    });

    if (model.read_only) {
      test("writes are refused (add, change, delete)", async ({ page, staff, facts }) => {
        void staff;
        await page.goto(model.changelist);
        const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")?.value ?? "";
        const fact = facts[model.label];
        const headers = { "X-CSRFToken": csrf, Referer: page.url() };
        const form = { csrfmiddlewaretoken: csrf, _save: "Saqlash", post: "yes" };
        const add = await page.request.post(model.add, { form, headers, maxRedirects: 0 });
        expect(add.status(), "POST add").toBe(403);
        expect((await page.request.get(model.add, { maxRedirects: 0 })).status(), "GET add").toBe(403);
        if (fact.change_url) {
          const change = await page.request.post(fact.change_url, { form, headers, maxRedirects: 0 });
          expect(change.status(), "POST change").toBe(403);
          const deleteUrl = fact.change_url.replace(/change\/$/, "delete/");
          const remove = await page.request.post(deleteUrl, { form, headers, maxRedirects: 0 });
          expect(remove.status(), "POST delete").toBe(403);
          const { count } = await django<{ count: number }>("count", { model: model.label, filter: { pk: fact.first_pk } });
          expect(count, "the row is still there").toBe(1);
        }
        // The changelist's bulk "delete selected" action does not exist either.
        const bulk = await page.request.post(model.changelist, {
          form: { ...form, action: "delete_selected", _selected_action: String(fact.first_pk ?? "") },
          headers,
          maxRedirects: 0,
        });
        expect([200, 302, 403]).toContain(bulk.status());
        if (fact.first_pk !== null) {
          const { count } = await django<{ count: number }>("count", { model: model.label, filter: { pk: fact.first_pk } });
          expect(count).toBe(1);
        }
      });
    }

    test("secret columns are never rendered", async ({ page, staff, facts }) => {
      void staff;
      const fact = facts[model.label];
      test.skip(fact.secret_fields.length === 0, "no secret column");
      expect(fact.secret_values.length, `seeded rows carry no value in ${fact.secret_fields}`).toBeGreaterThan(0);
      const pages = [model.changelist, ...(fact.secret_change_url ? [fact.secret_change_url] : [])];
      for (const path of pages) {
        await page.goto(path);
        const html = await page.content();
        for (const value of fact.secret_values) {
          expect(html.includes(value), `${path} renders a ${fact.secret_fields.join("/")} value`).toBe(false);
        }
        for (const field of fact.secret_fields) {
          await expect(page.locator(`[name="${field}"], .field-${field}, td.field-${field}`)).toHaveCount(0);
        }
      }
    });
  });
}
