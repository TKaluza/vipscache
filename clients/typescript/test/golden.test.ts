import { readFile, mkdtemp, rm, stat } from "node:fs/promises";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { describe, expect, it } from "vitest";
import { ImgCache, xxh3_128Hex, type ImageSpec, type ImageSpecPayload } from "../src/index.js";

const repoRoot = new URL("../../..", import.meta.url);
const goldenDir = new URL("tests/golden/", repoRoot);

interface SpecFixture {
  relpath: string;
  spec: ImageSpecPayload;
}

function fixtures(): SpecFixture[] {
  const text = readFileSyncUtf8(new URL("specs.jsonl", goldenDir));
  return text.trim().split("\n").map((line) => JSON.parse(line) as SpecFixture);
}

function readFileSyncUtf8(url: URL): string {
  return readFileSync(url, "utf8");
}

function buildFromPayload(cache: ImgCache, payload: ImageSpecPayload): ImageSpec {
  let spec = cache.open(payload.source.file_id, payload.source.mime, payload.source.metadata);
  for (const operation of payload.operations) {
    const p = operation.params;
    switch (operation.name) {
      case "render":
        spec = spec.page(Number(p.page), Object.fromEntries(Object.entries(p).filter(([key]) => key !== "page")));
        break;
      case "normalize":
        spec = spec.normalize(String(p.colorspace));
        break;
      case "scale":
        spec = spec.scale({ height: p.height as number | undefined, longestEdge: p.longest_edge as number | undefined, scaleFactor: p.scale_factor as number | undefined, width: p.width as number | undefined });
        break;
      case "resize":
        spec = spec.resize({ height: p.height as number | undefined, longestEdge: p.longest_edge as number | undefined, scaleFactor: p.scale_factor as number | undefined, width: p.width as number | undefined });
        break;
      case "crop":
        spec = spec.crop(Number(p.x), Number(p.y), Number(p.w), Number(p.h));
        break;
      case "crop_fraction":
        spec = spec.cropFraction({ bottom: p.bottom as number, left: p.left as number, right: p.right as number, top: p.top as number });
        break;
      case "rotate":
        spec = spec.rotate(Number(p.degrees));
        break;
      case "fast_rotate":
        spec = spec.fastRotate(Number(p.degrees));
        break;
      case "flip":
        spec = spec.flip();
        break;
      case "flop":
        spec = spec.flop();
        break;
      default:
        spec = spec.operation(operation.name, operation.params, operation.materialize);
    }
  }
  const encode = payload.encode;
  if (!encode) return spec;
  switch (encode.format) {
    case "webp": return spec.webp(encode.params);
    case "png": return spec.png(encode.params);
    case "jpg": return spec.jpg(encode.params);
    case "avif": return spec.avif(encode.params);
    case "tif": return spec.tif(encode.params);
    default: return spec.withEncode(encode.format, encode.params);
  }
}

describe("golden payload parity", () => {
  it("builds payloads matching Python ImageSpec.to_payload() fixtures", () => {
    const cache = new ImgCache({ endpoint: "tcp://127.0.0.1:5555" });
    for (const fixture of fixtures()) {
      expect(buildFromPayload(cache, fixture.spec).toPayload()).toEqual(fixture.spec);
    }
    cache.close();
  });
});

describe("xxh3-128 parity", () => {
  it("matches shared Python hash vectors", async () => {
    const vectors = JSON.parse(await readFile(new URL("hashes.json", goldenDir), "utf8")) as Record<string, { length: number; xxh3_128_hex: string }>;
    const inputs: Record<string, Buffer> = {
      empty: Buffer.alloc(0),
      short_ascii: Buffer.from("imgcache"),
      binary_0_255: Buffer.from(Array.from({ length: 256 }, (_, index) => index)),
      multi_mb_pattern: Buffer.concat(Array.from({ length: 131072 }, () => Buffer.from([...Buffer.from("imgcache-golden-vector"), 0, 255])))
    };
    expect(Object.keys(vectors).sort()).toEqual(Object.keys(inputs).sort());
    for (const [name, data] of Object.entries(inputs)) {
      expect(data.length).toBe(vectors[name]?.length);
      expect(await xxh3_128Hex(data)).toBe(vectors[name]?.xxh3_128_hex);
    }
  });

  it("ingest writes raw objects atomically-addressed by xxh3", async () => {
    const rawRoot = await mkdtemp(join(tmpdir(), "imgcache-raw-"));
    try {
      const cache = new ImgCache({ endpoint: "tcp://127.0.0.1:5555", rawRoot });
      const spec = await cache.ingest(Buffer.from("imgcache"), "text/plain", { filename: "sample.txt" });
      const payload = spec.toPayload();
      expect(payload.source.file_id).toBe("464bbf9ed4d7cd5101cec1235380b4d2");
      expect(payload.source.mime).toBe("text/plain");
      expect(payload.source.metadata.filename).toBe("sample.txt");
      expect((await stat(join(rawRoot, payload.source.file_id))).size).toBe(8);
      cache.close();
    } finally {
      await rm(rawRoot, { force: true, recursive: true });
    }
  });
});
