import { access } from "node:fs/promises";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { ImgCache, type ImageSpecPayload } from "../src/index.js";

const endpoint = process.env.IMGCACHE_ZMQ_ENDPOINT;
const cacheRoot = process.env.IMGCACHE_CACHE_ROOT;

interface SpecFixture {
  relpath: string;
  spec: ImageSpecPayload;
}

const maybeDescribe = endpoint && cacheRoot ? describe : describe.skip;

maybeDescribe("worker integration", () => {
  it("resolves golden fixtures against a running worker", async () => {
    const fs = await import("node:fs");
    const repoRoot = new URL("../../..", import.meta.url);
    const text = fs.readFileSync(new URL("tests/golden/specs.jsonl", repoRoot), "utf8");
    const fixtures = text.trim().split("\n").map((line) => JSON.parse(line) as SpecFixture);
    const cache = new ImgCache({ endpoint: endpoint!, timeoutMs: 300_000, retries: 2 });
    try {
      for (const fixture of fixtures) {
        const result = await cache.specFromPayload(fixture.spec).resolve();
        expect(result.relpath).toBe(fixture.relpath);
        await access(join(cacheRoot!, result.relpath));
      }
    } finally {
      cache.close();
    }
  });
});
