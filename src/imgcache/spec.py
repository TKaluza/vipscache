from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Literal

from imgcache.hash import file_id as compute_file_id
from imgcache.hash import hash_canonical

ENGINE_VERSION = "imgcache-v1"


class MaterializePolicy(StrEnum):
    NEVER = "never"
    FORCE = "force"
    PIN = "pin"


@dataclass(frozen=True)
class SourceSpec:
    file_id: str
    original_path: str
    mime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str, *, mime: str | None = None) -> "SourceSpec":
        return cls(
            file_id=compute_file_id(path),
            original_path=PurePosixPath(path).as_posix(),
            mime=mime,
        )

    def to_key_data(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "metadata": self.metadata,
            "mime": self.mime,
            "original_path": self.original_path,
        }

    def to_payload(self) -> dict[str, Any]:
        return self.to_key_data()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SourceSpec":
        return cls(
            file_id=payload["file_id"],
            original_path=payload["original_path"],
            mime=payload.get("mime"),
            metadata=payload.get("metadata", {}),
        )

    def is_pdf(self) -> bool:
        if self.mime == "application/pdf":
            return True
        return self.original_path.lower().endswith(".pdf")


@dataclass(frozen=True)
class Operation:
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    materialize: MaterializePolicy = MaterializePolicy.NEVER

    def __post_init__(self) -> None:
        object.__setattr__(self, "params", dict(sorted(self.params.items())))
        if not isinstance(self.materialize, MaterializePolicy):
            object.__setattr__(self, "materialize", MaterializePolicy(self.materialize))

    def to_key_data(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "params": self.params,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "materialize": self.materialize.value,
            "name": self.name,
            "params": self.params,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Operation":
        return cls(
            name=payload["name"],
            params=payload.get("params", {}),
            materialize=MaterializePolicy(payload.get("materialize", MaterializePolicy.NEVER.value)),
        )


@dataclass(frozen=True)
class NodeSpec:
    parent_key: str
    operation: Operation
    engine_version: str = ENGINE_VERSION

    @property
    def key(self) -> str:
        return hash_canonical(self.to_key_data())

    @property
    def materialize(self) -> MaterializePolicy:
        return self.operation.materialize

    def with_materialize(self, policy: MaterializePolicy) -> "NodeSpec":
        return replace(
            self,
            operation=replace(self.operation, materialize=policy),
        )

    def to_key_data(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "operation": self.operation.to_key_data(),
            "parent_key": self.parent_key,
            "type": "node",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "operation": self.operation.to_payload(),
            "parent_key": self.parent_key,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NodeSpec":
        return cls(
            parent_key=payload["parent_key"],
            operation=Operation.from_payload(payload["operation"]),
            engine_version=payload.get("engine_version", ENGINE_VERSION),
        )


@dataclass(frozen=True)
class EncodeSpec:
    parent_key: str
    format: Literal["webp", "png", "jpg", "jpeg", "avif", "tif", "tiff"]
    params: dict[str, Any] = field(default_factory=dict)
    engine_version: str = ENGINE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", self.format.lower())
        object.__setattr__(self, "params", dict(sorted(self.params.items())))

    @property
    def key(self) -> str:
        return hash_canonical(self.to_key_data())

    @property
    def extension(self) -> str:
        return "jpg" if self.format == "jpeg" else self.format

    def to_key_data(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "format": self.format,
            "operation": "encode",
            "params": self.params,
            "parent_key": self.parent_key,
            "type": "leaf",
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "format": self.format,
            "params": self.params,
            "parent_key": self.parent_key,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EncodeSpec":
        return cls(
            parent_key=payload["parent_key"],
            format=payload["format"],
            params=payload.get("params", {}),
            engine_version=payload.get("engine_version", ENGINE_VERSION),
        )


@dataclass(frozen=True)
class DerivativeSpec:
    source: SourceSpec
    nodes: tuple[NodeSpec, ...]
    leaf: EncodeSpec

    @classmethod
    def build(
        cls,
        source: SourceSpec,
        operations: list[Operation] | tuple[Operation, ...],
        encode: Operation | EncodeSpec,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> "DerivativeSpec":
        parent_key = source.file_id
        nodes: list[NodeSpec] = []

        for operation in operations:
            node = NodeSpec(parent_key, operation, engine_version)
            nodes.append(node)
            parent_key = node.key

        if isinstance(encode, EncodeSpec):
            leaf = encode
        else:
            if encode.name != "encode":
                raise ValueError("final operation must be named 'encode'")
            output_format = encode.params.get("format")
            if not output_format:
                raise ValueError("encode operation requires a 'format' param")
            params = {k: v for k, v in encode.params.items() if k != "format"}
            leaf = EncodeSpec(parent_key, output_format, params, engine_version)

        return cls(source=source, nodes=tuple(nodes), leaf=leaf)

    @classmethod
    def canonical(
        cls,
        source: SourceSpec,
        operations: list[Operation] | tuple[Operation, ...],
        encode: Operation | EncodeSpec,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> "DerivativeSpec":
        """Build a derivative using the project's stable operation order."""
        return cls.build(
            source,
            canonicalize_operations(source, operations),
            encode,
            engine_version=engine_version,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "leaf": self.leaf.to_payload(),
            "nodes": [node.to_payload() for node in self.nodes],
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DerivativeSpec":
        return cls(
            source=SourceSpec.from_payload(payload["source"]),
            nodes=tuple(NodeSpec.from_payload(node) for node in payload.get("nodes", [])),
            leaf=EncodeSpec.from_payload(payload["leaf"]),
        )


def canonicalize_operations(
    source: SourceSpec,
    operations: list[Operation] | tuple[Operation, ...],
) -> tuple[Operation, ...]:
    buckets: dict[int, list[Operation]] = {stage: [] for stage in range(6)}

    for operation in operations:
        stage = _operation_stage(source, operation)
        if stage is None:
            continue
        buckets[stage].append(operation)

    ordered: list[Operation] = []
    for stage in range(6):
        ordered.extend(buckets[stage])
    return tuple(ordered)


def _operation_stage(source: SourceSpec, operation: Operation) -> int | None:
    if operation.name == "render":
        return 0 if source.is_pdf() else None
    if operation.name in {"normalize", "colorspace", "alpha"}:
        return 1
    if operation.name in {"fast_rotate", "flip", "flop"}:
        return 2
    if operation.name == "crop":
        return 3
    if operation.name in {"scale", "resize"}:
        return 4
    if operation.name == "rotate":
        return 5
    raise ValueError(f"unsupported canonical operation: {operation.name}")
