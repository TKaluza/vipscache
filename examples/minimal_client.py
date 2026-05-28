from pathlib import Path

from imgcache import (
    CacheLayout,
    DerivativeSpec,
    Operation,
    OriginalsStore,
    ThinClient,
)
from imgcache.zmq_client import ZmqWorkerClient

cache = CacheLayout("shared/cache")
originals = OriginalsStore("shared/originals")

source = originals.put(Path("/home/tim/Downloads/Invoice-6017BED0-0038.pdf"), mime="application/pdf")
source = type(source)(
    file_id=source.file_id,
    original_path=f"/originals/{source.file_id}",
    mime=source.mime,
)

spec = DerivativeSpec.canonical(
    source,
    [
        Operation("normalize", {"colorspace": "srgb"}),
        Operation("scale", {"longest_edge": 1024}),
    ],
    Operation("encode", {"format": "webp", "quality": 82}),
)

with ZmqWorkerClient("tcp://127.0.0.1:5555") as worker:
    client = ThinClient(cache, worker)
    path = client.get(spec)

print(path)