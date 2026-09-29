from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vipscache.hash import canonical_json
from vipscache.spec import ImageSpec

STATE_SCHEMA_VERSION = "1"

_LOGGER = logging.getLogger("vipscache.state")


class WorkerState:
    def __init__(
        self,
        state_dir: Path,
        *,
        map_size_mb: int = 1024,
        versions: dict[str, str],
    ) -> None:
        try:
            import lmdb
        except ImportError as error:
            raise RuntimeError("WorkerState requires lmdb; install vipscache[worker].") from error

        self._lmdb = lmdb
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "meta.lmdb"
        self.map_size = map_size_mb * 2**20
        self.versions = dict(versions)
        self._logged_errors: set[str] = set()
        self._env = self._open_env()
        self._ensure_versions()

    def get_meta(self, pipeline_key: str) -> dict[str, Any] | None:
        try:
            with self._env.begin() as txn:
                value = txn.get(_key("meta", pipeline_key))
            return _loads(value) if value is not None else None
        except Exception as error:
            self._log_once("get_meta", error)
            return None

    def put_meta(self, pipeline_key: str, meta: dict[str, Any]) -> None:
        try:
            with self._env.begin(write=True) as txn:
                txn.put(_key("meta", pipeline_key), canonical_json(meta))
        except Exception as error:
            self._log_once("put_meta", error)

    def record_identify(self, spec: ImageSpec) -> None:
        try:
            now = time.time()
            with self._env.begin(write=True) as txn:
                record = _loads(txn.get(_key("stats:node", spec.parent_key))) or {}
                record["requests"] = int(record.get("requests", 0)) + 1
                record["last_requested"] = now
                txn.put(_key("stats:node", spec.parent_key), canonical_json(record))
        except Exception as error:
            self._log_once("record_identify", error)

    def record_materialize(self, spec: ImageSpec, built_node_keys: Sequence[str]) -> None:
        try:
            if spec.encode is None:
                return
            now = time.time()
            built = set(built_node_keys)
            with self._env.begin(write=True) as txn:
                leaf = _loads(txn.get(_key("stats:leaf", spec.leaf.key))) or {}
                leaf["materializations"] = int(leaf.get("materializations", 0)) + 1
                leaf["last_materialized"] = now
                txn.put(_key("stats:leaf", spec.leaf.key), canonical_json(leaf))

                for node_key in built:
                    record = _loads(txn.get(_key("stats:node", node_key))) or {}
                    record["builds"] = int(record.get("builds", 0)) + 1
                    record["last_built"] = now
                    txn.put(_key("stats:node", node_key), canonical_json(record))

                for node in spec.nodes:
                    txn.put(
                        _edge_key(node.parent_key, node.key),
                        canonical_json(
                            {
                                "name": node.operation.name,
                                "params": node.operation.params,
                            }
                        ),
                    )
        except Exception as error:
            self._log_once("record_materialize", error)

    def top_nodes(self, n: int) -> list[dict[str, Any]]:
        try:
            rows: list[dict[str, Any]] = []
            for key, record in self._scan("stats:node:"):
                node_key = key.removeprefix("stats:node:")
                rows.append({"key": node_key, **record})
            rows.sort(key=lambda row: int(row.get("builds", 0)), reverse=True)
            return rows[: max(0, n)]
        except Exception as error:
            self._log_once("top_nodes", error)
            return []

    def inspect(self, key: str) -> dict[str, Any]:
        try:
            with self._env.begin() as txn:
                node = _loads(txn.get(_key("stats:node", key)))
                leaf = _loads(txn.get(_key("stats:leaf", key)))
            children = [
                {"key": edge_key.removeprefix(f"edge:{key}:"), **edge}
                for edge_key, edge in self._scan(f"edge:{key}:")
            ]
            return {
                "children": children,
                "key": key,
                "record": node or leaf or {},
                "record_type": "node" if node is not None else "leaf" if leaf is not None else None,
            }
        except Exception as error:
            self._log_once("inspect", error)
            return {"children": [], "key": key, "record": {}, "record_type": None}

    def close(self) -> None:
        try:
            self._env.close()
        except Exception as error:
            self._log_once("close", error)

    def _open_env(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self._lmdb.open(
            str(self.path),
            map_size=self.map_size,
            metasync=False,
            subdir=True,
            sync=False,
            writemap=False,
        )

    def _ensure_versions(self) -> None:
        stored = None
        try:
            with self._env.begin() as txn:
                stored = txn.get(b"env:versions")
            if stored is not None and _loads(stored) == self.versions:
                return
            self._env.close()
            shutil.rmtree(self.path, ignore_errors=True)
            self._env = self._open_env()
            with self._env.begin(write=True) as txn:
                txn.put(b"env:versions", canonical_json(self.versions))
        except Exception:
            self._env.close()
            shutil.rmtree(self.path, ignore_errors=True)
            self._env = self._open_env()
            with self._env.begin(write=True) as txn:
                txn.put(b"env:versions", canonical_json(self.versions))

    def _scan(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        prefix_bytes = prefix.encode("utf-8")
        rows: list[tuple[str, dict[str, Any]]] = []
        with self._env.begin() as txn:
            cursor = txn.cursor()
            if not cursor.set_range(prefix_bytes):
                return rows
            for raw_key, raw_value in cursor:
                if not raw_key.startswith(prefix_bytes):
                    break
                rows.append((raw_key.decode("utf-8"), _loads(raw_value) or {}))
        return rows

    def _log_once(self, operation: str, error: Exception) -> None:
        if operation in self._logged_errors:
            return
        self._logged_errors.add(operation)
        _LOGGER.warning("worker state %s failed: %s", operation, error, exc_info=True)


def state_versions(*, engine_version: str, libvips: str = "") -> dict[str, str]:
    return {
        "engine_version": engine_version,
        "libvips": libvips,
        "state_schema": STATE_SCHEMA_VERSION,
    }


def _key(prefix: str, key: str) -> bytes:
    return f"{prefix}:{key}".encode("utf-8")


def _edge_key(parent_key: str, child_key: str) -> bytes:
    return f"edge:{parent_key}:{child_key}".encode("utf-8")


def _loads(value: bytes | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return json.loads(value.decode("utf-8"))
