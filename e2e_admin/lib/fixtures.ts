/**
 * Shared fixtures of the ops console suite.
 *
 * - `guard` (automatic): every `console.error`, uncaught page error, failed request and HTTP >= 400 answer of
 *   a same-origin request fails the test. A test that expects one (a 404 page, a 429 lockout) declares it
 *   first with `expected.status(code, /url/)` or `expected.console(/text/)`.
 * - `staff`: a manager staff member of this worker with a verified console session (the cookie is set on
 *   test's context); `signIn(context, user)` gives another user a session the same way.
 * - `newStaff(options)`: a fresh staff member (known TOTP secret unless `totp: false`).
 */
import { test as base, expect, type BrowserContext, type Page, type Response } from "@playwright/test";

import { django, type Target } from "./django";

export { expect };

export const MAIN_URL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:8770";
export const GATE_URL = process.env.E2E_GATE_URL ?? "http://127.0.0.1:8771";

export interface StaffMember {
  id: number;
  email: string;
  password: string;
  key_hex?: string;
  key_base32?: string;
  recovery_codes?: string[];
}

export interface StaffOptions {
  manager?: boolean;
  staff?: boolean;
  active?: boolean;
  totp?: boolean;
  superuser?: boolean;
  recovery?: boolean;
  email?: string;
  full_name?: string;
}

interface Expectations {
  statuses: { code: number; url?: RegExp }[];
  texts: RegExp[];
  status(code: number, url?: RegExp): void;
  console(text: RegExp): void;
}

interface Problem {
  kind: string;
  text: string;
}

type Fixtures = {
  expected: Expectations;
  guard: Problem[];
  staff: StaffMember;
  newStaff: (options?: StaffOptions, target?: Target) => Promise<StaffMember>;
  signIn: (context: BrowserContext, user: StaffMember, target?: Target) => Promise<string>;
};

type WorkerFixtures = {
  workerStaff: StaffMember;
  workerSession: string;
};

const sameOrigin = (url: string): boolean => url.startsWith(MAIN_URL) || url.startsWith(GATE_URL);

export async function addSession(context: BrowserContext, key: string, target: Target = "main"): Promise<void> {
  const url = target === "main" ? MAIN_URL : GATE_URL;
  await context.addCookies([{ name: "sessionid", value: key, url }]);
}

export const test = base.extend<Fixtures, WorkerFixtures>({
  workerStaff: [
    async ({}, use) => {
      await use(await django<StaffMember>("staff", { manager: true, full_name: "E2E Menejer" }));
    },
    { scope: "worker" },
  ],
  workerSession: [
    async ({ workerStaff }, use) => {
      const { session_key } = await django<{ session_key: string }>("session", { email: workerStaff.email });
      await use(session_key);
    },
    { scope: "worker" },
  ],

  expected: async ({}, use) => {
    const statuses: { code: number; url?: RegExp }[] = [];
    const texts: RegExp[] = [];
    await use({
      statuses,
      texts,
      status: (code, url) => statuses.push({ code, url }),
      console: (text) => texts.push(text),
    });
  },

  guard: [
    async ({ page, expected }, use) => {
      const problems: Problem[] = [];
      page.on("console", (message) => {
        if (message.type() !== "error") return;
        const text = `${message.text()} @ ${message.location().url}`;
        if (expected.texts.some((pattern) => pattern.test(text))) return;
        // The browser's own line for an answer the response hook below already judged.
        if (/Failed to load resource: the server responded with a status of (\d+)/.test(message.text())) {
          const code = Number(/status of (\d+)/.exec(message.text())?.[1]);
          const url = message.location().url;
          if (expected.statuses.some((entry) => entry.code === code && (!entry.url || entry.url.test(url)))) return;
        }
        problems.push({ kind: "console.error", text });
      });
      page.on("pageerror", (error) => problems.push({ kind: "pageerror", text: `${error.name}: ${error.message}` }));
      page.on("requestfailed", (request) => {
        if (!sameOrigin(request.url())) return;
        const failure = request.failure()?.errorText ?? "";
        // A navigation that replaced the page while a request was in flight (not a server problem).
        if (failure === "net::ERR_ABORTED") return;
        problems.push({ kind: "requestfailed", text: `${request.method()} ${request.url()}: ${failure}` });
      });
      page.on("response", (response: Response) => {
        if (!sameOrigin(response.url()) || response.status() < 400) return;
        const code = response.status();
        if (expected.statuses.some((entry) => entry.code === code && (!entry.url || entry.url.test(response.url())))) {
          return;
        }
        problems.push({ kind: `HTTP ${code}`, text: `${response.request().method()} ${response.url()}` });
      });
      await use(problems);
      expect(problems, "browser errors, failed requests or HTTP >= 400 answers during the test").toEqual([]);
    },
    { auto: true },
  ],

  staff: async ({ context, workerStaff, workerSession }, use) => {
    await addSession(context, workerSession);
    await use(workerStaff);
  },

  newStaff: async ({}, use) => {
    await use((options = {}, target = "main") => django<StaffMember>("staff", options, target));
  },

  signIn: async ({}, use) => {
    await use(async (context, user, target = "main") => {
      const { session_key } = await django<{ session_key: string }>("session", { email: user.email }, target);
      await addSession(context, session_key, target);
      return session_key;
    });
  },
});

/** Read the "N ta natija" count of a changelist header. */
export async function resultCount(page: Page): Promise<number> {
  const text = await page.locator("h1").filter({ hasText: "natija" }).first().innerText();
  const match = /([\d\s\u00a0\u202f]+)\s*ta natija/.exec(text);
  if (!match) throw new Error(`no result count in: ${text}`);
  return Number(match[1].replace(/[\s\u00a0\u202f]/g, ""));
}

/** The sidebar entries that carry a live badge (config/settings/ops_admin.py), by their changelist URL. */
export const SIDEBAR_BADGES = {
  "/django-admin/core/outboxmessage/": "dead_outbox",
  "/django-admin/integrations/webhookinbox/": "failed_webhooks",
  "/django-admin/payments/refund/": "refunds_needing_action",
} as const;

/** A sidebar navigation entry (the icon + title link, not the "all applications" list). */
export function sidebarItem(page: Page, href: string) {
  return page.locator(`a[href="${href}"]:has(span.material-symbols-outlined)`).first();
}
