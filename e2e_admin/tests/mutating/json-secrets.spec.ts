/**
 * Secrets in JSON documents (glowderma.core.jsonmask): outbox payloads that carry credentials on purpose —
 * `apple.revoke`'s sealed Apple refresh token, `push.account_security`'s farewell FCM tokens — show the value
 * of every secret-like key as «yashirilgan» on the change page and in the list, while ids stay readable. The
 * messages are dead (a delivered message has its token blanked by the handler), as staff find them before a
 * requeue. In the mutating project: the messages it creates would change the Ops dashboard's outbox numbers while the
 * read-only specs compare them.
 */
import { django } from "../../lib/django";
import { expect, test } from "../../lib/fixtures";

const MASK = "«yashirilgan»";

test("outbox payloads with credentials are masked on the change page and the list", async ({ page, staff }) => {
  void staff;
  const created = await django<{ customer_id: number; messages: { id: number; topic: string }[]; secrets: string[] }>(
    "outbox-secrets",
  );
  expect(created.messages.map((message) => message.topic).sort()).toEqual(["apple.revoke", "push.account_security"]);
  for (const message of created.messages) {
    await test.step(message.topic, async () => {
      const response = await page.goto(`/django-admin/core/outboxmessage/${message.id}/change/`);
      expect(response?.status()).toBe(200);
      const payload = page.locator("pre.gd-json").first();
      await expect(payload).toBeVisible();
      await expect(payload).toContainText(MASK);
      await expect(payload).toContainText(`"customer_id": ${created.customer_id}`);
      const key = message.topic === "apple.revoke" ? "sealed_token" : "farewell_tokens";
      await expect(payload).toContainText(`"${key}": "${MASK}"`);
      const html = await page.content();
      for (const secret of created.secrets) expect(html, `${message.topic} shows a credential`).not.toContain(secret);
    });
  }
  for (const topic of ["apple.revoke", "push.account_security"]) {
    await page.goto(`/django-admin/core/outboxmessage/?topic__exact=${topic}`);
    await expect(page.locator(`#result_list a[href^="/django-admin/core/outboxmessage/"]`).first()).toBeVisible();
    const html = await page.content();
    for (const secret of created.secrets) expect(html, `the ${topic} list shows a credential`).not.toContain(secret);
  }
});
