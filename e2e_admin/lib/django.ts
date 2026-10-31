/**
 * `manage.py e2e_console <action> --data <json>` from Node: per-test data setup and database checks against
 * the e2e databases (the same ones the servers use). The command prints its result as the last
 * `E2E_JSON:` line of stdout.
 */
import { execFile, execFileSync } from "node:child_process";
import path from "node:path";
import { promisify } from "node:util";

const run = promisify(execFile);

export const ROOT = path.resolve(__dirname, "..", "..");
const PYTHON = process.env.E2E_PYTHON ?? path.join(ROOT, ".venv", "bin", "python");
const PG = process.env.E2E_PG_URL ?? "postgres://glowderma:glowderma@localhost:5433";
const REDIS = process.env.E2E_REDIS_URL ?? "redis://localhost:6380";

export type Target = "main" | "gate";

/** The environment of one server (serve.sh): its database and Redis databases. */
export function serverEnv(target: Target): NodeJS.ProcessEnv {
  const main = target === "main";
  return {
    ...process.env,
    DJANGO_SETTINGS_MODULE: "config.settings.e2e",
    DATABASE_URL: `${PG}/${main ? "glowderma_e2e" : "glowderma_e2e_empty"}`,
    REDIS_URL: `${REDIS}/${main ? 9 : 11}`,
    CELERY_BROKER_URL: `${REDIS}/${main ? 10 : 12}`,
    LOG_LEVEL: "ERROR",
  };
}

function parse<T>(stdout: string, action: string): T {
  const line = stdout
    .split("\n")
    .reverse()
    .find((entry) => entry.startsWith("E2E_JSON:"));
  if (line === undefined) {
    throw new Error(`e2e_console ${action}: no result line in output:\n${stdout.slice(-2000)}`);
  }
  return JSON.parse(line.slice("E2E_JSON:".length)) as T;
}

function args(action: string, data: object): string[] {
  return ["manage.py", "e2e_console", action, "--data", JSON.stringify(data)];
}

/** Run one e2e_console action and return its JSON result. */
export async function django<T = unknown>(action: string, data: object = {}, target: Target = "main"): Promise<T> {
  try {
    const { stdout } = await run(PYTHON, args(action, data), {
      cwd: ROOT,
      env: serverEnv(target),
      maxBuffer: 64 * 1024 * 1024,
    });
    return parse<T>(stdout, action);
  } catch (error) {
    const failure = error as { stderr?: string; message: string };
    throw new Error(`e2e_console ${action} failed: ${failure.stderr?.slice(-3000) ?? failure.message}`);
  }
}

/** The synchronous form, for test collection (the model registry needs no database). */
export function djangoSync<T = unknown>(action: string, data: object = {}, target: Target = "main"): T {
  const stdout = execFileSync(PYTHON, args(action, data), {
    cwd: ROOT,
    env: serverEnv(target),
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
  return parse<T>(stdout, action);
}
