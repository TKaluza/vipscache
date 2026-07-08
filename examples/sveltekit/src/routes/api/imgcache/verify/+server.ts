import { env } from "$env/dynamic/private";
import { verifySignedUrl } from "@imgcache/client";
import type { RequestHandler } from "./$types";

export const GET: RequestHandler = ({ request }) => {
  const forwardedUri = request.headers.get("x-forwarded-uri");
  const secret = env.IMGCACHE_URL_SIGNING_SECRET;
  if (!forwardedUri || !secret) return new Response(null, { status: 403 });

  const previousSecret = env.IMGCACHE_URL_SIGNING_SECRET_PREVIOUS;
  const result = verifySignedUrl(
    forwardedUri,
    previousSecret ? { secret, previousSecret } : { secret }
  );

  return new Response(null, { status: result.ok ? 204 : 403 });
};
