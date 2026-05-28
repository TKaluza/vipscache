from pathlib import Path

from imgcache import ImgCacheClient


client = ImgCacheClient.zmq(
    root="shared",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    client.open(Path("/home/tim/Downloads/example.jpg"), mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

print(preview.path())
