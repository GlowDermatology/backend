/**
 * The console's safe audited actions (glowderma.core.admin_actions and the staff 2FA reset), one test per
 * action and path: the GET shows the confirm step, «Bekor qilish» changes nothing, the confirmed POST shows
 * the success message, the effect is in the database and the `ops.*` audit row exists; manager-only actions
 * are refused to other staff; an action that makes no sense for a row is hidden and refused.
 */
import type { Browser, Page } from "@playwright/test";

import { django } from "../../lib/django";
import { addSession, expect, MAIN_URL, test, type StaffMember } from "../../lib/fixtures";

type AuditRow = { id: number; action: string; entity_type: string; entity_id: string; after: Record<string, unknown> | null };

const auditMax = async () => (await django<{ id: number }>("audit-max")).id;
const audit = (after_id: number, action: string) => django<AuditRow[]>("audit", { after_id, action });

/** Open an action's confirm step from its button (Unfold loads it into the modal). */
async function openAction(page: Page, label: string | RegExp): Promise<void> {
  await page.getByRole("link", { name: label }).first().click();
  await expect(page.locator("#modal-content #dialog form")).toBeVisible();
}

async function confirm(page: Page): Promise<void> {
  await page.locator("#modal-content #dialog button[name=_ops_confirm], #dialog button[name=_ops_confirm]").first().click();
  await page.waitForLoadState("domcontentloaded");
}

async function cancelModal(page: Page): Promise<void> {
  await page.locator("#modal-content #dialog").getByRole("link", { name: "Bekor qilish" }).click();
  await expect(page.locator("#modal-content #dialog form")).toBeHidden();
}

/** A browser context of another (non-manager) staff member, signed in. */
async function asStaff(browser: Browser, user: StaffMember): Promise<Page> {
  const context = await browser.newContext({ baseURL: MAIN_URL });
  const { session_key } = await django<{ session_key: string }>("session", { email: user.email });
  await addSession(context, session_key);
  return context.newPage();
}

test.describe("outbox: requeue one message", () => {
  test("confirm step, cancel changes nothing", async ({ page, staff }) => {
    void staff;
    const { id } = await django<{ id: number }>("dead-outbox");
    const since = await auditMax();
    await page.goto(`/django-admin/core/outboxmessage/${id}/change/`);
    await openAction(page, "Qayta navbatga qo‘yish");
    await expect(page.locator("#dialog h1")).toHaveText("Xabarni qayta navbatga qo‘yish");
    await expect(page.locator("#dialog")).toContainText("Topic: push.order_status");
    await cancelModal(page);
    expect(await django<{ status: string }>("outbox", { id })).toMatchObject({ status: "dead", attempts: 10 });
    expect(await audit(since, "ops.")).toEqual([]);
  });

  test("confirmed from the change page: requeued, audited", async ({ page, staff }) => {
    const { id } = await django<{ id: number }>("dead-outbox");
    const since = await auditMax();
    await page.goto(`/django-admin/core/outboxmessage/${id}/change/`);
    await openAction(page, "Qayta navbatga qo‘yish");
    await confirm(page);
    await expect(page).toHaveURL(new RegExp(`/core/outboxmessage/${id}/change/$`));
    await expect(page.getByText(`Xabar #${id} qayta navbatga qo‘yildi.`)).toBeVisible();
    const row = await django<{ status: string; attempts: number }>("outbox", { id });
    expect(row.status).not.toBe("dead");
    expect(row.attempts).toBeLessThan(10);
    const rows = await audit(since, "ops.outbox_requeued");
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ entity_type: "outbox", entity_id: String(id) });
    expect((await audit(since, "outbox.requeued")).length).toBeGreaterThanOrEqual(1);
    // The button is gone now that the message is no longer dead.
    await page.reload();
    await expect(page.getByRole("link", { name: "Qayta navbatga qo‘yish" })).toHaveCount(0);
    void staff;
  });

  test("confirmed from the list row button (modal): requeued", async ({ page, staff }) => {
    void staff;
    const { id } = await django<{ id: number }>("dead-outbox");
    await page.goto(`/django-admin/core/outboxmessage/?status__exact=dead`);
    const row = page.locator("#result_list tbody tr", { has: page.locator(`a[href^="/django-admin/core/outboxmessage/${id}/change/"]`) });
    await row.getByRole("link", { name: /Qayta navbatga/ }).click();
    await expect(page.locator("#modal-content #dialog form")).toBeVisible();
    await confirm(page);
    await expect(page).toHaveURL(/\/core\/outboxmessage\/$/);
    await expect(page.getByText(`Xabar #${id} qayta navbatga qo‘yildi.`)).toBeVisible();
    expect((await django<{ status: string }>("outbox", { id })).status).not.toBe("dead");
  });

  test("a delivered message: no button, a direct request is refused", async ({ page, staff }) => {
    void staff;
    const { id } = await django<{ id: number }>("outbox-done");
    const since = await auditMax();
    await page.goto(`/django-admin/core/outboxmessage/${id}/change/`);
    await expect(page.getByRole("link", { name: "Qayta navbatga qo‘yish" })).toHaveCount(0);
    await page.goto(`/django-admin/core/outboxmessage/${id}/requeue/`);
    await expect(page.locator(".gd-ops-refusal")).toHaveText("Xabar allaqachon yetkazilgan: qayta navbatga qo‘yish kerak emas.");
    await expect(page.locator("button[name=_ops_confirm]")).toHaveCount(0);
    const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")!.value;
    const response = await page.request.post(`/django-admin/core/outboxmessage/${id}/requeue/`, {
      form: { csrfmiddlewaretoken: csrf, confirm: "True" },
      headers: { "X-CSRFToken": csrf },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(302);
    expect(await django("outbox", { id })).toMatchObject({ status: "done" });
    expect(await audit(since, "ops.")).toEqual([]);
  });

  test("other methods answer 405", async ({ page, staff, expected }) => {
    void staff;
    expected.status(405);
    const { id } = await django<{ id: number }>("dead-outbox");
    await page.goto("/django-admin/core/outboxmessage/");
    const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")!.value;
    const response = await page.request.put(`/django-admin/core/outboxmessage/${id}/requeue/`, {
      headers: { "X-CSRFToken": csrf },
      maxRedirects: 0,
    });
    expect(response.status()).toBe(405);
    expect((await django<{ status: string }>("outbox", { id })).status).toBe("dead");
  });
});

test.describe("outbox: requeue every dead message of a topic (managers only)", () => {
  test("manager: confirm with a topic → requeued, audited", async ({ page, staff }) => {
    void staff;
    const first = await django<{ id: number }>("dead-outbox", { topic: "push.order_status" });
    const second = await django<{ id: number }>("dead-outbox", { topic: "push.order_status" });
    const since = await auditMax();
    await page.goto("/django-admin/core/outboxmessage/");
    await openAction(page, "Topic bo‘yicha o‘liklarni qayta navbatga");
    await expect(page.locator("#dialog")).toContainText("Faqat menejerlar uchun.");
    await page.locator("#dialog select[name=topic]").selectOption("push.order_status");
    await confirm(page);
    await expect(page.getByText(/push\.order_status: \d+ ta xabar qayta navbatga qo‘yildi/)).toBeVisible();
    for (const { id } of [first, second]) expect((await django<{ status: string }>("outbox", { id })).status).not.toBe("dead");
    const rows = await audit(since, "ops.outbox_topic_requeued");
    expect(rows).toHaveLength(1);
    expect(rows[0].entity_id).toBe("push.order_status");
    expect(rows[0].after?.requeued).toEqual(expect.arrayContaining([first.id, second.id]));
  });

  test("non-manager: no button, and the URL is refused", async ({ browser, newStaff, expected }) => {
    expected.status(403);
    await django("dead-outbox");
    const page = await asStaff(browser, await newStaff({ manager: false }));
    await page.goto("/django-admin/core/outboxmessage/");
    await expect(page.getByRole("link", { name: "Topic bo‘yicha o‘liklarni qayta navbatga" })).toHaveCount(0);
    const response = await page.goto("/django-admin/core/outboxmessage/requeue-dead-topic/");
    expect(response?.status()).toBe(403);
    await page.context().close();
  });
});

test.describe("consistency report now (managers only)", () => {
  test("manager: queued, the report runs, audited; a second click inside the cooldown is refused", async ({ page, staff }) => {
    void staff;
    await django("cache-delete", { keys: ["ops:consistency_report"] });
    const since = await auditMax();
    await page.goto("/django-admin/core/auditentry/");
    await openAction(page, "Hisobotni hozir ishga tushirish");
    await cancelModal(page);
    expect(await audit(since, "ops.")).toEqual([]);
    await openAction(page, "Hisobotni hozir ishga tushirish");
    await confirm(page);
    await expect(page.getByText(/Muvofiqlik hisoboti navbatga qo‘yildi/)).toBeVisible();
    expect(await audit(since, "ops.consistency_report_requested")).toHaveLength(1);
    // CELERY_TASK_ALWAYS_EAGER in the e2e settings: the report itself ran and wrote its row.
    expect((await audit(since, "report.consistency")).length).toBeGreaterThanOrEqual(1);
    await openAction(page, "Hisobotni hozir ishga tushirish");
    await confirm(page);
    await expect(page.getByText(/Hisobot yaqinda ishga tushirilgan/)).toBeVisible();
    expect(await audit(since, "ops.consistency_report_requested")).toHaveLength(1);
    await django("cache-delete", { keys: ["ops:consistency_report"] });
  });

  test("non-manager: refused", async ({ browser, newStaff, expected }) => {
    expected.status(403);
    const page = await asStaff(browser, await newStaff({ manager: false }));
    await page.goto("/django-admin/core/auditentry/");
    await expect(page.getByRole("link", { name: "Hisobotni hozir ishga tushirish" })).toHaveCount(0);
    const response = await page.goto("/django-admin/core/auditentry/run-consistency-report/");
    expect(response?.status()).toBe(403);
    await page.context().close();
  });
});

for (const list of ["/django-admin/integrations/job/", "/django-admin/integrations/integrationstate/"]) {
  test.describe(`provider actions on ${list}`, () => {
    test("Billz delta sync: confirm → a job is queued and audited", async ({ page, staff }) => {
      void staff;
      const since = await auditMax();
      const jobsBefore = (await django<{ id: number }>("job-max")).id;
      await page.goto(list);
      await openAction(page, "Billz sinxronlashni ishga tushirish");
      await cancelModal(page);
      expect((await django<{ id: number }>("job-max")).id).toBe(jobsBefore);
      await openAction(page, "Billz sinxronlashni ishga tushirish");
      await confirm(page);
      await expect(page.getByText(/Billz sinxronlash navbatga qo‘yildi \(vazifa #\d+\)\./)).toBeVisible();
      const jobs = await django<{ id: number; kind: string; state: string }[]>("jobs", { after_id: jobsBefore });
      expect(jobs).toHaveLength(1);
      expect(jobs[0].kind).toBe("billz_import");
      // eager in the e2e settings: the fake-mode import ran to an end state, never a crash
      expect(["queued", "running", "succeeded", "done"]).toContain(jobs[0].state);
      const rows = await audit(since, "ops.billz_sync_requested");
      expect(rows).toHaveLength(1);
      expect(rows[0].entity_id).toBe(String(jobs[0].id));
    });

    test("BTS poll: confirm → queued and audited; a second request inside the cooldown is refused", async ({ page, staff }) => {
      void staff;
      await django("cache-delete", { keys: ["ops:bts_poll"] });
      const since = await auditMax();
      await page.goto(list);
      await openAction(page, "BTS holatini so‘rash");
      await confirm(page);
      await expect(page.getByText("BTS holat so‘rovi navbatga qo‘yildi.")).toBeVisible();
      expect(await audit(since, "ops.bts_poll_requested")).toHaveLength(1);
      await openAction(page, "BTS holatini so‘rash");
      await confirm(page);
      await expect(page.getByText(/BTS holati yaqinda so‘ralgan/)).toBeVisible();
      expect(await audit(since, "ops.bts_poll_requested")).toHaveLength(1);
      await django("cache-delete", { keys: ["ops:bts_poll"] });
    });
  });
}

test.describe("customer sessions", () => {
  test("revoke one session from its change page: cancel, then confirm", async ({ page, staff }) => {
    void staff;
    const { customer_id, session_ids } = await django<{ customer_id: number; session_ids: string[] }>("customer-sessions", { count: 2 });
    const since = await auditMax();
    await page.goto(`/django-admin/accounts/authsession/${session_ids[0]}/change/`);
    await openAction(page, "Seansni tugatish");
    await cancelModal(page);
    expect((await django<{ revoked_at: string | null }[]>("auth-sessions", { ids: session_ids })).every((row) => row.revoked_at === null)).toBe(true);
    await openAction(page, "Seansni tugatish");
    await confirm(page);
    await expect(page.getByText("Seans tugatildi.")).toBeVisible();
    const rows = await django<{ id: string; revoked_at: string | null; revoke_reason: string }[]>("auth-sessions", { ids: session_ids });
    const revoked = rows.find((row) => row.id === session_ids[0])!;
    const other = rows.find((row) => row.id === session_ids[1])!;
    expect(revoked.revoked_at).not.toBeNull();
    expect(revoked.revoke_reason).toBe("admin_revoked");
    expect(other.revoked_at).toBeNull();
    const audits = await audit(since, "ops.customer_session_revoked");
    expect(audits).toHaveLength(1);
    expect(audits[0]).toMatchObject({ entity_type: "customer", entity_id: String(customer_id) });
    await page.reload();
    await expect(page.getByRole("link", { name: "Seansni tugatish" })).toHaveCount(0);
  });

  test("revoke one session from the list row button", async ({ page, staff }) => {
    void staff;
    const { session_ids } = await django<{ session_ids: string[] }>("customer-sessions", { count: 1 });
    await page.goto(`/django-admin/accounts/authsession/?q=${session_ids[0]}`);
    await page.locator("#result_list tbody tr").first().getByRole("link", { name: /Tugatish/ }).click();
    await expect(page.locator("#modal-content #dialog form")).toBeVisible();
    await confirm(page);
    await expect(page.getByText("Seans tugatildi.")).toBeVisible();
    const [row] = await django<{ revoked_at: string | null }[]>("auth-sessions", { ids: session_ids });
    expect(row.revoked_at).not.toBeNull();
  });

  test("a staff (panel) session cannot be revoked here", async ({ page, staff, newStaff }) => {
    void staff;
    const user = await newStaff();
    const { id } = await django<{ id: string }>("panel-session", { email: user.email });
    await page.goto(`/django-admin/accounts/authsession/${id}/change/`);
    await expect(page.getByRole("link", { name: "Seansni tugatish" })).toHaveCount(0);
    await page.goto(`/django-admin/accounts/authsession/${id}/revoke/`);
    await expect(page.locator(".gd-ops-refusal")).toHaveText("Xodim seanslari bu yerda tugatilmaydi.");
  });

  test("revoke every session of a customer: cancel, then confirm", async ({ page, staff }) => {
    void staff;
    const { customer_id, session_ids } = await django<{ customer_id: number; session_ids: string[] }>("customer-sessions", { count: 3 });
    const since = await auditMax();
    await page.goto(`/django-admin/accounts/customer/${customer_id}/change/`);
    await openAction(page, "Barcha seanslarni tugatish");
    await cancelModal(page);
    expect((await django<{ revoked_at: string | null }[]>("auth-sessions", { ids: session_ids })).every((row) => row.revoked_at === null)).toBe(true);
    await openAction(page, "Barcha seanslarni tugatish");
    await confirm(page);
    await expect(page.getByText("3 ta seans tugatildi.")).toBeVisible();
    const rows = await django<{ revoked_at: string | null; revoke_reason: string }[]>("auth-sessions", { ids: session_ids });
    expect(rows.every((row) => row.revoked_at !== null && row.revoke_reason === "admin_revoked")).toBe(true);
    const audits = await audit(since, "ops.customer_sessions_revoked");
    expect(audits).toHaveLength(1);
    expect(audits[0].after).toMatchObject({ count: 3 });
    await page.reload();
    await expect(page.getByRole("link", { name: "Barcha seanslarni tugatish" })).toHaveCount(0);
  });
});

test.describe("staff 2FA reset (managers only)", () => {
  async function selectAndRun(page: Page, email: string): Promise<void> {
    await page.goto(`/django-admin/accounts/staffuser/?q=${encodeURIComponent(email)}`);
    await page.locator("#result_list tbody tr").first().locator("input.action-select").check();
    await page.locator("select[name=action]").selectOption("reset_two_factor");
    await page.locator("#changelist-actions button[type=submit], button[name=index]").first().click();
    await page.waitForLoadState("domcontentloaded");
  }

  test("manager: confirm page lists the target, cancel changes nothing, confirm resets and unverifies", async ({ page, staff, newStaff, signIn, browser }) => {
    void staff;
    const target = await newStaff();
    // the target is signed in elsewhere with a verified session
    const other = await browser.newContext({ baseURL: MAIN_URL });
    await signIn(other, target);
    const otherPage = await other.newPage();
    await otherPage.goto("/django-admin/core/outboxmessage/");
    await expect(otherPage).toHaveURL(/outboxmessage\/$/);

    const since = await auditMax();
    await selectAndRun(page, target.email);
    await expect(page.locator("#reset-2fa-targets")).toContainText(target.email);
    await page.getByRole("link", { name: "Bekor qilish" }).click();
    await expect(page).toHaveURL(/\/accounts\/staffuser\/$/);
    expect((await django<{ confirmed: number }>("two-factor", { email: target.email })).confirmed).toBe(1);

    await selectAndRun(page, target.email);
    await page.getByRole("button", { name: "Ha, 2FA ni qayta o‘rnatish" }).click();
    await page.waitForLoadState("domcontentloaded");
    await expect(page.getByText(/1 ta xodimning 2FA qurilmasi o‘chirildi/)).toBeVisible();
    expect(await django("two-factor", { email: target.email })).toEqual({ confirmed: 0, unconfirmed: 0, recovery_codes: 0 });
    const rows = await audit(since, "ops_access.2fa_reset");
    expect(rows).toHaveLength(1);
    expect(rows[0].entity_id).toBe(String(target.id));
    // Their open console session stops being verified at once.
    await otherPage.goto("/django-admin/core/outboxmessage/");
    await expect(otherPage).toHaveURL(/\/django-admin\/login\//);
    await other.close();
  });

  test("manager: one's own 2FA cannot be reset here", async ({ page, staff }) => {
    await selectAndRun(page, staff.email);
    await expect(page.getByText("O‘z 2FA ingizni bu yerdan qayta o‘rnatib bo‘lmaydi.")).toBeVisible();
    expect((await django<{ confirmed: number }>("two-factor", { email: staff.email })).confirmed).toBe(1);
  });

  test("non-manager: the action is not offered and a forged POST does nothing", async ({ browser, newStaff }) => {
    const target = await newStaff();
    const page = await asStaff(browser, await newStaff({ manager: false }));
    await page.goto("/django-admin/accounts/staffuser/");
    const options = await page.locator("select[name=action] option").evaluateAll((items) => items.map((item) => (item as HTMLOptionElement).value));
    expect(options).not.toContain("reset_two_factor");
    const csrf = (await page.context().cookies()).find((cookie) => cookie.name === "csrftoken")!.value;
    await page.request.post("/django-admin/accounts/staffuser/", {
      form: { csrfmiddlewaretoken: csrf, action: "reset_two_factor", _selected_action: String(target.id), post: "yes", index: "0" },
      headers: { "X-CSRFToken": csrf, Referer: `${MAIN_URL}/django-admin/accounts/staffuser/` },
      maxRedirects: 0,
    });
    expect((await django<{ confirmed: number }>("two-factor", { email: target.email })).confirmed).toBe(1);
    await page.context().close();
  });
});
