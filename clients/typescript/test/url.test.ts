import { describe, expect, it } from "vitest";
import { publicUrl, signUrl, verifySignedUrl, verifyUrl } from "../src/index.js";

const baseUrl = "https://images.example.test";
const relpath = "leaves/e7/e7f5da4b27b8c3270169618b71ccfb07.webp";
const secret = "0123456789abcdef0123456789abcdef";
const previousSecret = "prev-0123456789abcdef0123456789abcdef";
const now = Date.UTC(2026, 0, 1, 0, 0, 0);

describe("signed imgcache URLs", () => {
  it("signs and verifies a leaf URL round trip", () => {
    const url = signUrl(relpath, { baseUrl, secret, now });
    expect(url).toMatch(/^https:\/\/images\.example\.test\/cache\/leaves\/e7\//);
    const parsed = new URL(url);
    expect(parsed.searchParams.get("exp")).toBe(String(Math.floor(now / 1000) + 3600));
    expect(parsed.searchParams.get("sig")).toMatch(/^[0-9a-f]{64}$/);
    expect(verifySignedUrl(parsed.pathname + parsed.search, { secret, now })).toEqual({
      ok: true,
      path: parsed.pathname,
      exp: Math.floor(now / 1000) + 3600
    });
  });

  it("rejects tampered path, exp, and sig", () => {
    const url = new URL(signUrl(relpath, { baseUrl, secret, ttlSeconds: 120, now }));
    expect(verifyUrl(url.pathname.replace("/e7/", "/e8/") + url.search, { secret, now }).ok).toBe(false);

    const tamperedExp = new URL(url);
    tamperedExp.searchParams.set("exp", String(Number(tamperedExp.searchParams.get("exp")) + 1));
    expect(verifySignedUrl(tamperedExp.pathname + tamperedExp.search, { secret, now })).toMatchObject({ ok: false, reason: "bad_sig" });

    const tamperedSig = new URL(url);
    const originalSig = tamperedSig.searchParams.get("sig")!;
    const last = originalSig.at(-1) === "0" ? "1" : "0";
    tamperedSig.searchParams.set("sig", `${originalSig.slice(0, 63)}${last}`);
    expect(verifySignedUrl(tamperedSig.pathname + tamperedSig.search, { secret, now })).toMatchObject({ ok: false, reason: "bad_sig" });
  });

  it("rejects expired links", () => {
    const url = new URL(signUrl(relpath, { baseUrl, secret, ttlSeconds: 10, now }));
    expect(verifySignedUrl(url.pathname + url.search, { secret, now: now + 10_000 })).toMatchObject({ ok: false, reason: "expired" });
  });

  it("accepts previous secret during rotation", () => {
    const old = new URL(signUrl(relpath, { baseUrl, secret: previousSecret, now }));
    expect(verifySignedUrl(old.pathname + old.search, { secret, previousSecret, now }).ok).toBe(true);
    expect(verifySignedUrl(old.pathname + old.search, { secret, now })).toMatchObject({ ok: false, reason: "bad_sig" });
  });

  it("returns full unsigned public pinned URLs", () => {
    expect(publicUrl("pinned/ab/abcdef.webp", { baseUrl })).toBe("https://images.example.test/cache/pinned/ab/abcdef.webp");
  });
});
