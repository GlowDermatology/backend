/**
 * The console speaks Uzbek: on the main pages no visible English UI string remains where the project (or
 * Django/Unfold) has an Uzbek translation. Product and provider names (Billz, Click, Payme, BTS, Outbox,
 * Google…) are names, not UI words. Every registered model's change page (its tabs and inlines included) is
 * scanned for a label Django derives from an English field name ("Last attempt at"): every console field has
 * an explicit Uzbek label.
 */
import type { Page } from "@playwright/test";

import { passwordStep } from "../lib/console";
import { django, djangoSync } from "../lib/django";
import { expect, test as base } from "../lib/fixtures";

const test = base.extend<object, { changePages: Record<string, { change_url: string | null }> }>({
  changePages: [async ({}, use) => use(await django("facts")), { scope: "worker" }],
});
const DERIVED_LABELS = djangoSync<Record<string, string[]>>("english-labels");

// Whole words of the stock Django/Unfold English UI (case-sensitive: they are capitalised as labels).
const ENGLISH = [
  "Save",
  "Delete",
  "Search",
  "Filter",
  "Filters",
  "Add",
  "Recent actions",
  "Log out",
  "Log in",
  "Change password",
  "Home",
  "Dashboard",
  "History",
  "Show counts",
  "Hide counts",
  "Clear all filters",
  "Apply Filters",
  "View site",
  "Previous",
  "Next",
  "Close",
  "Cancel",
  "Welcome",
  "Select all",
  "Run",
  "Today",
  "Now",
  "Actions",
  "Action",
  "results",
  "Username",
  "Password",
  "Email address",
  "Remember me",
  "Yes",
  "No",
  "Unknown",
  "All",
  "Any date",
  "Past 7 days",
  "This month",
  "This year",
  "Light",
  "Dark",
  "System",
  "devices",
  "Type to search",
  "No results",
  "Go",
  "Submit",
  "Confirm",
  "Back",
  "General",
  "Change",
  "Verify",
  "Required",
  "Users",
  "Topic",
  "Superuser status",
  "Click to cancel",
  "This item will be deleted",
  "Add another",
  "Created at",
  "Updated at",
  "Last used at",
  "Last login",
  "October",
  "Note: Enter times",
];

async function englishWords(page: Page): Promise<string[]> {
  const text = await page.evaluate(() => {
    // Visible text only (Material Symbols ligatures such as "devices" or "today" are icons, not words), plus
    // the labels a screen reader announces (placeholders, titles, aria-labels).
    const chunks: string[] = [document.title];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || parent.closest(".material-symbols-outlined, script, style, noscript, template")) continue;
      if (!parent.checkVisibility()) continue;
      chunks.push(node.textContent ?? "");
    }
    for (const element of document.querySelectorAll("[placeholder],[aria-label],[title]")) {
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") continue;
      for (const name of ["placeholder", "aria-label", "title"]) {
        const value = element.getAttribute(name);
        if (value) chunks.push(value);
      }
    }
    return chunks.join("\n");
  });
  // whole words: "1Password" (a product name in the 2FA hint) is not "Password"
  return ENGLISH.filter((word) => new RegExp(`(^|[^\\p{L}\\p{N}])${word}([^\\p{L}\\p{N}]|$)`, "u").test(text));
}

const STAFF_PAGES = [
  "/django-admin/",
  "/django-admin/dashboards/money/",
  "/django-admin/dashboards/sales/",
  "/django-admin/dashboards/customers/",
  "/django-admin/commerce/order/",
  "/django-admin/core/outboxmessage/",
  "/django-admin/accounts/staffuser/",
  "/django-admin/otp_totp/totpdevice/",
  "/django-admin/professionals/professional/add/",
  "/django-admin/integrations/job/bts-poll/",
];

for (const path of STAFF_PAGES) {
  test(`no English UI on ${path}`, async ({ page, staff }) => {
    void staff;
    await page.goto(path);
    // Open the menus that hold more labels: filters (if any) and the user menu.
    const filters = page.locator("a.filter-open-button");
    if (await filters.count()) await filters.click();
    expect(await englishWords(page)).toEqual([]);
  });
}

test("no English UI on a change page with tabs and history", async ({ page, staff }) => {
  void staff;
  await page.goto("/django-admin/commerce/order/");
  await page.locator("#result_list tbody tr a[href*='/change/']").first().click();
  await page.waitForLoadState("domcontentloaded");
  expect(await englishWords(page)).toEqual([]);
  await page.goto(`${page.url()}history/`.replace("change/history/", "history/"));
  expect(await englishWords(page)).toEqual([]);
});

test("no English UI on the sign-in steps", async ({ page, newStaff }) => {
  await page.goto("/django-admin/login/");
  expect(await englishWords(page)).toEqual([]);
  const user = await newStaff();
  await passwordStep(page, user.email, "wrong-password");
  expect(await englishWords(page)).toEqual([]);
  await passwordStep(page, user.email, user.password);
  expect(await englishWords(page)).toEqual([]);
  await page.goto("/django-admin/2fa/recovery/");
  expect(await englishWords(page)).toEqual([]);
});

/** Every text node of the page's main column, hidden tabs and inlines included (JSON payloads are data). */
async function textLines(page: Page): Promise<Set<string>> {
  const lines = await page.evaluate(() => {
    const root = document.querySelector("#content") ?? document.body;
    const found: string[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement;
      if (!parent || parent.closest(".material-symbols-outlined, script, style, noscript, template, pre.gd-json")) continue;
      const text = (node.textContent ?? "").replace(/\s+/g, " ").trim().replace(/:$/, "");
      if (text) found.push(text);
    }
    return found;
  });
  return new Set(lines);
}

for (const [label, derived] of Object.entries(DERIVED_LABELS)) {
  test(`no English field label on the change page of ${label}`, async ({ page, staff, changePages }) => {
    void staff;
    const url = changePages[label]?.change_url;
    test.skip(!url, `${label} has no row`);
    const response = await page.goto(url!);
    expect(response?.status()).toBe(200);
    const lines = await textLines(page);
    expect(derived.filter((english) => lines.has(english)), `English labels on ${url}`).toEqual([]);
    expect(await englishWords(page)).toEqual([]);
  });
}
