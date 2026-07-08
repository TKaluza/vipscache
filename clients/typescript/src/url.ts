import { createHmac, timingSafeEqual } from "node:crypto";
import type { ImgCache } from "./client.js";
import type { ImageSpec } from "./spec.js";
import type { ImageSpecPayload, MaterializeResult } from "./types.js";

export const DEFAULT_SIGNED_URL_TTL_SECONDS = 3600;

export interface PublicUrlOptions {
  /** Public image origin, for example https://images.example.com. Defaults to IMGCACHE_PUBLIC_BASE_URL/IMGCACHE_BASE_URL. */
  baseUrl?: string | URL;
}

export interface SignUrlOptions extends PublicUrlOptions {
  secret: string | Buffer | Uint8Array;
  ttlSeconds?: number;
  now?: Date | number;
}

export interface VerifyUrlOptions {
  secret: string | Buffer | Uint8Array;
  previousSecret?: string | Buffer | Uint8Array;
  now?: Date | number;
}

export interface VerifyUrlResult {
  ok: boolean;
  reason?: "missing" | "expired" | "bad_exp" | "bad_sig" | "bad_path";
  path?: string;
  exp?: number;
}

export interface PublishUrlOptions extends PublicUrlOptions {}

/**
 * Return a full, unsigned public URL for an already-public imgcache relpath.
 * Use this for explicit pinned/public materializations under cache/pinned/.
 */
export function publicUrl(relpath: string, opts: PublicUrlOptions = {}): string {
  return new URL(toPublicPath(relpath), requiredBaseUrl(opts.baseUrl)).toString();
}

/**
 * Return a full signed URL for a cache/leaves relpath.
 * Signature covers only path + "\\n" + exp so future query params do not invalidate URLs.
 */
export function signUrl(relpath: string, opts: SignUrlOptions): string {
  const path = toPublicPath(relpath);
  if (!path.startsWith("/cache/leaves/")) throw new Error(`signUrl expects a cache/leaves relpath, got ${relpath}`);
  const exp = Math.floor(toMillis(opts.now ?? Date.now()) / 1000) + Math.trunc(opts.ttlSeconds ?? DEFAULT_SIGNED_URL_TTL_SECONDS);
  const url = new URL(path, requiredBaseUrl(opts.baseUrl));
  url.searchParams.set("exp", String(exp));
  url.searchParams.set("sig", signatureHex(opts.secret, path, exp));
  return url.toString();
}

/**
 * Verify a signed imgcache leaf URL or forwarded URI using constant-time signature comparison.
 * Accepts the active secret and, during rotation windows, an optional previous secret.
 */
export function verifySignedUrl(input: string | URL, opts: VerifyUrlOptions): VerifyUrlResult {
  const url = parseUrlLike(input);
  const path = url.pathname;
  if (!path.startsWith("/cache/leaves/")) return { ok: false, reason: "bad_path", path };
  const expText = url.searchParams.get("exp");
  const sig = url.searchParams.get("sig");
  if (!expText || !sig) return { ok: false, reason: "missing", path };
  if (!/^\d+$/.test(expText)) return { ok: false, reason: "bad_exp", path };
  const exp = Number(expText);
  if (!Number.isSafeInteger(exp)) return { ok: false, reason: "bad_exp", path };
  const nowSeconds = Math.floor(toMillis(opts.now ?? Date.now()) / 1000);
  if (exp <= nowSeconds) return { ok: false, reason: "expired", path, exp };
  const secrets = opts.previousSecret === undefined ? [opts.secret] : [opts.secret, opts.previousSecret];
  for (const secret of secrets) {
    if (constantTimeHexEqual(sig, signatureHex(secret, path, exp))) return { ok: true, path, exp };
  }
  return { ok: false, reason: "bad_sig", path, exp };
}

/** Alias with a shorter name for verifier routes. */
export const verifyUrl = verifySignedUrl;

/**
 * Explicitly materialize a spec for the public tier and return its unsigned public URL.
 * If the worker returns a leaves relpath, the URL is mapped to cache/pinned/ as the public tier path.
 */
export async function publishUrl(cache: ImgCache, spec: ImageSpec, opts: PublishUrlOptions = {}): Promise<string> {
  const result = await materializePinned(cache, spec);
  return publicUrl(toPinnedRelpath(result.relpath), opts);
}

function signatureHex(secret: string | Buffer | Uint8Array, path: string, exp: number): string {
  return createHmac("sha256", secret).update(path).update("\n").update(String(exp)).digest("hex");
}

function constantTimeHexEqual(actualHex: string, expectedHex: string): boolean {
  if (!/^[0-9a-fA-F]+$/.test(actualHex)) return false;
  const actual = Buffer.from(actualHex, "hex");
  const expected = Buffer.from(expectedHex, "hex");
  if (actual.length !== expected.length) return false;
  return timingSafeEqual(actual, expected);
}

function parseUrlLike(input: string | URL): URL {
  if (input instanceof URL) return input;
  return new URL(input, "http://imgcache.local");
}

function requiredBaseUrl(baseUrl: string | URL | undefined): string | URL {
  const resolved = baseUrl ?? process.env.IMGCACHE_PUBLIC_BASE_URL ?? process.env.IMGCACHE_BASE_URL;
  if (!resolved) throw new Error("imgcache public base URL is required (baseUrl or IMGCACHE_PUBLIC_BASE_URL)");
  return resolved;
}

function toPublicPath(relpath: string): string {
  const trimmed = relpath.trim();
  const withoutLeadingSlash = trimmed.replace(/^\/+/, "");
  const withoutCache = withoutLeadingSlash.startsWith("cache/") ? withoutLeadingSlash.slice("cache/".length) : withoutLeadingSlash;
  return `/cache/${withoutCache}`;
}

function toPinnedRelpath(relpath: string): string {
  const normalized = relpath.replace(/^\/+/, "");
  if (normalized.startsWith("pinned/")) return normalized;
  if (normalized.startsWith("cache/pinned/")) return normalized.slice("cache/".length);
  if (normalized.startsWith("leaves/")) return normalized.replace(/^leaves\//, "pinned/");
  if (normalized.startsWith("cache/leaves/")) return normalized.slice("cache/".length).replace(/^leaves\//, "pinned/");
  return normalized;
}

async function materializePinned(cache: ImgCache, spec: ImageSpec): Promise<MaterializeResult> {
  const payload = spec.toPayload();
  const pinnedPayload: ImageSpecPayload = {
    encode: payload.encode ? { ...payload.encode, params: { ...payload.encode.params } } : null,
    operations: payload.operations.map((operation, index, operations) => ({
      materialize: index === operations.length - 1 ? "pin" : operation.materialize,
      name: operation.name,
      params: { ...operation.params }
    })),
    source: { file_id: payload.source.file_id, metadata: { ...payload.source.metadata }, mime: payload.source.mime }
  };
  return cache.specFromPayload(pinnedPayload).materialize();
}

function toMillis(value: Date | number): number {
  return value instanceof Date ? value.getTime() : value;
}
