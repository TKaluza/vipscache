"""Container mount probe: /storage has library/ and cache/; /library is a second bind."""

import json
from pathlib import Path

from vipscache import VipsCacheClient, VipsCacheError
from vipscache.hash import file_id

source = Path("/storage/library/source.ppm")
source.parent.mkdir(parents=True, exist_ok=True)
source.write_bytes(b"P6\n2 2\n255\n" + b"\xff\x00\x00" * 4)
client = VipsCacheClient("/storage/cache")
digest = file_id(source)
linked = client.link(source, file_id=digest)
assert source.stat().st_ino == linked.path().stat().st_ino
client.unlink(digest)
try:
    client.link("/library/source.ppm", file_id=digest)
except VipsCacheError as error:
    assert error.type == "CrossDeviceLink"
else:
    raise AssertionError(
        "separate bind mounts unexpectedly permit a hardlink; record this platform's result"
    )
print(json.dumps({"common_parent_mount": "same inode", "separate_bind_mounts": "CrossDeviceLink"}))
