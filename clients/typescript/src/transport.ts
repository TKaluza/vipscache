import { Request } from "zeromq";
import { sleep } from "./util.js";
import type { JsonValue } from "./types.js";

export interface TransportOptions {
  endpoint: string;
  timeoutMs?: number;
  retries?: number;
}

type RequestPayload = Record<string, JsonValue | undefined>;
type ResponsePayload = Record<string, unknown>;

export class ZmqTransport {
  readonly endpoint: string;
  readonly timeoutMs: number;
  readonly retries: number;
  #socket: Request | null = null;
  #queue: Promise<unknown> = Promise.resolve();

  constructor(options: TransportOptions) {
    this.endpoint = options.endpoint;
    this.timeoutMs = options.timeoutMs ?? 300_000;
    this.retries = options.retries ?? 2;
  }

  request(payload: RequestPayload): Promise<ResponsePayload> {
    const run = this.#queue.then(() => this.#request(payload));
    this.#queue = run.catch(() => undefined);
    return run;
  }

  close(): void {
    this.#resetSocket(false);
  }

  #getSocket(): Request {
    if (this.#socket) return this.#socket;
    const socket = new Request({ linger: 0, receiveTimeout: this.timeoutMs, sendTimeout: this.timeoutMs });
    socket.connect(this.endpoint);
    this.#socket = socket;
    return socket;
  }

  #resetSocket(recreate: boolean): void {
    if (this.#socket) {
      this.#socket.close();
      this.#socket = null;
    }
    if (recreate) this.#getSocket();
  }

  async #request(payload: RequestPayload): Promise<ResponsePayload> {
    const attempts = this.retries + 1;
    const deadline = Date.now() + this.timeoutMs * attempts;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      try {
        const socket = this.#getSocket();
        await this.#withTimeout(socket.send(JSON.stringify(payload)), deadline, "ZMQ worker send timed out");
        for (;;) {
          const remaining = Math.max(0, Math.min(this.timeoutMs, deadline - Date.now()));
          if (remaining <= 0) break;
          const frames = await this.#withTimeout(socket.receive(), Date.now() + remaining, "ZMQ worker did not reply before timeout");
          const response = parseResponse(frames);
          const retryAfter = busyRetryAfter(response);
          if (retryAfter === null) return response;
          const retryAfterMs = retryAfter * 1000;
          if (Date.now() + retryAfterMs >= deadline) {
            throw new TimeoutError("ZMQ worker stayed busy past the request deadline");
          }
          await sleep(retryAfterMs);
          await this.#withTimeout(socket.send(JSON.stringify(payload)), deadline, "ZMQ worker send timed out");
        }
      } catch (error) {
        if (error instanceof TimeoutError) {
          this.#resetSocket(attempt !== attempts - 1);
          if (attempt === attempts - 1) throw error;
          continue;
        }
        this.#resetSocket(attempt !== attempts - 1);
        if (attempt === attempts - 1) throw new Error(`ZMQ request failed: ${errorMessage(error)}`);
      }
    }
    throw new TimeoutError(`ZMQ worker did not reply after ${attempts} request attempts`);
  }

  async #withTimeout<T>(promise: Promise<T>, deadline: number, message: string): Promise<T> {
    const remaining = Math.max(0, deadline - Date.now());
    if (remaining <= 0) throw new TimeoutError(message);
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([
        promise,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new TimeoutError(message)), remaining);
        })
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
}

export class TimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TimeoutError";
  }
}

function parseResponse(frames: Buffer[] | Buffer): ResponsePayload {
  const frame = Array.isArray(frames) ? frames[0] : frames;
  if (!frame) throw new Error("empty ZMQ response");
  return JSON.parse(frame.toString("utf8")) as ResponsePayload;
}

function busyRetryAfter(response: ResponsePayload): number | null {
  if (response.ok) return null;
  const error = response.error as { type?: unknown } | undefined;
  if (!error || error.type !== "Busy") return null;
  const value = Number(response.retry_after ?? 0.5);
  return Number.isFinite(value) ? Math.max(value, 0) : 0.5;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
