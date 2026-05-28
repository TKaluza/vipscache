# Image-/Page-Derivative-Engine — Architekturplan (v4)

Eine gemeinsame Derivative-Engine erzeugt aus stabilen Eingaben reproduzierbare Bild-, Seiten- und Ausschnittsvarianten. Der Cache ist content-addressiert, liegt auf einem geteilten Volume und wird durch einen Render-Worker befüllt. Thin Clients lesen Cache-Hits direkt aus dem Dateisystem und triggern den Worker nur bei Misses.

Der zentrale Gedanke ist kein flacher „Original → Derivat“-Cache, sondern ein **unveränderlicher Derivations-DAG**: Jede Operation erzeugt einen neuen Knoten, dessen Key aus `parent_key + operation + params + engine_version` entsteht. Zwischenknoten können als `.v` materialisiert werden, wenn sie mehrfach genutzt werden, teuer sind oder vom Nutzer explizit erzwungen werden.

---

## Zielbild

```text
Original / PDF / Bild
   ↓
file_id = xxh3-64(content)
   ↓
DAG-Knoten: render, rotate, flip, crop, scale, ...
   ↓
optionale .v-Zwischenknoten
   ↓
Leaf-Knoten: encode(webp/png/jpg/avif/...)
   ↓
fertige Cache-Datei
```

```text
Thin Client  ──(Hit)──>  open(pfad)                         kein Worker, kein DB
Thin Client  ──(Miss)─>  worker.materialize(spec) ──> Pfad   Worker baut fehlende Kette
Render-Worker ───────────> rendert, schreibt atomar           libvips lebt nur hier
```

Der Worker ist alleiniger Renderer und alleiniger Schreiber. Die Clients schreiben nie. Geteilt werden nur Specs, Keys, Pfade und Dateien — keine `pyvips.Image`-Objekte oder libvips-Pointer.

---

## Grundprinzip: unveränderlicher Derivations-DAG

Jeder Knoten ist eine reine Funktion seines Parents und seiner Operation:

```text
node_key = xxh3-64(parent_key | operation | canonical_params | engine_version)
```

Der `file_id` des Originals ist ebenfalls `xxh3-64` über den Dateiinhalt. Das reicht für ein internes, nicht-adversariales System. Es gibt keine Sonderlogik für unterschiedliche Kollisionsklassen; alle Keys sind gleich behandelt.

Beispiel:

```text
file_id
  └─ render(page=1, dpi=75, colorspace=srgb)        → base.v
      └─ crop(x,y,w,h)                              → crop.v       optional
          └─ fast_rotate(90)                        → crop_rot.v   optional
              ├─ encode(webp, q=82)                 → .webp
              └─ encode(png)                        → .png
```

Damit ist die bisherige „Seitenbasis“ nur ein Spezialfall eines allgemeinen Knotens.

---

## Kanonische Spec

Die Spec muss eindeutig, sortiert und reproduzierbar sein. Alle Parameter, die das Ergebnis beeinflussen, müssen im Key landen:

```text
engine_version
source / parent_key
operation name
operation params
output type
```

Die Operationsreihenfolge ist **nicht frei interpretierbar**, sondern Teil des Modells. Sonst entstehen nicht reproduzierbare Ergebnisse:

```text
crop → rotate ≠ rotate → crop
scale → crop ≠ crop → scale
```

Eine sinnvolle kanonische Kette wäre:

```text
source
  → render
  → normalize / colorspace / alpha handling
  → fast_rotate / flip
  → crop
  → scale
  → optional arbitrary rotate
  → encode
```

Die genaue Reihenfolge kann projektabhängig anders sein, muss aber stabil festgelegt werden.

---

## Operationen

Die Engine sollte diese Operationstypen sauber unterscheiden:

```text
Source:
  file_id, original_path, mime, optional document metadata

Render:
  page
  dpi
  width
  height
  longest_edge
  scale_factor
  colorspace

Geometry:
  fast_rotate: 0/90/180/270
  rotate: beliebiger Winkel
  flip / flop
  crop: x/y/w/h
  scale / resize

Encode:
  format: webp/png/jpg/avif/tif/...
  quality
  lossless
  compression
  background / alpha handling
```

Bei Rendergrößen sollten widersprüchliche Angaben vermieden werden. Also nicht gleichzeitig `dpi`, `width`, `height`, `longest_edge` und `scale_factor` als gleichberechtigte Ziele zulassen, sondern genau eine Größenstrategie pro Renderknoten.

---

## Materialisierung von Zwischenknoten

Zwischenknoten können als `.v` gespeichert werden. Das ist der Mechanismus, mit dem libvips-Ergebnisse über Requests, Prozesse und Container hinweg geteilt werden.

Materialisierung ist eine Policy pro Knoten:

```text
never  = nicht als .v speichern; nur bis zum Leaf streamen
auto   = Engine entscheidet nach Heuristik
force  = diesen Knoten als .v erzeugen
pin    = diesen Knoten als .v erzeugen und von normaler Eviction ausnehmen
```

Die Nutzer- oder Anwendungsebene kann damit Effizienz bewusst steuern:

```text
- Diese PDF-Seite in 75 DPI brauche ich oft → render(...).materialize(force)
- Diesen Crop brauche ich in WebP und PNG → crop(...).materialize(force/auto)
- Dieses Derivat ist einmalig → keine Zwischenstufe speichern
```

Faustregel:

```text
Materialisieren, wenn:
  - mindestens zwei Leaves denselben Knoten teilen,
  - der Knoten teuer ist,
  - der Nutzer ihn explizit erzwingt,
  - oder der Knoten als stabile Arbeitsbasis dienen soll.

Nicht materialisieren, wenn:
  - er nur ein einziges billiges Leaf füttert,
  - die .v-Datei größer wäre als der Nutzen,
  - oder der Cache unter Platzdruck steht.
```

---

## Worker-Strategie: rückwärts suchen, vorwärts bauen

Bei einem Cache-Miss des gewünschten Leafs wird nicht stumpf alles neu gerechnet. Der Worker betrachtet die komplette Kette:

```text
Leaf angefragt
  ↓
Dependency-Kette rückwärts prüfen
  ↓
tiefsten vorhandenen Parent finden
  ↓
von dort vorwärts bauen
  ↓
nur gewünschte / notwendige Knoten materialisieren
```

Beispiel:

```text
file_id
  └─ render(page=1,dpi=75) → base.v  existiert
      └─ crop(...)         → fehlt
          └─ encode(webp)  → fehlt
```

Dann startet der Worker bei `base.v`, erzeugt den Crop und danach das WebP. Wenn die Crop-Policy `force`, `pin` oder `auto` mit positiver Heuristik ist, wird zusätzlich `crop.v` gespeichert.

---

## Beispiel A/B/C/D

### A braucht Seite 1 mit 75 DPI als WebP

```text
file_id
  └─ render(page=1,dpi=75)      → base_75.v
      └─ encode(webp,q=82)      → a.webp
```

Wenn `base_75.v` noch nicht existiert, wird es erzeugt, falls die Policy das vorsieht.

### B braucht Seite 1 mit 100 DPI als WebP

```text
file_id
  └─ render(page=1,dpi=100)     → base_100.v
      └─ encode(webp,q=82)      → b.webp
```

Das ist ein anderer Renderknoten als 75 DPI. Die 75-DPI-Basis wird nicht verwendet.

### C braucht Seite 1 mit 75 DPI, gecroppt als WebP

```text
file_id
  └─ render(page=1,dpi=75)      → base_75.v
      └─ crop(x,y,w,h)          → crop.v optional
          └─ encode(webp,q=82)  → c.webp
```

C kann `base_75.v` von A verwenden.

### D braucht denselben Crop wie C, aber als PNG

```text
file_id
  └─ render(page=1,dpi=75)      → base_75.v
      └─ crop(x,y,w,h)          → crop.v optional
          └─ encode(png)        → d.png
```

Wenn `crop.v` durch C materialisiert wurde, muss D nur noch encoden. Wenn nicht, nutzt D immerhin `base_75.v` und berechnet den Crop neu.

---

## `copy_memory()` als lokale Multi-Output-Optimierung

`copy_memory()` ist kein Architektur-Cache und teilt nichts über Prozesse oder Container hinweg. Es ist nur ein lokaler Fast-Path innerhalb eines Worker-Laufs.

Sinnvoll ist es, wenn derselbe Worker in einem Auftrag mehrere Leaves aus derselben Pipeline erzeugt:

```text
base.v
  → crop
  → rotate
  → copy_memory()
  ├─ encode(webp)
  └─ encode(png)
```

Dadurch wird die gemeinsame Vorarbeit einmal ausgewertet, und danach werden nur die Encodes getrennt ausgeführt.

Faustregel:

```text
Mehrere Outputs im selben Worker-Lauf:
  copy_memory() kann sinnvoll sein, wenn die Pixelgröße begrenzt ist.

Mehrere Outputs über getrennte Requests/Prozesse/Container:
  .v-Zwischenknoten materialisieren.

Nur ein Output:
  kein copy_memory(); libvips streamen lassen.
```

Deshalb bleibt `copy_memory()` im Plan, aber nur als optionale Optimierung mit Speicherlimit.

---

## Cache-Layout

Der Cache kann weiterhin rein dateibasiert bleiben:

```text
/shared/cache/
  nodes/
    3a/3af9...c1.v          # materialisierter DAG-Knoten
    c7/c7d1...a4.v
  leaves/
    7b/7b2e...80.webp       # Auslieferformat
    e4/e41a...22.png
  pinned/
    ...                     # optional separat oder über Policy markiert
```

Alternativ können `nodes/` und `leaves/` auch beide unter einem gemeinsamen `objects/` liegen, solange die Endung und die Spec den Typ eindeutig machen.

Der Pfad wird aus dem Key berechnet und gesharded:

```text
objects/<first-two-hex>/<key>.<ext>
```

Pfade werden in POSIX-Form abgeleitet und erst am Dateisystemrand in native Pfade übersetzt.

---

## Architektur: Thin Client + Render-Worker

```text
┌──────────────────────────────┐         ┌──────────────────────────────────┐
│  Thin Client                  │         │  Render-Worker                    │
│  - rechnet Keys lokal aus     │  Miss   │  - hat libvips                    │
│  - open(pfad) → Hit           │ ──────> │  - analysiert die DAG-Kette        │
│  - bei Miss: Worker fragen    │         │  - backtrackt zum vorhandenen Node │
│  - keine libvips-Abhängigkeit │ <────── │  - baut fehlende Knoten vorwärts   │
│  - schreibt nie in Cache      │  Pfad   │  - schreibt atomar                 │
└──────────────────────────────┘         └──────────────────────────────────┘
            │                                           │
            └──────────── geteiltes Volume ─────────────┘
```

Die Client-Schnittstelle bleibt einfach:

```text
get(spec) → File/Pfad/Stream
```

Wie der Worker intern entscheidet, ob ein Knoten gestreamt, als `.v` geschrieben, gepinnt oder verworfen wird, bleibt für den Client unsichtbar.

---

## Kein zwingender Index

Das Dateisystem kann weiterhin der primäre Index sein:

```text
Existenz-Prüfung:     open() / stat()
Pfad:                 aus dem Key berechnet
Dedup im Worker:      inflight map: key → Future
Schreiben:            temp-Datei + atomarer replace
Eviction-Signal:      atime/mtime oder später worker-interne Metadaten
```

Ein Metadaten-Index ist optional und worker-intern:

```text
Optional später:
  key → width/height/mime
  key → parent_key
  key → operation summary
  key → pinned/materialize policy
  key → usage count
  key → created_at / last_access
```

Die Client-Schnittstelle sollte davon nicht abhängig werden.

---

## libvips-Rolle

libvips ist die Ausführungsmaschine, nicht der langfristige Cache.

```text
libvips:
  - rendert PDF-Seiten
  - transformiert Bilder
  - streamt speicherschonend
  - kann innerhalb eines Jobs tilecache/copy_memory nutzen

DAG-Dateien:
  - teilen Zwischenergebnisse über Requests/Prozesse/Container hinweg

Dateisystem/OS:
  - liefert fertige Dateien
  - profitiert vom Page Cache
```

Der globale libvips-Operation-Cache sollte klein bleiben oder bewusst deaktiviert werden. Die langfristige Wiederverwendung entsteht durch `.v`-Knoten und fertige Leaves.

---

## Leistungsbegrenzung

```text
Worker-Pool:
  max_workers = max. parallele Render-Jobs

libvips:
  concurrency_set(n) = Threads pro Operation
  cache_set_max_mem(...) = interner Cache klein halten

Spec-Grenzen:
  max DPI
  max Output-Pixel
  max Input-Seitenfläche
  max copy_memory-Bytes
  erlaubte Formate

OS / Container:
  CPUQuota / cpus
  MemoryMax / mem_limit
  IO-Limits, falls nötig
```

Die Poolgröße ist das harte globale Gate. libvips-Threads begrenzen nur die Parallelität innerhalb eines einzelnen Jobs.

---

## Eviction

Eviction kann weiter dateibasiert bleiben:

```text
TTL nach atime/mtime
max. Cache-Größe
älteste unpinned Dateien zuerst
pinned Nodes nie automatisch löschen
```

Bei DAG-Knoten ist wichtig:

```text
Ein Leaf kann gelöscht werden, ohne Parent-Nodes zu löschen.
Ein Parent-Node kann gelöscht werden, solange er reproduzierbar ist.
Pinned Nodes bleiben erhalten.
```

Es muss kein referenzieller Garbage Collector sein, solange alles reproduzierbar ist. Eviction darf aggressiv sein; fehlende Knoten werden bei Bedarf neu gebaut.

---

## Korrektheit & Fallstricke

```text
Hash:
  xxh3-64 für file_id, node_key und leaf_key.
  Internes, nicht-adversariales System; Kollisionen werden akzeptiert.

Kanonische Spec:
  stabile Reihenfolge, stabile Parameternamen, stabile Einheiten.

Engine-Version:
  Teil jedes Keys; Änderungen an Rendering-Logik invalidieren alte Ergebnisse automatisch.

Atomisches Schreiben:
  temp-Datei + os.replace auf demselben Dateisystem.

Race beim Lesen:
  open() statt exists()+open(); FileNotFoundError → Worker fragen.

Pfade:
  POSIX relativ ableiten, nativ erst am Dateisystemrand.

Worker:
  alleiniger Schreiber; Clients schreiben nie.

libvips:
  keine pyvips.Image-Objekte über Prozess-/Containergrenzen teilen.
```

---

## Kurzform

```text
1.  Originale bekommen eine file_id per xxh3-64(content).
2.  Die Engine modelliert Ableitungen als unveränderlichen DAG.
3.  Jeder Knoten-Key entsteht aus parent_key + operation + canonical_params + engine_version.
4.  Leaf-Knoten sind Auslieferformate wie webp/png/jpg/avif.
5.  Zwischenknoten können als .v materialisiert werden.
6.  Materialisierung ist Policy: never / auto / force / pin.
7.  Der Nutzer kann .v-Zwischenknoten gezielt triggern.
8.  Bei Miss sucht der Worker rückwärts den tiefsten vorhandenen Knoten.
9.  Von dort baut er die fehlende Kette vorwärts.
10. Thin Clients rechnen Keys/Pfade lokal und lesen Hits direkt.
11. Der Worker ist alleiniger Renderer und Schreiber.
12. libvips bleibt Ausführungsmaschine, nicht prozessübergreifender Cache.
13. copy_memory() bleibt als optionale Multi-Output-Optimierung im selben Worker-Lauf.
14. Cache-Dateien werden atomar veröffentlicht und dateibasiert evicted.
15. Ressourcen werden über Worker-Pool, libvips-Concurrency und Spec-Grenzen kontrolliert.
```
