from __future__ import annotations

import os

from vipscache.eviction import evict_ttl
from vipscache.layout import CacheLayout


def test_ttl_eviction_deletes_old_nodes_and_leaves_but_not_pinned(tmp_path):
    layout = CacheLayout(tmp_path / "cache")
    node = layout.node_path("aa1111")
    leaf = layout.leaf_path("bb2222", "png")
    pinned = layout.node_path("cc3333", pinned=True)
    for path in (node, leaf, pinned):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        os.utime(path, (100, 100))

    report = evict_ttl(layout, ttl_seconds=10, now=200)

    assert report.deleted_files == 2
    assert not node.exists()
    assert not leaf.exists()
    assert pinned.exists()
