import { Request } from 'zeromq';
import { copyFile, mkdir, open, unlink, rename, link, symlink, realpath, readdir, lstat, rmdir, rm } from 'node:fs/promises';
import type { FileHandle } from 'node:fs/promises';
import { basename, resolve, isAbsolute } from 'node:path';
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
export enum RetainPolicy { TTL = 'ttl', PIN = 'pin' }
export enum ErrorType {
  OriginalMissing = 'OriginalMissing', UnreadableFormat = 'UnreadableFormat',
  LimitExceeded = 'LimitExceeded', InvalidSpec = 'InvalidSpec', Timeout = 'Timeout',
  Busy = 'Busy', WorkerError = 'WorkerError', ManagementUnavailable = 'ManagementUnavailable',
  ManagementRequired = 'ManagementRequired', UnsupportedCapability = 'UnsupportedCapability',
  TransportError = 'TransportError', ProtocolError = 'ProtocolError',
  CrossDeviceLink = 'CrossDeviceLink', HashMismatch = 'HashMismatch', StagingLost = 'StagingLost',
  Closed = 'Closed', QueueFull = 'QueueFull'
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
  async register(path: string, options: { file_id?: string; verify?: boolean; mime?: string; metadata?: Record<string, Json> } = {}): Promise<Source> {
    this.assertOpen();
    if (options.file_id !== undefined) validateFileId(options.file_id);
    const raw = resolve(this.root, 'raw');
    await mkdir(raw, { recursive: true });
    return this.withStaging(async staged => {
      try { await copyFile(path, staged); }
      catch (error) { await this.importMissing(path, staged, error); throw error; }
      let file_id = options.file_id;
      try {
        file_id ??= await fileId(staged);
        if (options.verify && options.file_id && await fileId(staged) !== file_id)
          throw new VipsCacheError(ErrorType.HashMismatch, 'file_id does not match source content');
      } catch (error) { throw stagingError(error); }
      await publishImport(staged, resolve(raw, file_id));
      return { file_id, mime: options.mime ?? null, metadata: { filename: basename(path), ...options.metadata } };
    });
  }

  async link(path: string, options: { file_id?: string; verify?: boolean; mode?: 'hardlink' | 'symlink'; link_target?: string; mime?: string; metadata?: Record<string, Json> } = {}): Promise<Source> {
    this.assertOpen();
    if (options.file_id !== undefined) validateFileId(options.file_id);
    const mode = options.mode ?? 'hardlink';
    if (!['hardlink', 'symlink'].includes(mode)) throw new TypeError('Invalid link mode');
    if (options.link_target && (mode !== 'symlink' || !isAbsolute(options.link_target)))
      throw new TypeError('link_target requires symlink mode and an absolute worker-visible path');
    const sourcePath = await realpath(path);
    const file_id = options.file_id ?? await fileId(sourcePath);
    if (options.verify && options.file_id && await fileId(sourcePath) !== file_id)
      throw new VipsCacheError('HashMismatch', 'file_id does not match source content');
    const target = resolve(this.root, 'raw', file_id);
    const source = { file_id, mime: options.mime ?? null, metadata: { filename: basename(path), ...options.metadata } };
    try { const handle = await open(target, 'r'); await handle.close(); return source; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
    await mkdir(resolve(this.root, 'raw'), { recursive: true });
    await this.withStaging(async staged => {
      try {
        if (mode === 'hardlink') await link(sourcePath, staged);
        else await symlink(options.link_target ?? sourcePath, staged);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code === 'EXDEV')
          throw new VipsCacheError(ErrorType.CrossDeviceLink, 'hardlink crosses a filesystem or mount boundary');
        await this.importMissing(sourcePath, staged, error);
        throw error;
      }
      await publishImport(staged, target);
    });
    return source;
  }

  async unlink(file_id: string): Promise<void> {
    this.assertOpen(); validateFileId(file_id);
    await unlink(resolve(this.root, 'raw', file_id)).catch((error: NodeJS.ErrnoException) => { if (error.code !== 'ENOENT') throw error; });
  }

  /** Explicit housekeeping; a concurrent importer may receive StagingLost. */
  async cleanupStaging({olderThan = 86400}: {olderThan?: number} = {}): Promise<number> {
    this.assertOpen();
    if (!Number.isFinite(olderThan) || olderThan < 0) throw new RangeError('olderThan must be finite and nonnegative');
    const raw = resolve(this.root, 'raw');
    let names: string[];
    try { names = await readdir(raw); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return 0; throw error; }
    const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
    const modernPattern = new RegExp(`^\\.staging-${uuid}$`);
    const legacyPattern = new RegExp(`^(?:\\.${uuid}|\\.[0-9a-f]{32}\\.[a-z0-9_]{8})$`);
    const cutoff = Date.now() - olderThan * 1000;
    let removed = 0;
    for (const name of names) {
      const modern = modernPattern.test(name);
      if (!modern && !legacyPattern.test(name)) continue;
      const path = resolve(raw, name);
      try {
        const entry = await lstat(path);
        const timestamp = entry.isDirectory() ? entry.mtimeMs : Math.max(entry.mtimeMs, entry.ctimeMs);
        if (timestamp > cutoff) continue;
        if (entry.isDirectory()) {
          if ((await readdir(path)).some(child => child !== 'source')) continue;
          const source = resolve(path, 'source');
          let child;
          try { child = await lstat(source); }
          catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
          if (child?.isDirectory()) continue;
          if (child) await unlink(source);
          await rmdir(path);
        } else if (!modern) await unlink(path);
        else continue;
        removed++;
      } catch (error) {
        if (!['ENOENT', 'ENOTEMPTY'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error;
      }
    }
    return removed;
  }

  async ensure(spec: ImageSpec, {retain = RetainPolicy.TTL}: {retain?: RetainPolicy | 'ttl' | 'pin'} = {}): Promise<string> {
    if (![RetainPolicy.TTL, RetainPolicy.PIN].includes(retain as RetainPolicy)) throw new TypeError('Invalid retention');
    const resolved = await this.resolve(spec);
    if (!resolved.spec.encode) throw new TypeError('ensure requires a leaf');
    const reply = await this.request({method: 'ensure', spec: resolved.spec, retain});
    if (reply.relpath !== resolved.relpath) throw new VipsCacheError(ErrorType.ProtocolError, 'Worker returned a different cache path');
    return this.localPath(resolved.relpath);
  }

  async unpin(spec: ImageSpec): Promise<void> {
    const resolved = await this.resolve(spec);
    if (!resolved.spec.encode) throw new TypeError('unpin requires a leaf');
    await this.request({ method: 'unpin', spec: resolved.spec });
  }

  async delete(specs: ImageSpec[]): Promise<void> {
    const resolved = await Promise.all(specs.map(spec => this.resolve(spec)));
    if (resolved.some(item => !item.spec.encode)) throw new TypeError('delete requires leaves');
    await this.request({ method: 'delete', specs: resolved.map(item => item.spec) });
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
      if (reply.relpath !== resolved.relpath) throw new VipsCacheError(ErrorType.ProtocolError, 'Worker returned a different cache path');
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
    for (const waiter of this.queue.splice(0)) { clearTimeout(waiter.timer); waiter.reject(new VipsCacheError(ErrorType.Closed, 'Client is closed')); }
    for (const socket of this.sockets) socket.close();
  }

  private async withStaging<T>(write: (path: string) => Promise<T>): Promise<T> {
    const directory = resolve(this.root, 'raw', `.staging-${randomUUID()}`);
    await mkdir(resolve(this.root, 'raw'), {recursive: true});
    await mkdir(directory);
    try { return await write(resolve(directory, 'source')); }
    finally { await rm(directory, {recursive: true, force: true}); }
  }

  private async importMissing(source: string, staged: string, error: unknown): Promise<void> {
    if ((error as NodeJS.ErrnoException).code !== 'ENOENT') return;
    try { await lstat(resolve(staged, '..')); }
    catch (missing) {
      if ((missing as NodeJS.ErrnoException).code === 'ENOENT') throw stagingError(error);
      throw missing;
    }
    throw new VipsCacheError(ErrorType.OriginalMissing, source);
  }

  private localPath(relpath: string): string {
    if (!/^(raw\/[0-9a-f]{32}|cache\/leaves\/[0-9a-f]{2}\/[0-9a-f]{32}\.(webp|png|jpg|avif|tif|tiff))$/.test(relpath))
      throw new VipsCacheError(ErrorType.ProtocolError, 'Invalid cache path');
    return resolve(this.root, relpath);
  }
  private assertOpen(): void { if (this.closed) throw new VipsCacheError(ErrorType.Closed, 'Client is closed'); }
  private async acquire(deadline: number): Promise<void> {
    this.assertOpen();
    if (this.active < this.options.maxConcurrency) { this.active++; return; }
    if (this.queue.length >= this.options.maxQueue) throw new VipsCacheError(ErrorType.QueueFull, 'Client request queue is full');
    await new Promise<void>((resolve, reject) => {
      const waiter: Waiter = { resolve, reject, timer: setTimeout(() => {
        const index = this.queue.indexOf(waiter);
        if (index >= 0) this.queue.splice(index, 1);
        reject(new VipsCacheError(ErrorType.Timeout, 'Request expired in queue'));
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
          if (ms <= 0) throw new VipsCacheError(ErrorType.Timeout, 'Worker request deadline exceeded');
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
            if (frames.length !== 1) throw new VipsCacheError(ErrorType.ProtocolError, 'Expected one JSON reply frame');
            const reply = JSON.parse(frames[0]!.toString()) as Reply;
            if (!reply || typeof reply.ok !== 'boolean') throw new VipsCacheError(ErrorType.ProtocolError, 'Invalid worker reply');
            if (reply.ok) return reply;
            if (reply.error?.type !== ErrorType.Busy) throw new VipsCacheError(reply.error?.type ?? ErrorType.WorkerError, reply.error?.message ?? 'Worker request failed');
            const delay = typeof reply.retry_after === 'number' && Number.isFinite(reply.retry_after) ? Math.max(0, reply.retry_after * 1000) : 500;
            if (delay >= remaining()) throw new VipsCacheError(ErrorType.Timeout, 'Worker stayed busy past deadline');
            await sleep(Math.max(1, delay), undefined, { signal: this.stopping.signal });
          }
        } catch (error) {
          this.assertOpen();
          if ((error as NodeJS.ErrnoException).code !== 'EAGAIN') throw error;
          if (attempt >= this.options.requestRetries) throw new VipsCacheError(ErrorType.Timeout, 'Worker did not reply within request deadline');
        } finally { this.sockets.delete(socket); if (!socket.closed) socket.close(); }
      }
    } finally { this.release(); }
  }
}

function validateFileId(value: string): void {
  if (!/^[0-9a-f]{32}$/.test(value)) throw new TypeError('Invalid source file_id');
}

function stagingError(error: unknown): unknown {
  return (error as NodeJS.ErrnoException).code === 'ENOENT'
    ? new VipsCacheError(ErrorType.StagingLost, 'Import staging disappeared; caller may retry') : error;
}

async function publishImport(staged: string, target: string): Promise<void> {
  try { await rename(staged, target); }
  catch (error) { throw stagingError(error); }
}
