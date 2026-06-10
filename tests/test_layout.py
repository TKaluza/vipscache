from pathlib import PurePosixPath

from imgcache.layout import CacheLayout


def test_layout_shards_nodes_and_leaves(tmp_path):
    layout = CacheLayout(tmp_path)

    assert layout.node_relpath("abcdef") == PurePosixPath("nodes/ab/abcdef.v")
    assert layout.leaf_relpath("123456", "webp") == PurePosixPath("leaves/12/123456.webp")
    assert layout.node_path("abcdef") == tmp_path / "nodes" / "ab" / "abcdef.v"
