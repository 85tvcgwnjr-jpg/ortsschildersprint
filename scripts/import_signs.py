#!/usr/bin/env python3
"""
BKG + OSM Ortsteil-Methode: Ortsschild-Positionen berechnen.

Rechtsgrundlage:
  StVO § 42 i.V.m. VwV-StVO zu Zeichen 310/311:
  An jeder öffentlichen Straße, die eine "geschlossene Ortschaft" betritt oder
  verlässt, ist ein Ortsschild (Zeichen 310/311) aufzustellen. Das gilt für:
    a) Gemeinden: jede eigenständige Gemeinde (Verwaltungseinheit)
    b) Ortsteile: räumlich durch Bebauungslücke abgegrenzte Siedlungen innerhalb
       einer Gemeinde (z.B. Dorf das zu einer größeren Gemeinde gehört)
  Grundlage: Schnittpunkt der jeweiligen Grenze × öffentliche Straße.

Datenquellen:
  • BKG VG250 — Amtliche Gemeindegrenzen 1:250.000
    Quelle: © GeoBasis-DE / BKG (2024), Lizenz dl-de/by-2-0
    Dienst: https://sgx.geodatenzentrum.de/wfs_vg250
  • OpenStreetMap via Overpass API — Straßennetz + Ortsteilgrenzen
    (admin_level=9 Stadtbezirke, admin_level=10 Stadtteile/Ortsteile)

Vorgehen:
  1. BKG VG250 Gemeindegrenzen laden → globaler Spatial Index
  2. Pro Bundesland:
     a. Straßen via Overpass holen (alle öffentlichen Typen, keine Autobahn)
     b. Gemeinde-Schnittpunkte: Straße × BKG-Grenze
     c. Ortsteil-Grenzen aus OSM (admin_level 9/10) laden → lokaler Index
     d. Ortsteil-Schnittpunkte: Straße × OSM-Ortsteilgrenze
  3. Zusammenführen + Upsert in Supabase signs

Tabelle anlegen (einmalig im Supabase SQL Editor):
  CREATE TABLE IF NOT EXISTS signs (
      id          TEXT PRIMARY KEY,
      name        TEXT NOT NULL,
      lat         DOUBLE PRECISION NOT NULL,
      lon         DOUBLE PRECISION NOT NULL,
      road_type   TEXT,
      bundesland  TEXT,
      created_at  TIMESTAMPTZ DEFAULT now()
  );
  ALTER TABLE signs ENABLE ROW LEVEL SECURITY;
  CREATE POLICY "public read signs" ON signs
      FOR SELECT TO public USING (true);

Usage:
  SUPABASE_SERVICE_KEY=<service_role_key> python3 scripts/import_signs.py

Runtime: ca. 30–60 Minuten.
"""

import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from shapely.geometry import LineString, Point
from shapely.geometry import shape
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

# ── Konfiguration ─────────────────────────────────────────────────────────────

SUPABASE_URL         = "https://slcprtkqkqwgstnyfpus.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BATCH_SIZE = 500

# Straßentypen mit Ortsschild-Pflicht (StVO § 42 + VwV-StVO)
# Alle öffentlichen Straßen die eine geschlossene Ortschaft betreten können.
# motorway: Autobahn — Sonderregelung, kein normales Ortsschild
# living_street: verkehrsberuhigter Bereich — liegt bereits im Ort
# service/track: Privatwege/Wirtschaftswege — keine Pflicht
ROAD_TYPES = "trunk|primary|secondary|tertiary|unclassified|residential"

# BKG WFS: Gemeinden (vg250_gem), WGS84, seitenweise
BKG_WFS = (
    "https://sgx.geodatenzentrum.de/wfs_vg250"
    "?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature"
    "&TYPENAMES=vg250:vg250_gem"
    "&OUTPUTFORMAT=application/json"
    "&SRSNAME=CRS:84"
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


def road_bearing_at_point(ls: LineString, pt: Point) -> float:
    """
    Kompassrichtung der Straße am nächsten Segment zu pt (0=N, CW, Grad).
    Gibt die Digitalisierungsrichtung der Straße zurück — nicht notwendigerweise
    die Einfahrtsrichtung. Für die Einfahrtsrichtung: compute_entry_bearing().

    Direkte Float-Arithmetik statt Shapely-Objekten in der inneren Schleife.
    """
    coords = list(ls.coords)
    best_dist_sq = float("inf")
    bearing = 0.0
    px, py = pt.x, pt.y

    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i + 1]
        sdx, sdy = x2 - x1, y2 - y1
        seg_sq = sdx * sdx + sdy * sdy
        if seg_sq == 0.0:
            d_sq = (px - x1) ** 2 + (py - y1) ** 2
        else:
            t = max(0.0, min(1.0, ((px - x1) * sdx + (py - y1) * sdy) / seg_sq))
            nx = x1 + t * sdx - px
            ny = y1 + t * sdy - py
            d_sq = nx * nx + ny * ny
        if d_sq < best_dist_sq:
            best_dist_sq = d_sq
            lat_mid = (y1 + y2) / 2.0
            east = (x2 - x1) * math.cos(math.radians(lat_mid))
            bearing = math.degrees(math.atan2(east, y2 - y1)) % 360.0

    return round(bearing, 1)


def compute_entry_bearing(ls: LineString, pt: Point, poly) -> float:
    """
    Einfahrtsrichtung des Schilds: Kompassrichtung von außen in den Ort (0=N, CW, Grad).

    Vorgehensweise:
      1. Basisrichtung der Straße am Schnittpunkt berechnen (road_bearing_at_point).
      2. Einen kleinen Schritt vorwärts entlang der Straße interpolieren.
      3. Liegt der Vorwärtspunkt im Ortspolygon? → Basisrichtung ist Einfahrtsrichtung.
         Liegt der Rückwärtspunkt im Polygon?    → Einfahrtsrichtung = Basis + 180°.
      4. Fallback bei Randlagen: Basisrichtung beibehalten.

    Das Garmin-Gerät nutzt entry_bearing für den Dot-Product-Check:
    Nur wenn der Fahrer sich in Einfahrtsrichtung bewegt, wird ein Crossing gewertet.
    """
    STEP = 0.0001  # ~11 m in Grad — klein genug für alle Ortsgrößen

    base = road_bearing_at_point(ls, pt)
    proj = ls.project(pt)
    total = ls.length

    try:
        # Vorwärts entlang der Straße
        if proj + STEP <= total:
            fwd_pt = ls.interpolate(proj + STEP)
            if poly.contains(fwd_pt):
                return base                          # vorwärts = in den Ort

        # Rückwärts entlang der Straße
        if proj - STEP >= 0:
            bwd_pt = ls.interpolate(proj - STEP)
            if poly.contains(bwd_pt):
                return (base + 180.0) % 360.0       # rückwärts = in den Ort
    except Exception as e:
        print(f"  [compute_entry_bearing] Fehler: {e}", file=sys.stderr)

    return base  # Fallback: Digitalisierungsrichtung


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


def _overpass_query(query: str) -> list:
    """Fragt Overpass ab, versucht alle Mirror mit Retry."""
    for attempt in range(3):
        if attempt > 0:
            wait = 60 * attempt
            print(f"  Retry {attempt}/2 — warte {wait}s...")
            time.sleep(wait)
        for mirror in OVERPASS_MIRRORS:
            try:
                print(f"    {mirror} ... ", end="", flush=True)
                raw   = _overpass_post(mirror, query)
                elems = json.loads(raw.decode("utf-8")).get("elements", [])
                print(f"OK ({len(elems):,})")
                return elems
            except Exception as e:
                print(f"FEHLER ({e})")
                time.sleep(5)
    print("  Alle Overpass-Mirror fehlgeschlagen.", file=sys.stderr)
    return []

# ── 1. BKG Gemeindegrenzen laden ──────────────────────────────────────────────

def fetch_municipalities() -> list[dict]:
    """Lädt alle deutschen Gemeindegrenzen aus dem BKG WFS (paginiert)."""
    print("Lade BKG VG250 Gemeindegrenzen...")
    features: list[dict] = []
    start = 0

    while True:
        url = BKG_WFS.format(start=start)
        data = None
        for attempt in range(4):
            try:
                raw  = _get(url, timeout=120)
                data = json.loads(raw.decode("utf-8"))
                break
            except Exception as e:
                wait = 15 * (attempt + 1)
                print(f"\n  BKG WFS Fehler (start={start}, Versuch {attempt+1}/4): {e}")
                if attempt < 3:
                    print(f"  Warte {wait}s...")
                    time.sleep(wait)
        if data is None:
            print(f"  BKG WFS nach 4 Versuchen nicht erreichbar — Abbruch.", file=sys.stderr)
            sys.exit(1)

        batch = data.get("features", [])
        if not batch:
            break
        features.extend(batch)
        print(f"  {len(features):,} Gemeinden geladen...", end="\r", flush=True)
        if len(batch) < 2000:
            break
        start += 2000
        time.sleep(1)

    print(f"\n  Gesamt: {len(features):,} Gemeinden")
    return features


def build_gemeinde_index(features: list[dict]) -> tuple[list, STRtree]:
    """Erstellt globalen Spatial Index aus BKG Gemeindegrenzen."""
    print("Gemeinde-Index aufbauen...")
    muni_meta: list[tuple] = []
    boundaries = []
    errors = 0

    for feat in features:
        try:
            geom  = shape(feat["geometry"])
            props = feat.get("properties", {})

            # gf=4: Landfläche — gf=2 sind Wasserflächen ausschließen
            if props.get("gf") != 4:
                continue

            name = props.get("gen", "").strip()
            snl  = str(props.get("sn_l", "")).zfill(2)
            bl   = BL_CODES.get(snl, snl)

            if not name or geom.is_empty:
                continue

            boundary = geom.boundary
            if boundary.is_empty:
                continue

            muni_meta.append((boundary, geom, name, bl))
            boundaries.append(boundary)
        except Exception as e:
            errors += 1
            if errors <= 3:
                print(f"  Fehler: {e}")
            continue

    tree = STRtree(boundaries)
    print(f"  {len(muni_meta):,} Gemeindegrenzen indiziert")
    return muni_meta, tree

# ── 2. Straßen pro Bundesland ─────────────────────────────────────────────────

def fetch_roads(bundesland: str) -> list[dict]:
    """Holt alle relevanten Straßen eines Bundeslandes mit Geometrie."""
    query = f"""[out:json][timeout:300];
area["name"="{bundesland}"]["admin_level"="4"]->.bl;
(
  way["highway"~"^({ROAD_TYPES})$"](area.bl);
);
out geom;"""
    return _overpass_query(query)


def prepare_roads(ways: list[dict]) -> tuple[list[LineString], list[str]]:
    """Extrahiert valide LineStrings + Straßentypen aus Overpass-Ways."""
    road_geoms: list[LineString] = []
    road_types: list[str]        = []
    for way in ways:
        geom = way.get("geometry", [])
        if len(geom) < 2:
            continue
        try:
            ls = LineString([(p["lon"], p["lat"]) for p in geom])
            if not ls.is_empty:
                road_geoms.append(ls)
                road_types.append(way.get("tags", {}).get("highway", ""))
        except Exception:
            continue
    return road_geoms, road_types

# ── 3. Ortsteil-Grenzen aus OSM ───────────────────────────────────────────────

def fetch_ortsteil_relations(bundesland: str) -> list[dict]:
    """
    Holt Ortsteil-Grenzen (admin_level 9/10) aus OSM.

    Rechtsgrundlage: StVO § 42 gilt auch für räumlich abgegrenzte Ortsteile
    innerhalb einer Gemeinde — jeder Ortsteil mit eigener Bebauungslücke
    ist eine eigenständige "geschlossene Ortschaft" mit Ortsschild-Pflicht.

    OSM admin_level:
      9 = Stadtbezirk (z.B. Köln-Rodenkirchen)
     10 = Stadtteil/Ortsteil (z.B. Köln-Junkersdorf)
    """
    # timeout=600: Bayern/NRW haben tausende Ortsteile → große Antwort
    query = f"""[out:json][timeout:600];
area["name"="{bundesland}"]["admin_level"="4"]->.bl;
relation["admin_level"~"^(9|10)$"]["boundary"="administrative"]["name"](area.bl);
out geom;"""
    return _overpass_query(query)


def relation_to_polygon(relation: dict):
    """Rekonstruiert Polygon aus Outer-Ways einer OSM-Boundary-Relation."""
    outer_lines = []
    for member in relation.get("members", []):
        if (member.get("type") == "way"
                and member.get("role") == "outer"
                and "geometry" in member):
            coords = [(n["lon"], n["lat"]) for n in member["geometry"]]
            if len(coords) >= 2:
                try:
                    outer_lines.append(LineString(coords))
                except Exception:
                    continue

    if not outer_lines:
        return None

    try:
        polys = list(polygonize(outer_lines))
        if not polys:
            return None
        return unary_union(polys)
    except Exception:
        return None


def build_ortsteil_index(relations: list[dict]) -> tuple[list, Optional[STRtree]]:
    """Erstellt lokalen Spatial Index aus OSM Ortsteil-Relationen."""
    muni_meta: list[tuple] = []
    boundaries = []

    for rel in relations:
        try:
            name = rel.get("tags", {}).get("name", "").strip()
            if not name:
                continue

            poly = relation_to_polygon(rel)
            if poly is None or poly.is_empty:
                continue

            boundary = poly.boundary
            if boundary.is_empty:
                continue

            muni_meta.append((boundary, poly, name, ""))
            boundaries.append(boundary)
        except Exception:
            continue

    if not boundaries:
        return [], None

    return muni_meta, STRtree(boundaries)

# ── 4. Schnittpunkte berechnen ────────────────────────────────────────────────

def extract_points(intersection) -> list[Point]:
    """Extrahiert alle Punkte aus einem shapely Intersection-Ergebnis."""
    t = intersection.geom_type
    if t == "Point":
        return [intersection]
    if t == "MultiPoint":
        return list(intersection.geoms)
    if t in ("LineString", "LinearRing"):
        coords = list(intersection.coords)
        if len(coords) >= 2:
            return [Point(coords[0]), Point(coords[-1])]
        return []
    if t == "MultiLineString":
        pts = []
        for ls in intersection.geoms:
            coords = list(ls.coords)
            if coords:
                pts.extend([Point(coords[0]), Point(coords[-1])])
        return pts
    if t == "GeometryCollection":
        pts = []
        for g in intersection.geoms:
            pts.extend(extract_points(g))
        return pts
    return []


def compute_crossings(
    road_geoms: list[LineString],
    road_types: list[str],
    muni_meta: list[tuple],
    tree: STRtree,
    bundesland: str,
) -> dict[str, dict]:
    """
    Berechnet alle Schnittpunkte Straße × Grenze (Batch-Query).
    Funktioniert für Gemeinde- und Ortsteilgrenzen gleichermaßen.
    """
    if not road_geoms or not muni_meta:
        return {}

    # Shapely 2.x Batch-Query: alle Straßen auf einmal
    road_idxs, muni_idxs = tree.query(road_geoms, predicate="crosses")

    signs: dict[str, dict] = {}

    for road_idx, muni_idx in zip(road_idxs, muni_idxs):
        ls        = road_geoms[road_idx]
        road_type = road_types[road_idx]
        boundary, poly, name, bl = muni_meta[muni_idx]

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
                    # Einfahrtsrichtung (von außen in den Ort) — das Gerät wertet
                    # nur Kreuzungen in dieser Richtung (±90°) als Sprint-Ziel.
                    "entry_bearing": compute_entry_bearing(ls, pt, poly),
                }

    return signs

# ── 5. Supabase Upload ────────────────────────────────────────────────────────

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
            "POST", "/rest/v1/signs",
            body=json.dumps(batch).encode(),
            extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status not in (200, 201):
            print(f"  Fehler HTTP {status}: {resp.decode()[:300]}", file=sys.stderr)
            sys.exit(1)
        print(f"  {min(i + BATCH_SIZE, total):,}/{total:,}")


def delete_stale(current_ids: set[str]) -> None:
    print("\nVeraltete Einträge entfernen...")
    status, resp = _sb("GET", "/rest/v1/signs?select=id&limit=500000")
    if status != 200:
        print(f"  Konnte IDs nicht laden (HTTP {status}) — übersprungen.")
        return

    existing = {r["id"] for r in json.loads(resp.decode())}
    stale    = list(existing - current_ids)
    if not stale:
        print("  Keine veralteten Einträge.")
        return

    print(f"  Lösche {len(stale):,} veraltete Einträge...")
    failed = 0
    for i in range(0, len(stale), 200):
        batch = stale[i : i + 200]
        ids_p = urllib.parse.quote("(" + ",".join(batch) + ")")
        status, _ = _sb("DELETE", f"/rest/v1/signs?id=in.{ids_p}")
        if status not in (200, 204):
            failed += len(batch)
    if failed:
        print(f"  ⚠ {failed:,} Einträge konnten nicht gelöscht werden.")
    else:
        print(f"  {len(stale):,} gelöscht.")

# ── Hauptprogramm ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t0 = time.time()
    print("=== BKG + OSM Ortsteil-Methode: Ortsschild-Import Deutschland ===\n")
    print("Rechtsgrundlage: StVO § 42 + VwV-StVO Zeichen 310/311")
    print("Daten: © GeoBasis-DE / BKG (dl-de/by-2-0) | OpenStreetMap-Mitwirkende\n")

    # 1. Gemeindegrenzen laden + globalen Index aufbauen
    features              = fetch_municipalities()
    gemeinde_meta, g_tree = build_gemeinde_index(features)
    del features  # Speicher freigeben

    # 2. Pro Bundesland: Straßen + Gemeinde- und Ortsteil-Schnittpunkte
    all_signs: dict[str, dict] = {}

    for bl in BUNDESLAENDER:
        print(f"\n── {bl} ──")

        # 2a. Straßen holen
        ways = fetch_roads(bl)
        if not ways:
            print("  Keine Straßen — übersprungen.")
            continue
        road_geoms, road_types = prepare_roads(ways)
        del ways  # Speicher freigeben
        print(f"  {len(road_geoms):,} Straßenabschnitte mit Geometrie")

        # 2b. Gemeinde-Schnittpunkte (BKG)
        g_signs = compute_crossings(road_geoms, road_types, gemeinde_meta, g_tree, bl)
        print(f"  Gemeinden: {len(g_signs):,} Schilder")

        # 2c. Ortsteil-Grenzen aus OSM (admin_level 9/10)
        # BKG bietet keine Gemeindeteile via WFS an — OSM ist die vollständigste
        # kostenlose Quelle für Ortsteilgrenzen in Deutschland.
        ortsteil_relations = fetch_ortsteil_relations(bl)
        o_signs: dict[str, dict] = {}

        if ortsteil_relations:
            ortsteil_meta, o_tree = build_ortsteil_index(ortsteil_relations)
            if o_tree is not None:
                o_signs = compute_crossings(road_geoms, road_types, ortsteil_meta, o_tree, bl)
            print(f"  Ortsteile: {len(ortsteil_relations):,} Grenzen → {len(o_signs):,} Schilder")

        # 2d. Zusammenführen (Gemeinde hat Vorrang bei Duplikaten)
        combined  = {**o_signs, **g_signs}
        new_count = sum(1 for k in combined if k not in all_signs)
        all_signs.update(combined)
        print(f"  +{new_count:,} neue Schilder (Gesamt: {len(all_signs):,})")

        time.sleep(5)

    signs_list = list(all_signs.values())
    print(f"\nGesamt: {len(signs_list):,} Schildpositionen")

    # 3. Upload
    upsert_signs(signs_list)
    delete_stale({s["id"] for s in signs_list})

    elapsed = time.time() - t0
    m, s    = divmod(int(elapsed), 60)
    print(f"\n✓ Fertig in {m}m {s}s — {len(signs_list):,} Schilder in signs")
