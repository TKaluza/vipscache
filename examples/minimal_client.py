from pathlib import Path

from vipscache import VipsCacheClient


client = VipsCacheClient.zmq(
    root="shared/vipscache",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    client.register(Path("/home/tim/Downloads/Invoice-6017BED0-0038.pdf"), mime="application/pdf")
    .scale(longest_edge=200)
    .webp(quality=82)
)

print(preview.path())
