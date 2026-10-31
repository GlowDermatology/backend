import { defineConfig, devices } from "@playwright/test";

const MAIN_PORT = Number(process.env.E2E_PORT ?? 8770);
const GATE_PORT = Number(process.env.E2E_GATE_PORT ?? 8771);
const CI = Boolean(process.env.CI);

/**
 * The ops console (Django admin on django-unfold) in a real Chromium, against two Django servers started by
 * `scripts/serve.sh` (README.md): `main` on a freshly migrated and seeded `glowderma_e2e`, and `gate` on an
 * empty copy behind a network allow-list that excludes the browser.
 *
 * Projects: `console` runs the read-mostly specs in parallel; `mutating` (actions, editable professionals,
 * badge changes) runs after it, one test at a time, so the dashboards' and lists' counts stay stable while
 * they are compared.
 */
export default defineConfig({
  testDir: "./tests",
  outputDir: "./test-results",
  fullyParallel: true,
  forbidOnly: CI,
  retries: 0,
  workers: Number(process.env.E2E_WORKERS ?? 3),
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  use: {
    baseURL: `http://127.0.0.1:${MAIN_PORT}`,
    locale: "uz-UZ",
    timezoneId: "Asia/Tashkent",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: "console", testIgnore: /mutating\// },
    { name: "mutating", testMatch: /mutating\/.*\.spec\.ts/, dependencies: ["console"], workers: 1 },
  ],
  webServer: [
    {
      command: "scripts/serve.sh main",
      url: `http://127.0.0.1:${MAIN_PORT}/api/v1/health/`,
      timeout: 600_000,
      reuseExistingServer: !CI && !process.env.E2E_FRESH,
      stdout: "pipe",
      stderr: "pipe",
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
    {
      command: "scripts/serve.sh gate",
      url: `http://127.0.0.1:${GATE_PORT}/api/v1/health/`,
      timeout: 600_000,
      reuseExistingServer: !CI && !process.env.E2E_FRESH,
      stdout: "pipe",
      stderr: "pipe",
      gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
    },
  ],
});
