export class LruMap<K, V> {
  readonly #cap: number;
  readonly #map = new Map<K, V>();

  constructor(cap: number) {
    this.#cap = Math.max(0, Math.trunc(cap));
  }

  get(key: K): V | undefined {
    if (!this.#map.has(key)) return undefined;
    const value = this.#map.get(key) as V;
    this.#map.delete(key);
    this.#map.set(key, value);
    return value;
  }

  set(key: K, value: V): void {
    if (this.#cap <= 0) return;
    if (this.#map.has(key)) this.#map.delete(key);
    this.#map.set(key, value);
    while (this.#map.size > this.#cap) {
      const oldest = this.#map.keys().next().value as K | undefined;
      if (oldest === undefined) break;
      this.#map.delete(oldest);
    }
  }

  clear(): void {
    this.#map.clear();
  }
}
