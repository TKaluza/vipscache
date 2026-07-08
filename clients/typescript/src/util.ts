import type { JsonValue } from "./types.js";

export function sortedRecord<T>(params: Record<string, T | undefined>): Record<string, T> {
  const out: Record<string, T> = {};
  for (const key of Object.keys(params).sort()) {
    const value = params[key];
    if (value !== undefined) out[key] = value;
  }
  return out;
}

export function canonicalJson(value: JsonValue): string {
  return JSON.stringify(sortJson(value));
}

function sortJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map((item) => sortJson(item));
  if (value && typeof value === "object") {
    const out: Record<string, JsonValue> = {};
    for (const key of Object.keys(value).sort()) {
      const child = (value as Record<string, JsonValue | undefined>)[key];
      if (child !== undefined) out[key] = sortJson(child);
    }
    return out;
  }
  return value;
}

export function keyFromRelpath(relpath: string): string | null {
  const file = relpath.split("/").pop();
  if (!file) return null;
  const dot = file.lastIndexOf(".");
  return dot > 0 ? file.slice(0, dot) : file;
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
