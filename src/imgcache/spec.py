from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import cached_property
from typing import Any, Literal, Self

from imgcache.hash import hash_canonical

ENGINE_VERSION = "imgcache-v1"


class MaterializePolicy(StrEnum):
    NEVER = "never"
    FORCE = "force"
    PIN = "pin"


@dataclass(frozen=True)
class SourceSpec:
    file_id: str
    mime: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_key_data(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "metadata": self.metadata,
            "mime": self.mime,
        }

    def __hash__(self) -> int:
        return hash(hash_canonical(self.to_key_data()))

    def to_payload(self) -> dict[str, Any]:
        return self.to_key_data()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls(
            file_id=payload["file_id"],
            mime=payload.get("mime"),
            metadata=payload.get("metadata", {}),
        )

    def is_pdf(self) -> bool:
        if self.mime == "application/pdf":
            return True
        filename = str(self.metadata.get("filename", ""))
        if filename.lower().endswith(".pdf"):
            return True
        return False


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
    def from_payload(cls, payload: dict[str, Any]) -> Self:
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

    @cached_property
    def key(self) -> str:
        return hash_canonical(self.to_key_data())

    @property
    def materialize(self) -> MaterializePolicy:
        return self.operation.materialize

    def with_materialize(self, policy: MaterializePolicy) -> Self:
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
    def from_payload(cls, payload: dict[str, Any]) -> Self:
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

    @cached_property
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
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        return cls(
            parent_key=payload["parent_key"],
            format=payload["format"],
            params=payload.get("params", {}),
            engine_version=payload.get("engine_version", ENGINE_VERSION),
        )


@dataclass(frozen=True)
class ImageSpec:
    source: SourceSpec
    operations: tuple[Operation, ...] = ()
    encode: EncodeSpec | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        if self.encode is not None:
            object.__setattr__(self, "encode", self._encode_with_current_parent(self.encode))

    @cached_property
    def nodes(self) -> tuple[NodeSpec, ...]:
        return self._nodes_for(self._engine_version())

    def _nodes_for(self, engine_version: str) -> tuple[NodeSpec, ...]:
        parent_key = self.source.file_id
        nodes: list[NodeSpec] = []
        for operation in self.operations:
            node = NodeSpec(parent_key, operation, engine_version)
            nodes.append(node)
            parent_key = node.key
        return tuple(nodes)

    @cached_property
    def parent_key(self) -> str:
        return self._parent_key_for(self._engine_version())

    def _parent_key_for(self, engine_version: str) -> str:
        nodes = self._nodes_for(engine_version)
        if nodes:
            return nodes[-1].key
        return self.source.file_id

    @property
    def leaf(self) -> EncodeSpec:
        if self.encode is None:
            raise ValueError("ImageSpec has no encode")
        return self._encode_with_current_parent(self.encode)

    @property
    def is_original(self) -> bool:
        return not self.operations and self.encode is None

    @property
    def is_materializable_leaf(self) -> bool:
        return self.encode is not None

    @classmethod
    def build(
        cls,
        source: SourceSpec,
        operations: list[Operation] | tuple[Operation, ...],
        encode: Operation | EncodeSpec,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> Self:
        spec = cls(source=source, operations=tuple(operations))
        return spec.with_encode(encode, engine_version=engine_version)

    @classmethod
    def canonical(
        cls,
        source: SourceSpec,
        operations: list[Operation] | tuple[Operation, ...],
        encode: Operation | EncodeSpec,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> Self:
        """Build an image spec using the project's stable operation order."""
        return cls.build(
            source,
            canonicalize_operations(source, operations),
            encode,
            engine_version=engine_version,
        )

    def with_encode(
        self,
        encode: Operation | EncodeSpec,
        *,
        engine_version: str = ENGINE_VERSION,
    ) -> Self:
        if isinstance(encode, EncodeSpec):
            leaf = replace(encode, parent_key=self._parent_key_for(encode.engine_version))
        else:
            if encode.name != "encode":
                raise ValueError("final operation must be named 'encode'")
            output_format = encode.params.get("format")
            if not output_format:
                raise ValueError("encode operation requires a 'format' param")
            params = {k: v for k, v in encode.params.items() if k != "format"}
            leaf = EncodeSpec(self._parent_key_for(engine_version), output_format, params, engine_version)
        return replace(self, encode=leaf)

    def _encode_with_current_parent(self, encode: EncodeSpec) -> EncodeSpec:
        parent_key = self._parent_key_for(encode.engine_version)
        if encode.parent_key == parent_key:
            return encode
        return replace(encode, parent_key=parent_key)

    def _engine_version(self) -> str:
        if self.encode is not None:
            return self.encode.engine_version
        return ENGINE_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {
            "encode": self._encode_payload(),
            "operations": [operation.to_payload() for operation in self.operations],
            "source": self.source.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Self:
        source = SourceSpec.from_payload(payload["source"])
        operations = tuple(Operation.from_payload(operation) for operation in payload.get("operations", []))
        spec = cls(source=source, operations=operations)
        encode_payload = payload.get("encode")
        if encode_payload is None:
            return spec
        return spec.with_encode(EncodeSpec.from_payload({"parent_key": spec.parent_key, **encode_payload}))

    def _encode_payload(self) -> dict[str, Any] | None:
        if self.encode is None:
            return None
        return {
            "engine_version": self.encode.engine_version,
            "format": self.encode.format,
            "params": self.encode.params,
        }


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
    if operation.name in {"crop", "crop_fraction"}:
        return 3
    if operation.name in {"scale", "resize"}:
        return 4
    if operation.name == "rotate":
        return 5
    raise ValueError(f"unsupported canonical operation: {operation.name}")
