import { env } from "$env/dynamic/private";
import { ImgCache, signUrl, type ImageSpec } from "@imgcache/client";

/**
 * Singleton ImgCache instance — server-only module.
 *
 * SvelteKit enforces server-only access via the `$lib/server` path prefix:
 * importing this from a client-side file is a build-time error.
 *
 * zeromq holds long-lived TCP sockets; do not recreate per request.
 * The singleton is lazily initialised on first use.
 */

let _cache: ImgCache | null = null;

export function getCache(): ImgCache {
  if (!_cache) {
    const endpoint = env.IMGCACHE_ZMQ_ENDPOINT ?? "tcp://127.0.0.1:5555";
    _cache = new ImgCache({
      endpoint,
      timeoutMs: 300_000,
      retries: 2,
    });
  }
  return _cache;
}

function signingSecret(): string {
  const secret = env.IMGCACHE_URL_SIGNING_SECRET;
  if (!secret) throw new Error("IMGCACHE_URL_SIGNING_SECRET is required");
  return secret;
}

function baseUrl(): string {
  return (
    env.IMGCACHE_PUBLIC_BASE_URL ??
    env.IMGCACHE_BASE_URL ??
    "http://localhost:80"
  );
}

/**
 * Resolve a spec via ZMQ and return a signed URL in one call.
 *
 * The worker materialises the derivative (cache miss) or returns the
 * cached relpath (hit). We then HMAC-sign the URL for Caddy's
 * forward_auth verifier.
 */
export async function imageUrl(spec: ImageSpec): Promise<string> {
  const { relpath } = await spec.resolve();
  return signUrl(relpath, {
    secret: signingSecret(),
    baseUrl: baseUrl(),
  });
}
