#!/usr/bin/env python3
"""
Import all German Ortsschild (city_limit) nodes from OpenStreetMap into Supabase.

Every individual sign node is kept — multiple per town (one per road entry / direction).
Signs without a name use the nearest OSM place node as name fallback, matching the
same two-pass logic used in the Garmin app (OrtsschilderBackground.mc).

Usage:
    SUPABASE_SERVICE_KEY=<service_role_key> python3 scripts/import_signs.py

The service_role key is found in the Supabase dashboard under
Project Settings → API → "service_role" (not the anon key).
Safe to re-run: uses upsert (merge-duplicates). Stale signs removed at the end.

Runtime: ~3–7 minutes depending on Overpass server load.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

SUPABASE_URL         = "https://slcprtkqkqwgstnyfpus.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

BATCH_SIZE = 500   # signs per Supabase upsert call

# ── Overpass query ────────────────────────────────────────────────────────────
# Fetches city_limit sign nodes AND named place nodes (for name resolution)
# in a single round-trip for all of Germany.

OVERPASS_QUERY = """[out:json][timeout:300];
area["ISO3166-1"="DE"]["boundary"="administrative"]["admin_level"="2"]->.de;
(
  node["traffic_sign"~"city_limit"](area.de);
  node["highway"="city_limit"](area.de);
  node["place"~"^(village|town|city|municipality)$"](area.de);
);
out body;"""

# ── Overpass fetcher ──────────────────────────────────────────────────────────

def _overpass_post(mirror: str, query: str, timeout: int) -> bytes:
    body = urllib.parse.urlencode({"data": query}).encode()
    req  = urllib.request.Request(
        mirror, data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent":   "OrtsschilderSprint-Import/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_from_overpass() -> list:
    """Try each mirror up to 3 rounds with waits; return elements on first success."""
    print("Querying Overpass API (takes 2–5 minutes for all of Germany)...")
    for attempt in range(3):
        if attempt > 0:
            wait = 60 * attempt
            print(f"\n  Retry {attempt}/2 — waiting {wait}s for Overpass to recover...")
            time.sleep(wait)
        for mirror in OVERPASS_MIRRORS:
            try:
                print(f"  → {mirror} ... ", end="", flush=True)
                raw  = _overpass_post(mirror, OVERPASS_QUERY, timeout=360)
                data = json.loads(raw.decode("utf-8"))
                elems = data.get("elements", [])
                print(f"OK  ({len(elems)} elements)")
                return elems
            except Exception as exc:
                print(f"FAILED ({exc})")
                time.sleep(5)
    print("\nAll Overpass mirrors failed after 3 attempts. Try again later.", file=sys.stderr)
    sys.exit(1)

# ── Parser ────────────────────────────────────────────────────────────────────

def _extract_name(tags: dict) -> str:
    for key in ("name", "city", "destination"):
        val = tags.get(key, "").strip()
        if val:
            return val
    return ""


def parse_signs(elements: list) -> list[dict]:
    """
    Two-pass parse — identical logic to OrtsschilderBackground.mc:

    Pass 1: collect named place nodes → lookup list for name fallback.
    Pass 2: resolve each city_limit node to a name (from its own tags or
            nearest place node). Every individual node is kept as its own
            sign (same name, different lat/lon = different road entry).
    """
    # Pass 1 — place nodes
    places: list[tuple[str, float, float]] = []   # (name, lat, lon)
    for elem in elements:
        if elem.get("type") != "node":
            continue
        tags = elem.get("tags", {})
        if not tags.get("place"):
            continue
        name = _extract_name(tags)
        lat  = elem.get("lat")
        lon  = elem.get("lon")
        if name and lat is not None and lon is not None:
            places.append((name, float(lat), float(lon)))

    print(f"  Place nodes (name fallback pool): {len(places):,}")

    # Pass 2 — city_limit nodes
    signs: dict[str, dict] = {}
    has_city_limit = False

    for i, elem in enumerate(elements):
        if elem.get("type") != "node":
            continue
        tags = elem.get("tags", {})

        hw = tags.get("highway", "")
        ts = tags.get("traffic_sign", "")
        is_city_limit = (hw == "city_limit") or ("city_limit" in ts)
        if not is_city_limit:
            continue

        has_city_limit = True
        lat    = elem.get("lat")
        lon    = elem.get("lon")
        osm_id = elem.get("id")
        if lat is None or lon is None:
            continue

        lat, lon  = float(lat), float(lon)
        sign_id   = f"osm_{osm_id}"

        # Name from sign's own tags first, else nearest place node
        name = _extract_name(tags)
        if not name and places:
            best_d2, best_name = float("inf"), ""
            for p_name, p_lat, p_lon in places:
                d2 = (lat - p_lat) ** 2 + (lon - p_lon) ** 2
                if d2 < best_d2:
                    best_d2   = d2
                    best_name = p_name
            name = best_name

        if not name:
            continue   # skip signs that couldn't be named

        # Keep every node individually — multiple per town, one per road entry
        if sign_id not in signs:
            signs[sign_id] = {"id": sign_id, "name": name, "lat": lat, "lon": lon}

    # Fallback: no city_limit nodes at all → use place nodes directly as signs
    if not has_city_limit:
        print("  No city_limit nodes found — falling back to place nodes directly")
        for idx, (name, lat, lon) in enumerate(places):
            pid = f"place_{idx}"
            signs[pid] = {"id": pid, "name": name, "lat": lat, "lon": lon}

    result = list(signs.values())
    print(f"  Signs after parse:  {len(result):,}")
    return result

# ── Supabase helpers ──────────────────────────────────────────────────────────

def _sb_request(method: str, path: str,
                body: Optional[bytes] = None,
                extra_headers: Optional[dict] = None) -> tuple:
    if not SUPABASE_SERVICE_KEY:
        print("Error: SUPABASE_SERVICE_KEY env var not set.\n"
              "  Get the service_role key from Supabase → Project Settings → API.",
              file=sys.stderr)
        sys.exit(1)

    headers = {
        "Content-Type":  "application/json",
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(SUPABASE_URL + path, data=body,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def upsert_signs(signs: list[dict]) -> None:
    total = len(signs)
    print(f"\nUpserting {total:,} signs to Supabase (batches of {BATCH_SIZE})...")
    for i in range(0, total, BATCH_SIZE):
        batch  = signs[i : i + BATCH_SIZE]
        status, resp = _sb_request(
            "POST", "/rest/v1/signs",
            body=json.dumps(batch).encode(),
            extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
        )
        if status not in (200, 201):
            print(f"  Upsert failed (HTTP {status}): {resp.decode()[:300]}", file=sys.stderr)
            sys.exit(1)
        print(f"  {min(i + BATCH_SIZE, total):,}/{total:,}")


def delete_stale_signs(current_ids: list[str]) -> None:
    """Remove DB rows whose OSM id no longer exists in the current Overpass export."""
    print("\nRemoving stale signs...")
    status, resp = _sb_request("GET", "/rest/v1/signs?select=id&limit=200000")
    if status != 200:
        print(f"  Could not fetch existing IDs (HTTP {status}) — skipping stale cleanup.")
        return

    existing = {row["id"] for row in json.loads(resp.decode())}
    current  = set(current_ids)
    stale    = existing - current
    if not stale:
        print("  No stale signs.")
        return

    print(f"  Deleting {len(stale):,} stale signs...")
    stale_list = list(stale)
    for i in range(0, len(stale_list), 200):
        batch     = stale_list[i : i + 200]
        ids_param = urllib.parse.quote("(" + ",".join(batch) + ")")
        status, resp = _sb_request("DELETE", f"/rest/v1/signs?id=in.{ids_param}")
        if status not in (200, 204):
            print(f"  Delete batch warning (HTTP {status}): {resp.decode()[:100]}")

    print(f"  Removed {len(stale):,} stale signs.")

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    t0 = time.time()
    print("=== Ortsschild Import — Germany ===\n")

    elements = fetch_from_overpass()
    print()

    signs = parse_signs(elements)

    upsert_signs(signs)
    delete_stale_signs([s["id"] for s in signs])

    elapsed = time.time() - t0
    print(f"\n✓ Done in {elapsed:.0f}s — {len(signs):,} signs in Supabase.")
