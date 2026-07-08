export const ENGINE_VERSION = "imgcache-v1" as const;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonObject | JsonValue[];
export interface JsonObject { [key: string]: JsonValue | undefined }

export type MaterializePolicy = "never" | "force" | "pin";
export type EncodeFormat = "webp" | "png" | "jpg" | "jpeg" | "avif" | "tif" | "tiff";

export interface SourcePayload {
  file_id: string;
  metadata: Record<string, JsonValue>;
  mime: string | null;
}

export interface OperationPayload {
  materialize: MaterializePolicy;
  name: string;
  params: Record<string, JsonValue>;
}

export interface EncodePayload {
  engine_version: typeof ENGINE_VERSION | string;
  format: EncodeFormat | string;
  params: Record<string, JsonValue>;
}

export interface ImageSpecPayload {
  encode: EncodePayload | null;
  operations: OperationPayload[];
  source: SourcePayload;
}

export interface MaterializeResult {
  relpath: string;
  key: string | null;
}

export type IdentifyResult = Record<string, JsonValue>;
