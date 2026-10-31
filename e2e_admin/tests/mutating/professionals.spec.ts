/**
 * The only editable models of the console (glowderma.professionals.admin, every write through
 * professionals.services): create, edit, a service refusal shown on the form (nothing saved), delete with
 * and without protected rows, the weekly-hours inline, and every editable model's add form.
 */
import type { Page } from "@playwright/test";

import { django } from "../../lib/django";
import { expect, test } from "../../lib/fixtures";

type AuditRow = { id: number; action: string; entity_id: string; before: Record<string, unknown> | null; after: Record<string, unknown> | null };

const auditMax = async () => (await django<{ id: number }>("audit-max")).id;
const audit = (after_id: number, action: string) => django<AuditRow[]>("audit", { after_id, action });
const professionalRow = (id: number) =>
  django<Record<string, unknown> | null>("row", { model: "professionals.professional", filter: { pk: id } });

async function chooseAutocomplete(page: Page, field: string, text: string): Promise<void> {
  await page.locator(`#id_${field}`).locator("xpath=following-sibling::span[contains(@class,'select2')]").first().click();
  await page.locator(".select2-search__field").last().fill(text);
  await page.locator(".select2-results__option", { hasText: text }).first().click();
}

const SAVED = /muvaffaqiyatli (qo‘shildi|o‘zgartirildi)/;
// …and the whole message is Uzbek (Django's own uz catalog lacks the "You may edit it again below" forms).
const ENGLISH_MESSAGE = /successfully|You may|below/;

test("create a professional: saved through the service and audited", async ({ page, staff }) => {
  void staff;
  const since = await auditMax();
  const name = `E2E Yangi ${Date.now()}`;
  await page.goto("/django-admin/professionals/professional/add/");
  await page.locator("#id_name").fill(name);
  await page.locator("#id_role").selectOption("dermatologist");
  await chooseAutocomplete(page, "location", "");
  await page.locator("#id_slot_step_min").fill("20");
  await page.locator("[name=_save]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(/\/professionals\/professional\/$/);
  await expect(page.getByText(SAVED)).toBeVisible();
  const created = await audit(since, "professional.created");
  expect(created).toHaveLength(1);
  const row = await professionalRow(Number(created[0].entity_id));
  expect(row).toMatchObject({ name, role: "dermatologist", slot_step_min: 20, is_active: true });
  expect(created[0].after).toMatchObject({ name });
});

test("edit a professional: the change is saved and audited with before/after", async ({ page, staff }) => {
  void staff;
  const { id } = await django<{ id: number }>("professional", { fresh: true });
  const since = await auditMax();
  await page.goto(`/django-admin/professionals/professional/${id}/change/`);
  await page.locator("#id_bio_uz").fill("E2E tahrir: yangi bio.");
  await page.locator("#id_position").fill("7");
  await page.locator("[name=_continue]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(new RegExp(`/professionals/professional/${id}/change/$`));
  await expect(page.getByText(SAVED)).toBeVisible();
  await expect(page.getByText(ENGLISH_MESSAGE)).toHaveCount(0);
  expect(await professionalRow(id)).toMatchObject({ bio_uz: "E2E tahrir: yangi bio.", position: 7 });
  const rows = await audit(since, "professional.updated");
  expect(rows).toHaveLength(1);
  expect(rows[0].before).toMatchObject({ position: 0 });
  expect(rows[0].after).toMatchObject({ position: 7 });
});

test("a service refusal is shown on the form and nothing is saved (deactivation with upcoming appointments)", async ({ page, staff }) => {
  void staff;
  const professional = await django<{ id: number; upcoming: number }>("professional", { upcoming: true });
  const since = await auditMax();
  await page.goto(`/django-admin/professionals/professional/${professional.id}/change/`);
  await page.locator("#id_bio_en").fill("must not be saved");
  await page.locator("#id_is_active").uncheck();
  await page.locator("[name=_save]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(new RegExp(`/professionals/professional/${professional.id}/change/$`));
  await expect(page.getByText(new RegExp(`Mutaxassisda ${professional.upcoming} ta kelgusi qabul bor`))).toBeVisible();
  await expect(page.getByText(SAVED)).toHaveCount(0);
  // the typed input is kept on the re-rendered form
  await expect(page.locator("#id_bio_en")).toHaveValue("must not be saved");
  const row = await professionalRow(professional.id);
  expect(row).toMatchObject({ is_active: true });
  expect(row?.bio_en).not.toBe("must not be saved");
  expect(await audit(since, "professional.")).toEqual([]);
});

test("delete a professional nothing points to: confirm page, deleted, audited", async ({ page, staff }) => {
  void staff;
  const { id, name } = await django<{ id: number; name: string }>("professional", { fresh: true });
  const since = await auditMax();
  await page.goto(`/django-admin/professionals/professional/${id}/change/`);
  await page.locator(`a[href$="/professional/${id}/delete/"]`).first().click();
  await expect(page).toHaveURL(new RegExp(`/professional/${id}/delete/$`));
  await expect(page.getByText(name).first()).toBeVisible();
  await expect(page.getByText(/ishonchingiz komilmi/)).toBeVisible();
  await page.locator("form input[type=submit], form button[type=submit]").filter({ hasText: /Ha|ishonchim/ }).first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page).toHaveURL(/\/professionals\/professional\/$/);
  await expect(page.getByText(/muvaffaqiyatli o‘chirildi/)).toBeVisible();
  expect(await professionalRow(id)).toBeNull();
  const rows = await audit(since, "professional.deleted");
  expect(rows).toHaveLength(1);
  expect(rows[0].entity_id).toBe(String(id));
});

test("delete a professional with appointments: refused in Uzbek, nothing deleted", async ({ page, staff }) => {
  void staff;
  const professional = await django<{ id: number; name: string }>("professional", { upcoming: true });
  const since = await auditMax();
  await page.goto(`/django-admin/professionals/professional/${professional.id}/delete/`);
  // The admin's own pre-check (appointments are protected and read-only here): no confirm button at all.
  await expect(page.getByText(/o‘chirib bo‘lmaydi|o‘chirish uchun/).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Ha, ishonchim komil/ })).toHaveCount(0);
  expect(await professionalRow(professional.id)).not.toBeNull();
  expect(await audit(since, "professional.deleted")).toEqual([]);
});

test("weekly hours inline: an invalid row is refused on the form, a valid one is saved and audited", async ({ page, staff }) => {
  void staff;
  const { id } = await django<{ id: number }>("professional", { fresh: true });
  await page.goto(`/django-admin/professionals/professional/${id}/change/`);
  await page.locator("a.add-row").click();
  const field = (name: string) => page.locator(`input[name^="working_hours-"][name$="-${name}"]:not([name*="__prefix__"])`).last();
  // the weekday is a select of Uzbek day names (the value stays 0–6)
  const weekday = page.locator('select[name^="working_hours-"][name$="-weekday"]:not([name*="__prefix__"])').last();
  await expect(weekday.locator("option", { hasText: "Dushanba" })).toHaveAttribute("value", "0");
  await weekday.selectOption("0");
  await field("start").fill("18:00");
  await field("end").fill("09:00");
  const since = await auditMax();
  await page.locator("[name=_continue]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page.getByText(SAVED)).toHaveCount(0);
  await expect(page.locator(".errorlist, .errornote, [class*=text-red-600]").first()).toBeVisible();
  expect(await audit(since, "working_hours.")).toEqual([]);

  await field("start").fill("09:00");
  await field("end").fill("18:00");
  await page.locator("[name=_continue]").first().click();
  await page.waitForLoadState("domcontentloaded");
  await expect(page.getByText(SAVED)).toBeVisible();
  const rows = await audit(since, "working_hours.created");
  expect(rows).toHaveLength(1);
  expect(rows[0].after).toMatchObject({ professional_id: id, weekday: 0 });
});

// admin URL name → the services' audit entity (professionals.services.ENTITY_NAMES)
const EDITABLE: Record<string, string> = {
  location: "location",
  professional: "professional",
  service: "service",
  professionalservice: "professional_service",
  workinghours: "working_hours",
  workinghoursoverride: "working_hours_override",
  blockedperiod: "blocked_period",
};

for (const [model, entity] of Object.entries(EDITABLE)) {
  test(`${model}: the add form renders and an empty submit shows the required fields`, async ({ page, staff }) => {
    void staff;
    const since = await auditMax();
    const response = await page.goto(`/django-admin/professionals/${model}/add/`);
    expect(response?.status()).toBe(200);
    await expect(page.locator("[name=_save]").first()).toBeVisible();
    await page.locator("[name=_save]").first().click();
    await page.waitForLoadState("domcontentloaded");
    await expect(page).toHaveURL(new RegExp(`/professionals/${model}/add/$`));
    await expect(page.locator(".errornote, .errorlist, [class*=text-red]").first()).toBeVisible();
    expect(await audit(since, `${entity}.`)).toEqual([]);
  });
}
