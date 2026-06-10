from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from imgcache import CacheLayout, MaterializePolicy, Operation, RenderWorker, SourceSpec, WorkerState
from imgcache.spec import ENGINE_VERSION, ImageSpec
from imgcache.state import state_versions


@dataclass(frozen=True)
class FakeImage:
    width: int = 64
    height: int = 48


class CountingExecutor:
    def __init__(self) -> None:
        self.applies = 0
        self.loads = 0
        self.metadata_calls = 0

    def load_source(self, path: Path) -> FakeImage:
        self.loads += 1
        return FakeImage()

    def render_source(self, source: SourceSpec, path: Path, spec) -> FakeImage:
        return self.apply_node(self.load_source(path), spec)

    def load_node(self, path: Path) -> FakeImage:
        raise FileNotFoundError(path)

    def apply_node(self, image: FakeImage, spec) -> FakeImage:
        self.applies += 1
        if "width" in spec.operation.params:
            return replace(image, width=int(spec.operation.params["width"]))
        return image

    def write_node(self, image: FakeImage, path: Path) -> None:
        path.write_bytes(b"node")

    def write_leaf(self, image: FakeImage, spec, path: Path) -> None:
        path.write_bytes(b"leaf")

    def metadata(self, image: FakeImage) -> dict:
        self.metadata_calls += 1
        return {"height": image.height, "mode": "RGB", "width": image.width}


def make_state(tmp_path: Path, **versions: str) -> WorkerState:
    payload = state_versions(engine_version=ENGINE_VERSION, libvips="")
    payload.update(versions)
    return WorkerState(tmp_path / "state", versions=payload, map_size_mb=16)


def test_worker_state_memoizes_measure_metadata(tmp_path):
    state = make_state(tmp_path)
    executor = CountingExecutor()
    worker = RenderWorker(CacheLayout(tmp_path / "cache"), executor=executor, state=state)
    spec = ImageSpec(SourceSpec("f" * 32), (Operation("scale", {"width": 20}),))

    assert worker.measure(spec) == {"height": 48, "mode": "RGB", "width": 20}
    assert worker.measure(spec) == {"height": 48, "mode": "RGB", "width": 20}

    assert executor.applies == 1
    assert executor.metadata_calls == 1
    inspection = state.inspect(spec.parent_key)
    assert inspection["record"]["requests"] == 2
    state.close()


def test_worker_state_wipes_on_version_change(tmp_path):
    state = make_state(tmp_path, state_schema="old")
    state.put_meta("node-key", {"width": 10})
    state.close()

    next_state = make_state(tmp_path)

    assert next_state.get_meta("node-key") is None
    next_state.close()


def test_worker_state_records_materialize_stats_and_edges(tmp_path):
    state = make_state(tmp_path)
    executor = CountingExecutor()
    worker = RenderWorker(CacheLayout(tmp_path / "cache"), executor=executor, state=state)
    spec = ImageSpec.build(
        SourceSpec("a" * 32),
        [Operation("scale", {"width": 20}, MaterializePolicy.FORCE)],
        Operation("encode", {"format": "png"}),
    )

    path = worker.materialize(spec)

    assert path.read_bytes() == b"leaf"
    top = state.top_nodes(1)
    assert top[0]["key"] == spec.nodes[0].key
    assert top[0]["builds"] == 1
    leaf = state.inspect(spec.leaf.key)
    assert leaf["record"]["materializations"] == 1
    parent = state.inspect(spec.source.file_id)
    assert parent["children"] == [
        {
            "key": spec.nodes[0].key,
            "name": "scale",
            "params": {"width": 20},
        }
    ]
    state.close()


def test_worker_state_errors_do_not_break_render(tmp_path):
    state = make_state(tmp_path)
    state.close()
    executor = CountingExecutor()
    worker = RenderWorker(CacheLayout(tmp_path / "cache"), executor=executor, state=state)
    spec = ImageSpec(SourceSpec("f" * 32), (Operation("scale", {"width": 20}),))

    assert worker.measure(spec)["width"] == 20
