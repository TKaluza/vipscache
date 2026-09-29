import { Request } from 'zeromq';
import { copyFile, mkdir, open, unlink, rename } from 'node:fs/promises';
import type { FileHandle } from 'node:fs/promises';
import { basename, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { setTimeout as sleep } from 'node:timers/promises';
import { canonicalJson, fileId, resolveSpec } from './spec.js';
export { fileId, resolveSpec, canonicalJson } from './spec.js';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface Source { file_id: string; mime?: string | null; metadata?: Record<string, Json> }
export interface Operation {
  name: string;
  params?: Record<string, Json>;
  materialize?: 'never' | 'force' | 'pin';
}
export interface ImageSpec {
  source: Source;
  operations?: Operation[];
  encode?: { format: 'webp' | 'png' | 'jpg' | 'jpeg' | 'avif' | 'tif' | 'tiff'; params?: Record<string, Json>; engine_version?: string } | null;
}
export interface Resolution { key: string; relpath: string; spec: ImageSpec }
export interface ClientOptions {
  root: string;
  endpoint: string;
  /** Total deadline including queue, send, receive and Busy waits. */
  timeoutMs?: number;
  requestRetries?: number;
  maxConcurrency?: number;
  maxQueue?: number;
}
export class VipsCacheError extends Error {
  constructor(public readonly type: string, message: string) { super(message); this.name = type; }
}
interface Reply { ok: boolean; error?: { type: string; message: string }; retry_after?: number; [key: string]: unknown }
interface Waiter { resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }

/** Node-only client; authorize source paths and specs in your application before calling. */
export class VipsCacheClient {
  readonly root: string;
  private readonly options: Required<ClientOptions>;
  private active = 0;
  private closed = false;
  private readonly queue: Waiter[] = [];
  private readonly sockets = new Set<Request>();
  private readonly stopping = new AbortController();

  constructor(options: ClientOptions) {
    this.root = resolve(options.root);
    this.options = { timeoutMs: 30_000, requestRetries: 2, maxConcurrency: 4, maxQueue: 64, ...options };
    for (const key of ['timeoutMs', 'maxConcurrency', 'requestRetries', 'maxQueue'] as const) {
      const value = this.options[key];
      if (!Number.isSafeInteger(value) || value < (key === 'timeoutMs' || key === 'maxConcurrency' ? 1 : 0) || value > 2_147_483_647)
        throw new RangeError(`invalid ${key}`);
    }
  }

  async healthcheck(): Promise<boolean> { return (await this.request({ method: 'health' })).ok; }

  /** Atomically register a snapshot; hash the copied bytes, not a mutable input. */
  async register(path: string, options: { mime?: string; metadata?: Record<string, Json> } = {}): Promise<Source> {
    this.assertOpen();
    const raw = resolve(this.root, 'raw');
    await mkdir(raw, { recursive: true });
    const staged = resolve(raw, `.${randomUUID()}`);
    try {
      await copyFile(path, staged);
      const file_id = await fileId(staged);
      await rename(staged, resolve(raw, file_id));
      return { file_id, mime: options.mime ?? null, metadata: { filename: basename(path), ...options.metadata } };
    } finally { await unlink(staged).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'ENOENT') throw error; }); }
  }

  async identify(spec: ImageSpec): Promise<Record<string, Json>> {
    return (await this.request({ method: 'identify', spec })).meta as Record<string, Json>;
  }

  async resolve(spec: ImageSpec): Promise<Resolution> {
    this.assertOpen();
    return resolveSpec(spec);
  }

  /** Open first, so eviction cannot race between an existence check and the caller's read. */
  async open(spec: ImageSpec): Promise<FileHandle> {
    const resolved = await this.resolve(spec);
    const path = this.localPath(resolved.relpath);
    try { return await open(path, 'r'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT' || !resolved.spec.encode) throw error; }
    // Retry one eviction race after a successful render.
    for (let attempt = 0; ; attempt++) {
      const reply = await this.request({ method: 'materialize', spec: resolved.spec });
      if (reply.relpath !== resolved.relpath) throw new VipsCacheError('ProtocolError', 'Worker returned a different cache path');
      try { return await open(path, 'r'); }
      catch (error) { if (attempt || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    }
  }

  async readBytes(spec: ImageSpec): Promise<Buffer> {
    const file = await this.open(spec);
    try { return await file.readFile(); } finally { await file.close(); }
  }

  close(): void {
    this.closed = true;
    this.stopping.abort();
    for (const waiter of this.queue.splice(0)) { clearTimeout(waiter.timer); waiter.reject(new VipsCacheError('Closed', 'Client is closed')); }
    for (const socket of this.sockets) socket.close();
  }

  private localPath(relpath: string): string {
    if (!/^(raw\/[0-9a-f]{32}|cache\/leaves\/[0-9a-f]{2}\/[0-9a-f]{32}\.(webp|png|jpg|avif|tif|tiff))$/.test(relpath))
      throw new VipsCacheError('ProtocolError', 'Invalid cache path');
    return resolve(this.root, relpath);
  }
  private assertOpen(): void { if (this.closed) throw new VipsCacheError('Closed', 'Client is closed'); }
  private async acquire(deadline: number): Promise<void> {
    this.assertOpen();
    if (this.active < this.options.maxConcurrency) { this.active++; return; }
    if (this.queue.length >= this.options.maxQueue) throw new VipsCacheError('QueueFull', 'Client request queue is full');
    await new Promise<void>((resolve, reject) => {
      const waiter: Waiter = { resolve, reject, timer: setTimeout(() => {
        const index = this.queue.indexOf(waiter);
        if (index >= 0) this.queue.splice(index, 1);
        reject(new VipsCacheError('Timeout', 'Request expired in queue'));
      }, Math.max(1, deadline - performance.now())) };
      this.queue.push(waiter);
    });
  }
  private release(): void {
    const next = this.queue.shift();
    if (next) { clearTimeout(next.timer); next.resolve(); } else this.active--;
  }
  private async request(payload: object): Promise<Reply> {
    const deadline = performance.now() + this.options.timeoutMs;
    const wire = canonicalJson(payload as Json);
    await this.acquire(deadline);
    try {
      for (let attempt = 0; ; attempt++) {
        this.assertOpen();
        const remaining = () => {
          const ms = Math.ceil(deadline - performance.now());
          if (ms <= 0) throw new VipsCacheError('Timeout', 'Worker request deadline exceeded');
          return ms;
        };
        // Every in-flight exchange owns its REQ socket. A failed exchange discards it.
        const socket = new Request({ linger: 0, immediate: true, maxMessageSize: 4 * 1024 * 1024 });
        this.sockets.add(socket);
        try {
          socket.connect(this.options.endpoint);
          for (;;) {
            this.assertOpen();
            // Reserve time for subsequent transport attempts within the total deadline.
            const budget = Math.max(1, Math.floor(remaining() / (this.options.requestRetries - attempt + 1)));
            const attemptDeadline = performance.now() + budget;
            socket.sendTimeout = budget;
            await socket.send(wire);
            socket.receiveTimeout = Math.max(1, Math.min(remaining(), Math.ceil(attemptDeadline - performance.now())));
            const frames = await socket.receive();
            if (frames.length !== 1) throw new VipsCacheError('ProtocolError', 'Expected one JSON reply frame');
            const reply = JSON.parse(frames[0]!.toString()) as Reply;
            if (!reply || typeof reply.ok !== 'boolean') throw new VipsCacheError('ProtocolError', 'Invalid worker reply');
            if (reply.ok) return reply;
            if (reply.error?.type !== 'Busy') throw new VipsCacheError(reply.error?.type ?? 'WorkerError', reply.error?.message ?? 'Worker request failed');
            const delay = typeof reply.retry_after === 'number' && Number.isFinite(reply.retry_after) ? Math.max(0, reply.retry_after * 1000) : 500;
            if (delay >= remaining()) throw new VipsCacheError('Timeout', 'Worker stayed busy past deadline');
            await sleep(Math.max(1, delay), undefined, { signal: this.stopping.signal });
          }
        } catch (error) {
          this.assertOpen();
          if ((error as NodeJS.ErrnoException).code !== 'EAGAIN') throw error;
          if (attempt >= this.options.requestRetries) throw new VipsCacheError('Timeout', 'Worker did not reply within request deadline');
        } finally { this.sockets.delete(socket); if (!socket.closed) socket.close(); }
      }
    } finally { this.release(); }
  }
}
