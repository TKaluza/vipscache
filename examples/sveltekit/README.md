# imgcache SvelteKit Reference App

Minimal SvelteKit application demonstrating the intended usage pattern for
imgcache web delivery.

## Architecture

```text
Browser ──HTTPS──▶ Caddy ──file_server──▶ shared volume (read-only)
   │                                       
   │ signed URL: /cache/leaves/<key>.webp?exp=…&sig=…
   │                                       
SvelteKit server (Node) ──ZMQ REQ/REP──▶ imgcache worker
   │ resolves specs, signs URLs             
   │ $lib/server/imgcache.ts (singleton)    
```

- **Control plane**: SvelteKit server ↔ ZMQ worker (trusted network).
- **Data plane**: Browser → Caddy → shared volume (read-only, signed URLs).
- The worker is the only cache writer; Caddy mounts the volume `:ro`.

## Key files

| File | Purpose |
|---|---|
| `src/lib/server/imgcache.ts` | Singleton `ImgCache` instance + `imageUrl(spec)` resolve-and-sign helper. Server-only via `$lib/server`. |
| `src/routes/+page.server.ts` | Load function: opens a PDF, resolves 3 pages at 2–3 widths (srcset pattern). |
| `src/routes/+page.svelte` | Renders `<img>` with `src` / `srcset` / `sizes`. |
| `src/routes/api/imgcache/verify/+server.ts` | Caddy `forward_auth` target: validates `sig` + `exp`, returns 204/403. |

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `IMGCACHE_ZMQ_ENDPOINT` | yes | `tcp://127.0.0.1:5555` | ZMQ REQ/REP endpoint for the imgcache worker. |
| `IMGCACHE_URL_SIGNING_SECRET` | yes | — | HMAC-SHA256 secret (32+ random bytes). |
| `IMGCACHE_URL_SIGNING_SECRET_PREVIOUS` | no | — | Previous secret accepted during rotation overlap. |
| `IMGCACHE_PUBLIC_BASE_URL` | no | `http://localhost:80` | Public origin for signed image URLs (Caddy). |
| `PORT` | no | `3000` | Node server listen port. |

## Deployment notes

### Node adapter (required)

This app uses `@sveltejs/adapter-node`. The Node server runs inside the
compose network alongside the worker and Caddy.

### zeromq native dependency

`@imgcache/client` depends on `zeromq` (zeromq.js v6), which ships
linux-x64 prebuilds. If prebuilds don't match your platform:

```bash
apt-get install -y python3 make g++
npm rebuild zeromq
```

### Pin Node version

The Dockerfile pins **Node 20** (`node:20-slim`). Do not switch to a
different major without testing zeromq prebuild compatibility.

### Serverless / edge adapters: unsupported

Do **not** use `@sveltejs/adapter-vercel` (serverless),
`@sveltejs/adapter-cloudflare`, or any edge adapter. The ZMQ REQ/REP
transport requires **long-lived TCP sockets** — serverless functions
terminate between requests, breaking the REQ/REP alternation contract.

### Docker build context

The Dockerfile expects the **repo root** as build context so the
`file:` dependency on `@imgcache/client` resolves:

```yaml
sveltekit:
  build:
    context: ..
    dockerfile: examples/sveltekit/Dockerfile
```

## Local development

```bash
# Build the TS client first (file: dependency needs dist/)
cd clients/typescript && npm ci && npm run build && cd -

# Install + run the SvelteKit dev server
cd examples/sveltekit
npm install
npm run dev
```

## Running with compose

```bash
cd examples
docker compose up
```

The app serves at `http://localhost:3000`. Image URLs point to
`http://localhost:80/cache/leaves/...` (Caddy on port 80).
