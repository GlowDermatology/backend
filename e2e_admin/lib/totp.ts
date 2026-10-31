/**
 * RFC 6238 TOTP (HMAC-SHA1, 30 s steps, 6 digits) with Node's crypto: the codes an authenticator app shows
 * for a staff member's secret. The console (django-otp) accepts the current step ±1 and never the same step
 * twice for one device, so `freshCode` remembers the steps this process used per key and moves to the next
 * acceptable one (waiting for the clock only when both are spent).
 */
import { createHmac } from "node:crypto";

const STEP = 30;
const DIGITS = 6;
const B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

export function base32ToHex(text: string): string {
  const clean = text.replace(/[\s=]/g, "").toUpperCase();
  let bits = "";
  for (const char of clean) {
    const value = B32.indexOf(char);
    if (value < 0) throw new Error(`not base32: ${char}`);
    bits += value.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let index = 0; index + 8 <= bits.length; index += 8) {
    bytes.push(Number.parseInt(bits.slice(index, index + 8), 2));
  }
  return Buffer.from(bytes).toString("hex");
}

/** The code of `keyHex` for time step `counter` (RFC 4226 HOTP, dynamic truncation). */
export function hotp(keyHex: string, counter: number): string {
  const message = Buffer.alloc(8);
  message.writeBigUInt64BE(BigInt(counter));
  const digest = createHmac("sha1", Buffer.from(keyHex, "hex")).update(message).digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const binary = (digest.readUInt32BE(offset) & 0x7fffffff) % 10 ** DIGITS;
  return binary.toString().padStart(DIGITS, "0");
}

export function currentStep(now = Date.now()): number {
  return Math.floor(now / 1000 / STEP);
}

export function totp(keyHex: string, now = Date.now()): string {
  return hotp(keyHex, currentStep(now));
}

const used = new Map<string, number>();

/** A code the console accepts now for `keyHex` and has not seen from this process yet. */
export async function freshCode(keyHex: string): Promise<string> {
  for (;;) {
    const step = currentStep();
    const last = used.get(keyHex) ?? -1;
    const candidate = Math.max(step, last + 1);
    if (candidate <= step + 1) {
      used.set(keyHex, candidate);
      return hotp(keyHex, candidate);
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}
