#!/usr/bin/env python3
"""Load test for the imgcache Caddy data plane.

Exercises signed leaf URLs, unsigned pinned URLs, and verifies 403/404
behavior for tampered/expired/raw/nodes paths.  Measures throughput and
latency percentiles for valid requests.

Configuration is via environment variables (all optional, defaults shown):

  IMGCACHE_LOAD_TEST_ENDPOINT        http://localhost:80
  IMGCACHE_LOAD_TEST_SECRET          test-secret-change-me-32-bytes-min
  IMGCACHE_LOAD_TEST_LEAF_PATH       /cache/leaves/e7/test-leaf.webp
  IMGCACHE_LOAD_TEST_PINNED_PATH     /cache/pinned/cd/test-pinned.webp
  IMGCACHE_LOAD_TEST_TTL             3600
  IMGCACHE_LOAD_TEST_CONCURRENCY     10
  IMGCACHE_LOAD_TEST_REQUESTS        100
  IMGCACHE_LOAD_TEST_TIMEOUT         10
  IMGCACHE_LOAD_TEST_DRY_RUN         1   (set to 0 to hit a live endpoint)

Usage:
  # Dry-run: prints what would be tested, validates URL signing locally
  python scripts/load_test.py

  # Live: point at a running Caddy + SvelteKit stack
  IMGCACHE_LOAD_TEST_ENDPOINT=https://images.example.com \
  IMGCACHE_LOAD_TEST_SECRET=$IMGCACHE_URL_SIGNING_SECRET \
  IMGCACHE_LOAD_TEST_LEAF_PATH=/cache/leaves/e7/abc...webp \
  IMGCACHE_LOAD_TEST_DRY_RUN=0 \
  python scripts/load_test.py
"""

from __future__ import annotations

import hashlib
import hmac
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import NamedTuple


# ── config ───────────────────────────────────────────────────────────────────

ENDPOINT = os.getenv("IMGCACHE_LOAD_TEST_ENDPOINT", "http://localhost:80").rstrip("/")
SECRET = os.getenv("IMGCACHE_LOAD_TEST_SECRET", "test-secret-change-me-32-bytes-min")
LEAF_PATH = os.getenv("IMGCACHE_LOAD_TEST_LEAF_PATH", "/cache/leaves/e7/test-leaf.webp")
PINNED_PATH = os.getenv("IMGCACHE_LOAD_TEST_PINNED_PATH", "/cache/pinned/cd/test-pinned.webp")
TTL_SECONDS = int(os.getenv("IMGCACHE_LOAD_TEST_TTL", "3600"))
CONCURRENCY = int(os.getenv("IMGCACHE_LOAD_TEST_CONCURRENCY", "10"))
TOTAL_REQUESTS = int(os.getenv("IMGCACHE_LOAD_TEST_REQUESTS", "100"))
TIMEOUT = int(os.getenv("IMGCACHE_LOAD_TEST_TIMEOUT", "10"))
DRY_RUN = os.getenv("IMGCACHE_LOAD_TEST_DRY_RUN", "1") != "0"


# ── URL signing (mirrors clients/typescript/src/url.ts) ──────────────────────

def sign_url(path: str, secret: str = SECRET, ttl: int = TTL_SECONDS) -> str:
    """Return a signed URL for a cache/leaves path.

    Signature = HMAC-SHA256(secret, path + "\n" + exp).hex
    """
    now = int(time.time())
    exp = now + ttl
    sig = hmac.new(
        secret.encode(),
        f"{path}\n{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{ENDPOINT}{path}?exp={exp}&sig={sig}"


def tampered_url(path: str = LEAF_PATH) -> str:
    """A leaf URL with a garbage signature."""
    now = int(time.time())
    exp = now + TTL_SECONDS
    return f"{ENDPOINT}{path}?exp={exp}&sig={'0' * 64}"


def expired_url(path: str = LEAF_PATH) -> str:
    """A leaf URL whose expiry is in the past."""
    past = int(time.time()) - 1
    sig = hmac.new(
        SECRET.encode(),
        f"{path}\n{past}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{ENDPOINT}{path}?exp={past}&sig={sig}"


def unsigned_url(path: str = LEAF_PATH) -> str:
    """A leaf URL with no query parameters."""
    return f"{ENDPOINT}{path}"


def raw_url() -> str:
    """A /raw/ path that must 404."""
    return f"{ENDPOINT}/raw/somefile"


def nodes_url() -> str:
    """A /cache/nodes/ path that must 404."""
    return f"{ENDPOINT}/cache/nodes/ab/somekey.v"


# ── HTTP ──────────────────────────────────────────────────────────────────────

class HttpResult(NamedTuple):
    status: int
    elapsed_ms: float
    error: str | None = None


def fetch(url: str, timeout: int = TIMEOUT) -> HttpResult:
    """Perform a single GET and return status + elapsed time."""
    start = time.perf_counter()
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            resp.read()  # drain
        elapsed = (time.perf_counter() - start) * 1000
        return HttpResult(status, elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return HttpResult(exc.code, elapsed)
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        return HttpResult(0, elapsed, str(exc))


# ── benchmark ─────────────────────────────────────────────────────────────────

@dataclass
class BenchSummary:
    label: str
    total: int
    statuses: dict[int, int]
    latencies_ms: list[float]
    errors: list[str]

    def report(self) -> str:
        lines = [f"  {self.label}"]
        lines.append(f"    requests: {self.total}")
        lines.append(f"    statuses: {dict(sorted(self.statuses.items()))}")
        if self.latencies_ms:
            lat = sorted(self.latencies_ms)
            p50 = lat[len(lat) // 2]
            p99 = lat[int(len(lat) * 0.99)]
            mean = statistics.mean(lat)
            lines.append(f"    latency_ms: mean={mean:.1f}  p50={p50:.1f}  p99={p99:.1f}")
            lines.append(f"    throughput: {self.total / (sum(lat) / 1000):.1f} req/s (wall)")
        if self.errors:
            sample = self.errors[:3]
            lines.append(f"    errors ({len(self.errors)}): {sample}")
        return "\n".join(lines)


def benchmark(label: str, url_fn, expected_status: int, n: int = TOTAL_REQUESTS) -> BenchSummary:
    """Run *n* concurrent fetches against *url_fn* (callable returning a URL)."""
    statuses: dict[int, int] = {}
    latencies: list[float] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [pool.submit(fetch, url_fn()) for _ in range(n)]
        for fut in as_completed(futures):
            res = fut.result()
            statuses[res.status] = statuses.get(res.status, 0) + 1
            if res.status > 0:
                latencies.append(res.elapsed_ms)
            if res.error:
                errors.append(res.error)

    return BenchSummary(label, n, statuses, latencies, errors)


# ── validation (single-shot checks) ───────────────────────────────────────────

def validate(label: str, url: str, expected: int) -> bool:
    """Single request, assert expected status."""
    if DRY_RUN:
        print(f"  [dry-run] {label}: would GET {url} expecting {expected}")
        return True
    res = fetch(url)
    ok = res.status == expected
    tag = "OK" if ok else "FAIL"
    print(f"  [{tag}] {label}: got {res.status}, expected {expected}  ({res.elapsed_ms:.0f}ms)")
    if res.error:
        print(f"         error: {res.error}")
    return ok


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("imgcache data-plane load test")
    print(f"  endpoint:     {ENDPOINT}")
    print(f"  leaf_path:    {LEAF_PATH}")
    print(f"  pinned_path:  {PINNED_PATH}")
    print(f"  ttl:          {TTL_SECONDS}s")
    print(f"  concurrency:  {CONCURRENCY}")
    print(f"  requests:     {TOTAL_REQUESTS}")
    print(f"  mode:         {'DRY-RUN' if DRY_RUN else 'LIVE'}")
    print("=" * 70)

    # ── validation checks ───────────────────────────────────────────────────
    print("\nValidation checks:")
    all_ok = True

    all_ok &= validate("signed leaf (200 expected)", sign_url(LEAF_PATH), 200)
    all_ok &= validate("unsigned pinned (200 expected)", unsigned_url(PINNED_PATH), 200)
    all_ok &= validate("tampered sig (403 expected)", tampered_url(LEAF_PATH), 403)
    all_ok &= validate("expired URL (403 expected)", expired_url(LEAF_PATH), 403)
    all_ok &= validate("unsigned leaf (403 expected)", unsigned_url(LEAF_PATH), 403)
    all_ok &= validate("raw path (404 expected)", raw_url(), 404)
    all_ok &= validate("nodes path (404 expected)", nodes_url(), 404)

    if not all_ok:
        print("\nValidation FAILED — fix before load testing.")
        return 1

    if DRY_RUN:
        print("\nDry-run complete. Set IMGCACHE_LOAD_TEST_DRY_RUN=0 to run live.")

        # Still verify local signing round-trip
        url = sign_url(LEAF_PATH)
        path_part = url.split(ENDPOINT, 1)[1]
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(path_part)
        exp = int(parse_qs(parsed.query)["exp"][0])
        sig = parse_qs(parsed.query)["sig"][0]
        expected_sig = hmac.new(
            SECRET.encode(),
            f"{parsed.path}\n{exp}".encode(),
            hashlib.sha256,
        ).hexdigest()
        assert sig == expected_sig, "local sign/verify mismatch"
        print("  Local HMAC sign/verify round-trip: OK")
        return 0

    # ── load tests ───────────────────────────────────────────────────────────
    print(f"\nLoad tests ({TOTAL_REQUESTS} requests, {CONCURRENCY} concurrent):")

    summaries: list[BenchSummary] = []

    # Signed leaves
    signed = sign_url(LEAF_PATH)
    summaries.append(benchmark("signed leaf", lambda: signed, 200))

    # Pinned public
    pinned = unsigned_url(PINNED_PATH)
    summaries.append(benchmark("unsigned pinned", lambda: pinned, 200))

    print()
    for s in summaries:
        print(s.report())

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
