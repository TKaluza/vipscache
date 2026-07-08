import adapter from "@sveltejs/adapter-node";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    // Node adapter — runs as a standard Node.js server.
    // Required because zeromq needs long-lived TCP sockets;
    // serverless/edge adapters are unsupported.
    adapter: adapter(),
  },
};

export default config;
