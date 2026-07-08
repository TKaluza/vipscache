import type { ImgCache } from "./client.js";
import { sortedRecord } from "./util.js";
import { ENGINE_VERSION, type EncodeFormat, type ImageSpecPayload, type JsonValue, type MaterializePolicy, type OperationPayload, type SourcePayload, type IdentifyResult, type MaterializeResult } from "./types.js";

export interface ScaleParams {
  longestEdge?: number;
  width?: number;
  height?: number;
  scaleFactor?: number;
}

export interface ResizeParams extends ScaleParams {}

export class ImageSpec {
  readonly #client: ImgCache;
  readonly source: SourcePayload;
  readonly operations: readonly OperationPayload[];
  readonly encode: ImageSpecPayload["encode"];

  constructor(client: ImgCache, source: SourcePayload, operations: readonly OperationPayload[] = [], encode: ImageSpecPayload["encode"] = null) {
    this.#client = client;
    this.source = { file_id: source.file_id, metadata: { ...source.metadata }, mime: source.mime ?? null };
    this.operations = operations.map((operation) => ({
      materialize: operation.materialize,
      name: operation.name,
      params: sortedRecord(operation.params)
    }));
    this.encode = encode ? { engine_version: encode.engine_version, format: encode.format, params: sortedRecord(encode.params) } : null;
    Object.freeze(this.source.metadata);
    Object.freeze(this.source);
    for (const operation of this.operations) {
      Object.freeze(operation.params);
      Object.freeze(operation);
    }
    Object.freeze(this.operations);
    if (this.encode) {
      Object.freeze(this.encode.params);
      Object.freeze(this.encode);
    }
  }

  static fromPayload(client: ImgCache, payload: ImageSpecPayload): ImageSpec {
    return new ImageSpec(client, payload.source, payload.operations, payload.encode);
  }

  toPayload(): ImageSpecPayload {
    return {
      encode: this.encode ? { engine_version: this.encode.engine_version, format: this.encode.format, params: { ...this.encode.params } } : null,
      operations: this.operations.map((operation) => ({ materialize: operation.materialize, name: operation.name, params: { ...operation.params } })),
      source: { file_id: this.source.file_id, metadata: { ...this.source.metadata }, mime: this.source.mime }
    };
  }

  operation(name: string, params: Record<string, JsonValue | undefined> = {}, materialize: MaterializePolicy = "never"): ImageSpec {
    return new ImageSpec(this.#client, this.source, [...this.operations, { materialize, name, params: sortedRecord(params) }], this.encode);
  }

  page(page = 1, params: Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.operation("render", { page, ...params });
  }

  normalize(colorspace = "srgb"): ImageSpec {
    return this.operation("normalize", { colorspace });
  }

  scale(params: ScaleParams): ImageSpec {
    return this.operation("scale", snakeScaleParams(params));
  }

  resize(params: ResizeParams): ImageSpec {
    return this.operation("resize", snakeScaleParams(params));
  }

  crop(x: number, y: number, w: number, h: number): ImageSpec {
    return this.operation("crop", { h, w, x, y });
  }

  cropFraction(params: { left?: number; top?: number; right?: number; bottom?: number } = {}): ImageSpec {
    return this.operation("crop_fraction", {
      bottom: params.bottom ?? 1.0,
      left: params.left ?? 0.0,
      right: params.right ?? 1.0,
      top: params.top ?? 0.0
    });
  }

  rotate(degrees: number): ImageSpec {
    return this.operation("rotate", { degrees });
  }

  fastRotate(degrees: number): ImageSpec {
    return this.operation("fast_rotate", { degrees });
  }

  flip(): ImageSpec {
    return this.operation("flip");
  }

  flop(): ImageSpec {
    return this.operation("flop");
  }

  webp(params: { quality?: number } & Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.withEncode("webp", { quality: params.quality ?? 82, ...without(params, "quality") });
  }

  png(params: Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.withEncode("png", params);
  }

  jpg(params: { quality?: number } & Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.withEncode("jpg", { quality: params.quality ?? 85, ...without(params, "quality") });
  }

  avif(params: { quality?: number } & Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.withEncode("avif", { quality: params.quality ?? 60, ...without(params, "quality") });
  }

  tif(params: Record<string, JsonValue | undefined> = {}): ImageSpec {
    return this.withEncode("tif", params);
  }

  withEncode(format: EncodeFormat, params: Record<string, JsonValue | undefined> = {}): ImageSpec {
    return new ImageSpec(this.#client, this.source, this.operations, {
      engine_version: ENGINE_VERSION,
      format: format.toLowerCase(),
      params: sortedRecord(params)
    });
  }

  resolve(): Promise<MaterializeResult> {
    return this.#client.resolve(this);
  }

  materialize(): Promise<MaterializeResult> {
    return this.#client.materialize(this);
  }

  identify(): Promise<IdentifyResult> {
    return this.#client.identify(this);
  }
}

function snakeScaleParams(params: ScaleParams): Record<string, JsonValue | undefined> {
  return {
    height: params.height,
    longest_edge: params.longestEdge,
    scale_factor: params.scaleFactor,
    width: params.width
  };
}

function without<T extends Record<string, unknown>, K extends keyof T>(value: T, key: K): Omit<T, K> {
  const clone = { ...value };
  delete clone[key];
  return clone;
}
