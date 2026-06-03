#!/usr/bin/env python3
"""
BKG-Methode: Ortsschild-Positionen aus Gemeindegrenzen × Straßennetz berechnen.

Rechtsgrundlage:
  StVO § 42 i.V.m. VwV-StVO zu Zeichen 310/311:
  An jeder öffentlichen Straße, die eine geschlossene Ortschaft betritt oder
  verlässt, ist ein Ortsschild (Zeichen 310 / 311) aufzustellen.
  Schnittpunkt Gemeindegrenze × Straße = rechtlich korrekte Schildposition.

Datenquellen:
  • BKG VG250 — Amtliche Gemeindegrenzen 1:250.000
    Quelle: © GeoBasis-DE / BKG (2024), Lizenz dl-de/by-2-0
    Dienst: https://sgx.geodatenzentrum.de/wfs_vg250
  • OpenStreetMap via Overpass API — Straßennetz

Vorgehen:
  1. BKG VG250 Gemeindegrenzen als GeoJSON via WFS laden (WGS84)
  2. Spatial Index (shapely STRtree) über alle Gemeindegrenzen aufbauen
  3. Pro Bundesland: Straßen via Overpass holen (trunk → unclassified, keine Autobahn)
  4. Schnittpunkte Straße × Gemeindegrenze berechnen → Schildpositionen
  5. Upsert in Supabase-Tabelle signs_bkg (idempotent)

Tabelle anlegen (einmalig im Supabase SQL Editor ausführen):
  CREATE TABLE IF NOT EXISTS signs_bkg (
      id          TEXT PRIMARY KEY,
      name        TEXT NOT NULL,
      lat         DOUBLE PRECISION NOT NULL,
      lon         DOUBLE PRECISION NOT NULL,
      road_type   TEXT,
      bundesland  TEXT,
      created_at  TIMESTAMPTZ DEFAULT now()
  );
  ALTER TABLE signs_bkg ENABLE ROW LEVEL SECURITY;
  CREATE POLICY "public read signs_bkg" ON signs_bkg
      FOR SELECT TO public USING (true);

Usage:
  SUPABASE_SERVICE_KEY=<service_role_key> python3 scripts/import_signs_bkg.py

Runtime: ca. 60–120 Minuten (abhängig von Overpass-Last).
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from shapely.geometry import shape, LineString, Point, MultiLineString
from shapely.strtree import STRtree

# ── Konfiguration ─────────────────────────────────────────────────────────────

SUPABASE_URL         = "https://slcprtkqkqwgstnyfpus.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BATCH_SIZE = 500

# Straßentypen mit Ortsschild-Pflicht (StVO § 42 VwV)
# motorway ausgeschlossen: Autobahn hat keine Ortsdurchfahrten
ROAD_TYPES = "trunk|primary|secondary|tertiary|unclassified"

# BKG WFS: Gemeinden (vg250_gem), WGS84, seitenweise
BKG_WFS = (
    "https://sgx.geodatenzentrum.de/wfs_vg250"
    "?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES=vg250:vg250_gem"
    "&OUTPUTFORMAT=application/json"
    "&SRSNAME=CRS:84"          # lon/lat Reihenfolge (GeoJSON-konform)
    "&COUNT=2000"
    "&STARTINDEX={start}"
)

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

BUNDESLAENDER = [
    "Baden-Württemberg", "Bayern", "Berlin", "Brandenburg", "Bremen",
    "Hamburg", "Hessen", "Mecklenburg-Vorpommern", "Niedersachsen",
    "Nordrhein-Westfalen", "Rheinland-Pfalz", "Saarland", "Sachsen",
    "Sachsen-Anhalt", "Schleswig-Holstein", "Thüringen",
]

# Amtliche Bundesland-Schlüssel (SN_L aus BKG VG250)
BL_CODES = {
    "01": "Schleswig-Holstein",    "02": "Hamburg",
    "03": "Niedersachsen",         "04": "Bremen",
    "05": "Nordrhein-Westfalen",   "06": "Hessen",
    "07": "Rheinland-Pfalz",       "08": "Baden-Württemberg",
    "09": "Bayern",                "10": "Saarland",
    "11": "Berlin",                "12": "Brandenburg",
    "13": "Mecklenburg-Vorpommern","14": "Sachsen",
    "15": "Sachsen-Anhalt",        "16": "Thüringen",
}

# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def make_id(lat: float, lon: float) -> str:
    """Stabiler ID aus gerundeten Koordinaten (~11 m Präzision)."""
    key = f"{round(lat, 4)},{round(lon, 4)}"
    return "bkg_" + hashlib.sha1(key.encode()).hexdigest()[:12]


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "OrtsschilderSprint-BKG/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _overpass_post(mirror: str, query: str, timeout: int = 360) -> bytes:
    body = urllib.parse.urlencode({"data": query}).encode()
    req = urllib.request.Request(
        mirror, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   "OrtsschilderSprint-BKG/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

# ── 1. BKG Gemeindegrenzen laden ──────────────────────────────────────────────

def fetch_municipalities() -> list[dict]:
    """Lädt alle deutschen Gemeindegrenzen aus dem BKG WFS (paginiert)."""
    print("Lade BKG VG250 Gemeindegrenzen...")
    features: list[dict] = []
    start = 0

    while True:
        url = BKG_WFS.format(start=start)
        try:
            raw  = _get(url, timeout=120)
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"  BKG WFS Fehler bei start={start}: {e}", file=sys.stderr)
            sys.exit(1)

        batch = data.get("features", [])
        if not batch:
            break
        features.extend(batch)
        print(f"  {len(features):,} Gemeinden geladen...", end="\r", flush=True)
        if len(batch) < 2000:
            break
        start += 2000
        time.sleep(0.5)

    print(f"\n  Gesamt: {len(features):,} Gemeinden")
    return features


def build_index(features: list[dict]) -> tuple[list, STRtree]:
    """Erstellt Spatial Index aus Gemeindegrenzen."""
    print("Spatial Index aufbauen...")

    # Debug: erstes Feature ausgeben um Feldnamen zu prüfen
    if features:
        first_props = features[0].get("properties", {})
        first_geom  = features[0].get("geometry", {})
        print(f"  Debug — Felder: {list(first_props.keys())[:10]}")
        print(f"  Debug — Geometry-Typ: {first_geom.get('type', '?')}")
        if first_geom.get("coordinates"):
            sample = first_geom["coordinates"]
            # Zeige erste Koordinate um CRS zu erkennen
            try:
                first_coord = sample[0][0] if isinstance(sample[0][0], (list, tuple)) else sample[0]
                print(f"  Debug — Erste Koordinate: {first_coord[:2] if len(first_coord) >= 2 else first_coord}")
            except Exception:
                pass

    muni_meta: list[tuple] = []   # (boundary_geom, name, bundesland)
    boundaries = []
    errors = 0

    for feat in features:
        try:
            geom  = shape(feat["geometry"])
            props = feat.get("properties", {})

            # Mehrere mögliche Feldnamen für Gemeindename
            name = (
                props.get("GEN") or props.get("gen") or
                props.get("NAME") or props.get("name") or
                props.get("gemeinde_name") or ""
            )
            name = name.strip() if name else ""

            # Mehrere mögliche Feldnamen für Bundesland-Schlüssel
            snl_raw = (
                props.get("SN_L") or props.get("sn_l") or
                props.get("BL") or props.get("bundesland_schluessel") or ""
            )
            snl = str(snl_raw).zfill(2) if snl_raw else ""
            bl  = BL_CODES.get(snl, snl)

            if not name or geom.is_empty:
                continue

            boundary = geom.boundary
            if boundary.is_empty:
                continue

            muni_meta.append((boundary, name, bl))
            boundaries.append(boundary)
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  Fehler bei Feature: {e}")
            continue

    if errors > 0:
        print(f"  {errors:,} Features konnten nicht verarbeitet werden")

    tree = STRtree(boundaries)
    print(f"  {len(muni_meta):,} Gemeindegrenzen indiziert")
    return muni_meta, tree

# ── 2. Straßen per Bundesland via Overpass holen ─────────────────────────────

def _overpass_query(query: str) -> list:
    for attempt in range(3):
        if attempt > 0:
            wait = 60 * attempt
            print(f"  Retry {attempt}/2 — warte {wait}s...")
            time.sleep(wait)
        for mirror in OVERPASS_MIRRORS:
            try:
                print(f"    {mirror} ... ", end="", flush=True)
                raw  = _overpass_post(mirror, query)
                elems = json.loads(raw.decode("utf-8")).get("elements", [])
                print(f"OK ({len(elems):,} Wege)")
                return elems
            except Exception as e:
                print(f"FEHLER ({e})")
                time.sleep(5)
    print("  Alle Overpass-Mirror fehlgeschlagen.", file=sys.stderr)
    return []


def fetch_roads(bundesland: str) -> list[dict]:
    """Holt alle relevanten Straßen eines Bundeslandes mit Geometrie."""
    query = f"""[out:json][timeout:300];
area["name"="{bundesland}"]["admin_level"="4"]->.bl;
(
  way["highway"~"^({ROAD_TYPES})$"](area.bl);
);
out geom;"""
    return _overpass_query(query)

# ── 3. Schnittpunkte berechnen ────────────────────────────────────────────────

def way_to_linestring(way: dict) -> Optional[LineString]:
    """Konvertiert Overpass-Way (mit geometry) in shapely LineString."""
    geom = way.get("geometry", [])
    if len(geom) < 2:
        return None
    try:
        return LineString([(p["lon"], p["lat"]) for p in geom])
    except Exception:
        return None


def extract_points(intersection) -> list[Point]:
    """Extrahiert alle Punkte aus einem shapely Intersection-Ergebnis."""
    t = intersection.geom_type
    if t == "Point":
        return [intersection]
    if t == "MultiPoint":
        return list(intersection.geoms)
    if t in ("LineString", "LinearRing"):
        # Straße verläuft entlang der Grenze → Endpunkte nehmen
        coords = list(intersection.coords)
        if len(coords) >= 2:
            return [Point(coords[0]), Point(coords[-1])]
        return []
    if t == "MultiLineString":
        pts = []
        for ls in intersection.geoms:
            coords = list(ls.coords)
            if coords:
                pts.append(Point(coords[0]))
                pts.append(Point(coords[-1]))
        return pts
    if t == "GeometryCollection":
        pts = []
        for g in intersection.geoms:
            pts.extend(extract_points(g))
        return pts
    return []


def compute_crossings(
    roads: list[dict],
    muni_meta: list[tuple],
    tree: STRtree,
    bundesland: str,
) -> dict[str, dict]:
    """Berechnet alle Schnittpunkte Straße × Gemeindegrenze."""
    signs: dict[str, dict] = {}

    for way in roads:
        road_type = way.get("tags", {}).get("highway", "")
        ls = way_to_linestring(way)
        if ls is None or ls.is_empty:
            continue

        # Spatial Index: Kandidaten per Bounding-Box-Filter
        candidates = tree.query(ls)

        for idx in candidates:
            boundary, name, bl = muni_meta[idx]
            # Straße muss Grenze wirklich schneiden (nicht nur berühren)
            if not ls.crosses(boundary):
                continue
            try:
                intersection = ls.intersection(boundary)
            except Exception:
                continue
            if intersection.is_empty:
                continue

            for pt in extract_points(intersection):
                # Koordinaten plausibel? (Deutschland: 47–55°N, 6–15°E)
                if not (47 < pt.y < 55.5 and 5.5 < pt.x < 15.5):
                    continue
                sid = make_id(pt.y, pt.x)
                if sid not in signs:
                    signs[sid] = {
                        "id":         sid,
                        "name":       name,
                        "lat":        round(pt.y, 6),
                        "lon":        round(pt.x, 6),
                        "road_type":  road_type,
                        "bundesland": bl or bundesland,
                    }

    return signs

# ── 4. Supabase Upload ────────────────────────────────────────────────────────

def _sb(method: str, path: str,
        body: Optional[bytes] = None,
        extra: Optional[dict] = None) -> tuple[int, bytes]:
    if not SUPABASE_SERVICE_KEY:
        print("SUPABASE_SERVICE_KEY nicht gesetzt!", file=sys.stderr)
        sys.exit(1)
    headers = {
        "Content-Type":  "application/json",
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    if extra:
        headers.update(extra)
    req = urllib.request.Request(
        SUPABASE_URL + path, data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upsert_signs(signs: list[dict]) -> None:
    total = len(signs)
    print(f"\nUpsert {total:,} Schilder → Supabase (Batches à {BATCH_SIZE})...")
    for i in range(0, total, BATCH_SIZE):
        batch = signs[i : i + BATCH_SIZE]
        status, resp = _sb(
            "POST", "/rest/v1/signs_bkg",
            body=json.dumps(batch).encode(),
            extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status not in (200, 201):
            print(f"  Fehler HTTP {status}: {resp.decode()[:300]}", file=sys.stderr)
            sys.exit(1)
        print(f"  {min(i + BATCH_SIZE, total):,}/{total:,}")


def delete_stale(current_ids: set[str]) -> None:
    """Entfernt Einträge, die im aktuellen Lauf nicht mehr vorkommen."""
    print("\nVeraltete Einträge entfernen...")
    status, resp = _sb("GET", "/rest/v1/signs_bkg?select=id&limit=500000")
    if status != 200:
        print(f"  Konnte bestehende IDs nicht laden (HTTP {status}) — übersprungen.")
        return

    existing = {r["id"] for r in json.loads(resp.decode())}
    stale    = list(existing - current_ids)
    if not stale:
        print("  Keine veralteten Einträge.")
        return

    print(f"  Lösche {len(stale):,} veraltete Einträge...")
    for i in range(0, len(stale), 200):
        batch = stale[i : i + 200]
        ids_p = urllib.parse.quote("(" + ",".join(batch) + ")")
        _sb("DELETE", f"/rest/v1/signs_bkg?id=in.{ids_p}")
    print(f"  {len(stale):,} gelöscht.")

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t0 = time.time()
    print("=== BKG-Methode: Ortsschild-Import Deutschland ===\n")
    print("Rechtsgrundlage: StVO § 42 + VwV-StVO Zeichen 310/311")
    print("Daten: © GeoBasis-DE / BKG, Lizenz dl-de/by-2-0 | OpenStreetMap-Mitwirkende\n")

    # 1. Gemeindegrenzen laden + indizieren
    features         = fetch_municipalities()
    muni_meta, tree  = build_index(features)

    # 2. Pro Bundesland Straßen holen + Schnittpunkte berechnen
    all_signs: dict[str, dict] = {}

    for bl in BUNDESLAENDER:
        print(f"\n── {bl} ──")
        ways     = fetch_roads(bl)
        if not ways:
            print("  Keine Straßen gefunden — übersprungen.")
            continue
        print(f"  {len(ways):,} Straßenabschnitte")
        crossings = compute_crossings(ways, muni_meta, tree, bl)
        new_count = sum(1 for k in crossings if k not in all_signs)
        all_signs.update(crossings)
        print(f"  +{new_count:,} neue Schilder (Gesamt: {len(all_signs):,})")
        time.sleep(15)   # Overpass Rate-Limit respektieren

    signs_list = list(all_signs.values())
    print(f"\nGesamt: {len(signs_list):,} Schildpositionen berechnet")

    # 3. Upload
    upsert_signs(signs_list)
    delete_stale({s["id"] for s in signs_list})

    elapsed = time.time() - t0
    m, s    = divmod(int(elapsed), 60)
    print(f"\n✓ Fertig in {m}m {s}s — {len(signs_list):,} Schilder in signs_bkg")
