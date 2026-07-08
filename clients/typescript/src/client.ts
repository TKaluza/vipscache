import { constants as fsConstants } from "node:fs";
import { mkdir, open, readFile, rename, rm } from "node:fs/promises";
import { basename, join } from "node:path";
import { randomUUID } from "node:crypto";
import xxhashWasm from "xxhash-wasm";
import { xxh3 } from "@node-rs/xxhash";
import { LruMap } from "./lru.js";
import { ImageSpec } from "./spec.js";
import { ZmqTransport } from "./transport.js";
import { canonicalJson, keyFromRelpath } from "./util.js";
import type { IdentifyResult, ImageSpecPayload, JsonValue, MaterializeResult, SourcePayload } from "./types.js";

export interface ImgCacheOptions {
  endpoint: string;
  timeoutMs?: number;
  retries?: number;
  memoCap?: number;
  rawRoot?: string;
}

export interface IngestOptions {
  metadata?: Record<string, JsonValue>;
  filename?: string;
}

export class ImgCache {
  readonly endpoint: string;
  readonly rawRoot: string | undefined;
  readonly #transport: ZmqTransport;
  readonly #memo: LruMap<string, string>;

  constructor(options: ImgCacheOptions) {
    this.endpoint = options.endpoint;
    this.rawRoot = options.rawRoot;
    const transportOptions: { endpoint: string; retries?: number; timeoutMs?: number } = { endpoint: options.endpoint };
    if (options.retries !== undefined) transportOptions.retries = options.retries;
    if (options.timeoutMs !== undefined) transportOptions.timeoutMs = options.timeoutMs;
    this.#transport = new ZmqTransport(transportOptions);
    this.#memo = new LruMap(options.memoCap ?? 10_000);
  }

  open(fileId: string, mime: string | null = null, metadata: Record<string, JsonValue> = {}): ImageSpec {
    return new ImageSpec(this, { file_id: fileId, metadata, mime });
  }

  specFromPayload(payload: ImageSpecPayload): ImageSpec {
    return ImageSpec.fromPayload(this, payload);
  }

  async materialize(spec: ImageSpec): Promise<MaterializeResult> {
    const payload = spec.toPayload();
    const memoKey = canonicalJson(payload as unknown as JsonValue);
    const cached = this.#memo.get(memoKey);
    if (cached !== undefined) return { relpath: cached, key: keyFromRelpath(cached) };
    const response = await this.#transport.request({ method: "materialize", spec: payload as unknown as JsonValue });
    if (response.ok) {
      const relpath = String(response.relpath);
      this.#memo.set(memoKey, relpath);
      return { relpath, key: keyFromRelpath(relpath) };
    }
    throw workerError(response);
  }

  resolve(spec: ImageSpec): Promise<MaterializeResult> {
    return this.materialize(spec);
  }

  async identify(spec: ImageSpec): Promise<IdentifyResult> {
    const payload = spec.toPayload();
    const memoKey = `identify:${canonicalJson(payload as unknown as JsonValue)}`;
    const cached = this.#memo.get(memoKey);
    if (cached !== undefined) return JSON.parse(cached) as IdentifyResult;
    const response = await this.#transport.request({ method: "identify", spec: payload as unknown as JsonValue });
    if (response.ok) {
      const meta = response.meta as IdentifyResult;
      this.#memo.set(memoKey, JSON.stringify(meta));
      return meta;
    }
    throw workerError(response);
  }

  async health(): Promise<boolean> {
    const response = await this.#transport.request({ method: "health" });
    return Boolean(response.ok);
  }

  async ingest(input: Buffer | Uint8Array | ArrayBuffer | string, mime: string | null, options: IngestOptions = {}): Promise<ImageSpec> {
    if (!this.rawRoot) throw new Error("ImgCache.ingest requires rawRoot in constructor options");
    const bytes = typeof input === "string" ? await readFile(input) : toBuffer(input);
    const fileId = await xxh3_128Hex(bytes);
    await mkdir(this.rawRoot, { recursive: true });
    const storedPath = join(this.rawRoot, fileId);
    const tmpPath = join(this.rawRoot, `.${fileId}.${process.pid}.${randomUUID()}`);
    const handle = await open(tmpPath, fsConstants.O_CREAT | fsConstants.O_EXCL | fsConstants.O_WRONLY, 0o600);
    try {
      await handle.writeFile(bytes);
      await handle.close();
      try {
        await rename(tmpPath, storedPath);
      } catch (error) {
        if (isAlreadyExists(error)) await rm(tmpPath, { force: true });
        else throw error;
      }
    } catch (error) {
      try { await handle.close(); } catch { /* ignore */ }
      await rm(tmpPath, { force: true });
      throw error;
    }
    const metadata: Record<string, JsonValue> = { ...(options.metadata ?? {}) };
    const filename = options.filename ?? (typeof input === "string" ? basename(input) : undefined);
    if (filename !== undefined) metadata.filename = filename;
    return this.open(fileId, mime, metadata);
  }

  close(): void {
    this.#transport.close();
  }
}

let xxhashPromise: ReturnType<typeof xxhashWasm> | undefined;

export async function xxh3_128Hex(data: Buffer | Uint8Array | ArrayBuffer): Promise<string> {
  xxhashPromise ??= xxhashWasm();
  const hasher = await xxhashPromise;
  void hasher;
  return xxh3.xxh128(toBuffer(data)).toString(16).padStart(32, "0");
}

function toBuffer(data: Buffer | Uint8Array | ArrayBuffer): Buffer {
  if (Buffer.isBuffer(data)) return data;
  if (data instanceof ArrayBuffer) return Buffer.from(data);
  return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
}

function workerError(response: Record<string, unknown>): Error {
  const error = (response.error ?? {}) as { type?: unknown; message?: unknown };
  return new Error(`${String(error.type ?? "WorkerError")}: ${String(error.message ?? "worker request failed")}`);
}

function isAlreadyExists(error: unknown): boolean {
  return typeof error === "object" && error !== null && "code" in error && (error as { code?: unknown }).code === "EEXIST";
}

export type { ImageSpec } from "./spec.js";
