# Next Step: Bildartige Client-API

## Ziel

Die neue Haupt-API soll sich fuer Nutzer wie ein lazy Bildobjekt anfuehlen:

```python
from PIL import Image

from imgcache import ImgCacheClient

client = ImgCacheClient.zmq(
    root="shared",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    client
    .open("/home/tim/Downloads/example.jpg", mime="image/jpeg")
    .normalize()
    .scale(longest_edge=1024)
    .webp(quality=82)
)

pil = Image.open(preview)
blob = preview.bytes()
```

Async soll dasselbe mentale Modell haben:

```python
from PIL import Image

from imgcache import ImgCacheClient

client = ImgCacheClient.zmq(
    root="shared",
    endpoint="tcp://127.0.0.1:5555",
)

preview = (
    await client.aopen("/home/tim/Downloads/example.jpg", mime="image/jpeg")
).normalize().scale(longest_edge=1024).webp(quality=82)

pil = Image.open(await preview)
blob = await preview.abytes()
```

## Storage-Layout

Client und Worker sollen keine absoluten Originalpfade in Specs austauschen. Beide bekommen einen eigenen lokalen Root, der auf dasselbe geteilte Storage-Volume zeigen kann.

```text
<root>/
  raw/
    <file_id>
  cache/
    nodes/
    pinned/
    leaves/
```

Beispiele:

```python
# Host/client
client = ImgCacheClient.zmq(root="shared", endpoint="tcp://127.0.0.1:5555")
```

```bash
# Worker/container
IMGCACHE_ROOT=/data uv run imgcache-zmq-worker
```

Wenn `shared` im Container als `/data` gemountet ist, sehen Client und Worker dieselben Dateien. Die `SourceSpec` muss dann nur noch `file_id`, `mime` und Metadaten enthalten. Der Worker findet Originale ueber `<worker-root>/raw/<file_id>`.

## Public API

### ImgCacheClient

`ImgCacheClient` wird die neue komfortable Haupt-API. Fuer `ThinClient` und `ZmqWorkerClient` gibt es keine Backward-Compatibility-Pflicht; sie koennen entfernt, umbenannt oder als interne Bausteine weiterverwendet werden.

```python
client = ImgCacheClient.zmq(
    root="shared",
    endpoint="tcp://127.0.0.1:5555",
    request_retries=2,
    timeout_ms=300_000,
)
```

Geplante Methoden:

```python
image = client.open(path, mime=None, metadata=None)
image = await client.aopen(path, mime=None, metadata=None)

path = client.get(spec)
path = await client.aget(spec)
```

`open` und `aopen` nehmen eine lokale Datei in `<root>/raw/<file_id>` auf und liefern ein immutable `CachedImage`.

### CachedImage

`CachedImage` ist eine frozen dataclass und traegt die deklarative Pipeline.

```python
@dataclass(frozen=True)
class CachedImage:
    client: ImgCacheClient
    source: SourceSpec
    operations: tuple[Operation, ...] = ()
    encode: Operation | None = None
```

Jede Aenderung erzeugt ein neues Objekt:

```python
original = client.open("example.jpg", mime="image/jpeg")
small = original.scale(longest_edge=512).webp(quality=82)
large = original.scale(longest_edge=2048).webp(quality=82)
```

Geplante Transformationsmethoden:

```python
image.page(page=1, dpi=144)
image.normalize(colorspace="srgb")
image.scale(longest_edge=1024)
image.resize(width=..., height=...)
image.crop(x=..., y=..., w=..., h=...)
image.rotate(degrees=...)
image.fast_rotate(degrees=...)
image.flip()
image.flop()
```

Geplante Encode-Methoden:

```python
image.webp(quality=82)
image.png()
image.jpg(quality=85)
image.avif(quality=60)
image.tif()
```

Materialisierung:

```python
path = image.path()
path = await image.apath()

with image.open("rb") as handle:
    data = handle.read()

data = image.bytes()
data = await image.abytes()
```

Magic Methods:

```python
def __fspath__(self) -> str:
    return str(self.path())

def __await__(self):
    return self.apath().__await__()
```

Damit funktionieren diese Formen:

```python
Image.open(image)        # sync lazy, blockiert bei Cache-Miss
Image.open(await image)  # async lazy, await liefert Path
```

`bytes(image)` soll nicht Teil der offiziellen API sein. Bytes bleiben explizit ueber `image.bytes()` und `await image.abytes()`.

## Original-Fallback und Encode-Regeln

Ein frisch geoeffnetes `CachedImage` ohne Transformation und ohne Encode steht fuer das Original:

```python
image = client.open("example.jpg", mime="image/jpeg")
Image.open(image)
path = image.path()
```

Bei PDFs bedeutet das: Ohne `.page(...)` ist das Objekt die Original-PDF-Datei, nicht eine gerenderte Bildseite.

```python
pdf = client.open("invoice.pdf", mime="application/pdf")
pdf_path = pdf.path()

page = pdf.page(1, dpi=144).scale(longest_edge=1024).webp(quality=82)
Image.open(page)
```

Sobald Transformationen vorhanden sind, muss vor Materialisierung ein Ausgabeformat gesetzt werden:

```python
client.open("example.jpg").scale(longest_edge=1024).path()
```

soll mit einer klaren Fehlermeldung abbrechen:

```text
transformed CachedImage must choose an output format; call .webp(), .png(), or .jpg()
```

## Spec- und Worker-Aenderungen

`DerivativeSpec` soll als oeffentliches und internes Kernmodell wegfallen. Die zentrale Wahrheit wird eine einzige `ImageSpec`, die Originale, Zwischenpipelines und fertige Derivate beschreiben kann.

```python
@dataclass(frozen=True)
class ImageSpec:
    source: SourceSpec
    operations: tuple[Operation, ...] = ()
    encode: EncodeSpec | None = None
```

`CachedImage.spec` ist immer eine `ImageSpec`:

```python
original = client.open("example.jpg", mime="image/jpeg")
original.spec
# ImageSpec(source=..., operations=(), encode=None)

preview = original.scale(longest_edge=1024)
preview.spec
# ImageSpec(source=..., operations=(scale,), encode=None)

webp = preview.webp(quality=82)
webp.spec
# ImageSpec(source=..., operations=(scale,), encode=EncodeSpec(...))
```

`SourceSpec` enthaelt keine absoluten Originalpfade mehr als notwendige Worker-Information. Ziel:

```python
SourceSpec(
    file_id="ac045e19e0574d13",
    mime="image/jpeg",
    metadata={},
)
```

Der Worker empfaengt ebenfalls `ImageSpec` im ZMQ-Payload. Er bekommt sein Root aus Settings und oeffnet Originale ueber:

```python
root / "raw" / source.file_id
```

Der Client berechnet und liest Cache-Leaves unter:

```python
root / "cache" / "leaves" / ...
```

Der Worker schreibt dieselben relativen Cache-Pfade unter seinem eigenen Root.

Materialisierungsregeln:

- Keine Operationen, kein Encode: Original-Fallback; kein Worker-Request noetig.
- Operationen ohne Encode: ungueltig fuer `path/open/bytes`; klare Fehlermeldung.
- Operationen mit Encode: Worker materialisiert das Leaf.
- Encode ohne Operationen: erlaubt als Re-Encode des Originals.

## Sync/Async Worker-Client

Sync:

- ZMQ-Requests bleiben REQ-basiert.
- Sockets duerfen nicht thread-uebergreifend geteilt werden.
- `ImgCacheClient` soll Endpoint und Optionen speichern, aber Sync-Sockets intern lazy pro Thread oder pro Session verwalten.

Async:

- `aget`, `apath`, `abytes` und `aopen` sollen ohne blockierende ZMQ-Requests laufen.
- Fuer ZMQ bietet sich `zmq.asyncio` an.
- Dateioperationen wie Hashing/Kopieren/Lesen koennen in v1 ueber `asyncio.to_thread(...)` laufen, damit keine neue Async-Datei-Abhaengigkeit noetig ist.

## Umsetzungsschritte

1. Neues Storage-Layout einfuehren:
   - Root-Konzept mit `raw/` und `cache/`.
   - `OriginalsStore` auf `raw/` umstellen oder durch einen klar benannten Raw-Store ersetzen.
   - `CacheLayout` auf `<root>/cache` verwenden oder in ein uebergeordnetes Store-Objekt einbetten.

2. `SourceSpec` vereinfachen:
   - Absolute `original_path` als notwendige Worker-Quelle entfernen.
   - Payloads und Tests auf `file_id`-basierte Raw-Aufloesung umstellen.

3. `DerivativeSpec` durch `ImageSpec` ersetzen:
   - `ImageSpec(source, operations=(), encode=None)` als zentrale Spec einfuehren.
   - Worker-, Client- und ZMQ-Payloads auf `ImageSpec` umstellen.
   - Alte `DerivativeSpec.build/canonical`-Denke entfernen; Canonical Ordering liegt bei `ImageSpec`/`CachedImage`.

4. `ImgCacheClient` einfuehren:
   - Konstruktor/factory `ImgCacheClient.zmq(root, endpoint, ...)`.
   - `open/aopen`, `get/aget`, `path_for` und interne Worker-Session-Logik.

5. `CachedImage` einfuehren:
   - Frozen dataclass mit immutable Pipeline.
   - Transformations- und Encode-Methoden.
   - `path/apath/open/bytes/abytes`, `__fspath__`, `__await__`.
   - Original-Fallback und klare Fehler bei transformierten Images ohne Encode.

6. Worker anpassen:
   - Settings auf `IMGCACHE_ROOT` als primaere Konfiguration erweitern.
   - Originale ueber `<root>/raw/<file_id>` laden.
   - Cache ueber `<root>/cache` schreiben.
   - ZMQ-Erfolg nicht als worker-lokalen absoluten Pfad modellieren; Client berechnet seinen lokalen Zielpfad selbst.

7. Beispiele und Doku aktualisieren:
   - `examples/minimal_client.py` auf `ImgCacheClient` und `CachedImage` umstellen.
   - README-Minimalbeispiel ersetzen.
   - ARCHITECTURE.md auf relatives Root-Layout aktualisieren.

## Tests

Mindestens folgende Tests ergaenzen oder umbauen:

- `client.open(...)` kopiert Original nach `<root>/raw/<file_id>` und liefert ein `CachedImage`.
- `await client.aopen(...)` liefert dasselbe Ergebnis ohne blockierendes API-Design.
- Unveraenderte Originalbilder funktionieren mit `image.path()` und `Image.open(image)`.
- Transformierte Bilder ohne Encode werfen eine klare Fehlermeldung.
- Transformierte Bilder mit `.webp()` oder `.png()` materialisieren ueber den Worker.
- `image.spec` ist immer eine `ImageSpec`, auch fuer Originale ohne Encode.
- Worker-ZMQ-Payloads nutzen `ImageSpec` statt `DerivativeSpec`.
- `await image` liefert einen `Path`, der mit `Image.open(await image)` nutzbar ist.
- `image.bytes()` und `await image.abytes()` lesen die materialisierte Leaf-Datei.
- Client und Worker koennen unterschiedliche absolute Roots verwenden, solange das relative Layout identisch ist.
- ZMQ-Sockets werden nicht ueber Threads geteilt.

## Offene Detailentscheidungen

- Ob `client.open(...)` den MIME-Type automatisch erkennen soll. V1 kann bei explizitem `mime` bleiben.
- Ob sehr PIL-nahe Komfortmethoden spaeter ergaenzt werden sollen; v1 bleibt bei Pipeline- und Materialisierungsfunktionen.
