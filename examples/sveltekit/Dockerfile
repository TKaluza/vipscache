# syntax=docker/dockerfile:1

# ── Build stage ──────────────────────────────────────────────
# Build context must be the repo root so the file: reference to
# ../../clients/typescript resolves inside the container.
FROM node:20-slim AS builder

# zeromq ships linux-x64 prebuilds, but keep build tools as fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /repo

# 1. Build the TypeScript client → dist/
COPY clients/typescript/package.json clients/typescript/package-lock.json* ./clients/typescript/
COPY clients/typescript/tsconfig.json ./clients/typescript/
COPY clients/typescript/src ./clients/typescript/src
RUN cd clients/typescript && npm ci && npm run build

# 2. Install + build the SvelteKit app.
#    The file: dependency resolves to ../../clients/typescript (already built).
COPY examples/sveltekit/package.json examples/sveltekit/svelte.config.js \
     examples/sveltekit/vite.config.ts examples/sveltekit/tsconfig.json \
     ./examples/sveltekit/
COPY examples/sveltekit/src ./examples/sveltekit/src
RUN cd examples/sveltekit && npm install && npm run build

# ── Runtime stage ────────────────────────────────────────────
FROM node:20-slim AS runtime

WORKDIR /app

# Adapter-node outputs to build/.
COPY --from=builder /repo/examples/sveltekit/build ./build
COPY --from=builder /repo/examples/sveltekit/package.json ./package.json
COPY --from=builder /repo/examples/sveltekit/node_modules ./node_modules

ENV NODE_ENV=production
ENV PORT=3000
EXPOSE 3000

# zeromq needs LD_LIBRARY_PATH on some slim images.
ENV LD_LIBRARY_PATH=/app/node_modules/zeromq/build/Release:/usr/lib

CMD ["node", "build"]
