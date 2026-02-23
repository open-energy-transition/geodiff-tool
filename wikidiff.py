#!/usr/bin/env python3
"""
OSM (GeoJSON Points) → Wikidata matcher (+ optional "nearby Wikidata items").

Matching logic:
  1) Name-based candidate generation via wbsearchentities
     - Try default language behavior (no language/uselang params)
     - If no hits, fallback to English ("en")
  2) For candidates returned by search, fetch claims via wbgetentities:
       - P31 (instance of) MUST intersect ALLOWED_P31_QIDS
       - If P625 (coordinate location) exists on the Wikidata entity:
            compute haversine distance to OSM Point
            accept ONLY if distance <= 1000m
         If P625 does NOT exist:
            accept based on P31 allowlist only (no distance filter)
  3) Choose the first acceptable candidate in Wikidata search ranking order

Optional:
  - If --nearby is enabled, queries Wikidata SPARQL for items within a radius
    around the OSM Point and stores them in properties["wikidata_nearby"].

Output:
  - GeoJSON FeatureCollection containing ONLY accepted matches
  - Adds properties:
      wikidata_qid, wikidata_label, wikidata_description
      wikidata_instance_of (P31 QIDs)
      wikidata_distance_m (only if P625 exists)
      wikidata_has_p625 (bool)
      wikidata_search_language ("default" or "en")
      wikidata_nearby (optional, if --nearby)
  - Prints each accepted match to stdout

Robustness:
  - Retries on transient network issues/timeouts with exponential backoff (default: retry forever)

Install:
  pip install requests tqdm

Usage:
  python match_osm_powerplants_to_wikidata.py \
      --input input.geojson \
      --output matched_only.geojson \
      --max-hits 10 \
      --sleep-s 0.05 \
      --nearby --nearby-radius-km 1.0 --nearby-limit 25
"""

from __future__ import annotations

import argparse
import json
import math
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"

DISTANCE_THRESHOLD_M = 1000.0

ALLOWED_P31_QIDS = {
    "Q1047832", "Q1066997", "Q1096907", "Q112046", "Q1144084", "Q11658",
    "Q12353044", "Q1260916", "Q1262712", "Q12748", "Q131502", "Q134447",
    "Q15911738", "Q159719", "Q1662011", "Q1662100", "Q174814", "Q1798641",
    "Q180253", "Q1805337", "Q190107", "Q1907989", "Q193470", "Q1939150",
    "Q194356", "Q200297", "Q200928", "Q20170565", "Q2069494", "Q2140665",
    "Q2144320", "Q217941", "Q2298412", "Q2356943", "Q2466889", "Q25342",
    "Q25509593", "Q2881503", "Q2944640", "Q30565277", "Q339353", "Q370607",
    "Q390516", "Q3971388", "Q40858", "Q4117949", "Q472093", "Q48970589",
    "Q49833", "Q55364050", "Q56397239", "Q56697283", "Q60173690", "Q6558431",
    "Q671224", "Q689855", "Q810924", "Q820477", "Q83405", "Q837718",
    "Q839922", "Q844861", "Q911379", "Q914711",
}


# -------------------- geo helpers --------------------

def extract_osm_point(feature: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Returns (lat, lon) from a GeoJSON Point feature, else None.
    GeoJSON point coordinates are [lon, lat].
    """
    geom = feature.get("geometry") or {}
    if geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    lon, lat = coords[0], coords[1]
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    return float(lat), float(lon)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


# -------------------- network retry helpers --------------------

def is_transient_requests_error(exc: Exception) -> bool:
    transient_types = (
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    return isinstance(exc, transient_types)


def is_retryable_http_status(status_code: int) -> bool:
    return status_code in (429, 500, 502, 503, 504)


def sleep_backoff(attempt: int, base_s: float, cap_s: float) -> None:
    exp = base_s * (2 ** max(0, attempt - 1))
    exp = min(cap_s, exp)
    jitter = random.uniform(0.5, 1.5)
    time.sleep(exp * jitter)


def wd_api_get(
    session: requests.Session,
    params: Dict[str, Any],
    timeout_s: float,
    max_retries: int,   # 0 => infinite
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Dict[str, Any]:
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(WIKIDATA_API, params=params, timeout=timeout_s)

            if is_retryable_http_status(r.status_code):
                raise requests.exceptions.HTTPError(
                    f"HTTP {r.status_code} from Wikidata API",
                    response=r,
                )

            r.raise_for_status()
            return r.json()

        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is not None and is_retryable_http_status(status)
            if not retryable:
                raise
            if max_retries != 0 and attempt > max_retries:
                raise
            print(f"[WARN] {e}. Backing off and retrying (attempt {attempt})...")
            sleep_backoff(attempt, backoff_base_s, backoff_cap_s)

        except Exception as e:
            if not is_transient_requests_error(e):
                raise
            if max_retries != 0 and attempt > max_retries:
                raise
            print(f"[WARN] Transient error: {type(e).__name__}: {e}")
            print(f"       Backing off and retrying (attempt {attempt})...")
            sleep_backoff(attempt, backoff_base_s, backoff_cap_s)


def wd_sparql_get(
    session: requests.Session,
    query: str,
    timeout_s: float,
    max_retries: int,   # 0 => infinite
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Dict[str, Any]:
    """
    SPARQL endpoint GET with the same retry/backoff rules.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(
                WIKIDATA_SPARQL,
                params={"format": "json", "query": query},
                timeout=timeout_s,
            )

            if is_retryable_http_status(r.status_code):
                raise requests.exceptions.HTTPError(
                    f"HTTP {r.status_code} from Wikidata SPARQL",
                    response=r,
                )

            r.raise_for_status()
            return r.json()

        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            retryable = status is not None and is_retryable_http_status(status)
            if not retryable:
                raise
            if max_retries != 0 and attempt > max_retries:
                raise
            print(f"[WARN] {e}. Backing off and retrying (attempt {attempt})...")
            sleep_backoff(attempt, backoff_base_s, backoff_cap_s)

        except Exception as e:
            if not is_transient_requests_error(e):
                raise
            if max_retries != 0 and attempt > max_retries:
                raise
            print(f"[WARN] Transient SPARQL error: {type(e).__name__}: {e}")
            print(f"       Backing off and retrying (attempt {attempt})...")
            sleep_backoff(attempt, backoff_base_s, backoff_cap_s)


# -------------------- wikidata api wrappers --------------------

def wbsearchentities(
    session: requests.Session,
    query: str,
    language: Optional[str],
    limit: int,
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "action": "wbsearchentities",
        "search": query,
        "limit": limit,
        "format": "json",
    }
    if language is not None:
        params["language"] = language
        params["uselang"] = language

    data = wd_api_get(
        session=session,
        params=params,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )
    return data.get("search", []) or []


def wbgetentities_claims(
    session: requests.Session,
    qids: List[str],
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Dict[str, Any]:
    if not qids:
        return {}

    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "claims",
        "format": "json",
    }
    data = wd_api_get(
        session=session,
        params=params,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )
    return data.get("entities", {}) or {}


def extract_p31(entity: Dict[str, Any]) -> List[str]:
    claims = (entity or {}).get("claims", {}) or {}
    p31_claims = claims.get("P31", []) or []
    vals: List[str] = []
    for claim in p31_claims:
        try:
            v = claim["mainsnak"]["datavalue"]["value"]
            if v.get("entity-type") == "item":
                qid = v.get("id")
                if isinstance(qid, str) and qid.startswith("Q"):
                    vals.append(qid)
        except Exception:
            continue
    return sorted(set(vals))


def extract_p625(entity: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Extract P625 coordinate location as (lat, lon) if present.
    """
    claims = (entity or {}).get("claims", {}) or {}
    p625_claims = claims.get("P625", []) or []
    if not p625_claims:
        return None
    try:
        dv = p625_claims[0]["mainsnak"].get("datavalue", {}).get("value", {})
        lat = dv.get("latitude")
        lon = dv.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return float(lat), float(lon)
    except Exception:
        return None
    return None


# -------------------- SPARQL "nearby" --------------------

def sparql_nearby_items(
    session: requests.Session,
    center_lat: float,
    center_lon: float,
    radius_km: float,
    limit: int,
    language: str,
    p31_filter_roots: Optional[List[str]],
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> List[Dict[str, Any]]:
    """
    Finds items with P625 within radius_km around (center_lat, center_lon).
    Optional: restrict to items whose P31 is a subclass of any given roots (QIDs).
      Uses: ?item wdt:P31/wdt:P279* wd:Q...
    """
    # SPARQL uses WKT Point(lon lat)
    wkt = f"Point({center_lon} {center_lat})"

    type_filter = ""
    if p31_filter_roots:
        parts = [f"{{ ?item wdt:P31/wdt:P279* wd:{qid} . }}" for qid in p31_filter_roots]
        type_filter = "FILTER ( " + " || ".join([f"EXISTS {p}" for p in parts]) + " )"

    query = f"""
    SELECT ?item ?itemLabel ?itemDescription ?dist WHERE {{
      SERVICE wikibase:around {{
        ?item wdt:P625 ?loc .
        bd:serviceParam wikibase:center "{wkt}"^^geo:wktLiteral .
        bd:serviceParam wikibase:radius "{radius_km}" .
        bd:serviceParam wikibase:distance ?dist .
      }}
      {type_filter}
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "{language}". }}
    }}
    ORDER BY ?dist
    LIMIT {limit}
    """

    data = wd_sparql_get(
        session=session,
        query=query,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )

    out: List[Dict[str, Any]] = []
    for row in (data.get("results", {}) or {}).get("bindings", []) or []:
        item_url = (row.get("item") or {}).get("value", "")
        qid = item_url.rsplit("/", 1)[-1] if isinstance(item_url, str) else ""
        if not (isinstance(qid, str) and qid.startswith("Q")):
            continue

        label = (row.get("itemLabel") or {}).get("value", qid)
        desc = (row.get("itemDescription") or {}).get("value", "")

        dist_km_val = (row.get("dist") or {}).get("value")
        try:
            dist_km = float(dist_km_val)
        except Exception:
            dist_km = None

        out.append({
            "qid": qid,
            "label": label,
            "description": desc,
            "distance_m": None if dist_km is None else round(dist_km * 1000.0, 1),
        })

    return out


# -------------------- osm helpers --------------------

def get_osm_name(feature: Dict[str, Any]) -> Optional[str]:
    props = feature.get("properties", {}) or {}

    if isinstance(props.get("name"), str) and props["name"].strip():
        return props["name"].strip()

    tags = props.get("tags", {})
    if isinstance(tags, dict):
        for k in ("name", "name:en"):
            if isinstance(tags.get(k), str) and tags[k].strip():
                return tags[k].strip()

    return None


# -------------------- matching --------------------

def find_match(
    session: requests.Session,
    osm_name: str,
    osm_point: Tuple[float, float],
    limit: int,
    sleep_s: float,
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Optional[Dict[str, Any]]:
    """
    Candidate generation: wbsearchentities by name (default language, fallback to en)
    Filtering:
      - P31 intersects allowlist
      - If P625 exists: distance(osm_point, p625) <= threshold
    Selection:
      - first acceptable candidate in search ranking order
    """
    hits = wbsearchentities(
        session=session,
        query=osm_name,
        language=None,
        limit=limit,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )
    if sleep_s:
        time.sleep(sleep_s)
    lang_used = "default"

    if not hits:
        hits = wbsearchentities(
            session=session,
            query=osm_name,
            language="en",
            limit=limit,
            timeout_s=timeout_s,
            max_retries=max_retries,
            backoff_base_s=backoff_base_s,
            backoff_cap_s=backoff_cap_s,
        )
        if sleep_s:
            time.sleep(sleep_s)
        lang_used = "en"

    if not hits:
        return None

    qids = [h.get("id") for h in hits if isinstance(h.get("id"), str) and h["id"].startswith("Q")]
    if not qids:
        return None

    entities = wbgetentities_claims(
        session=session,
        qids=qids,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )
    if sleep_s:
        time.sleep(sleep_s)

    osm_lat, osm_lon = osm_point

    for hit in hits:
        qid = hit.get("id")
        if not (isinstance(qid, str) and qid.startswith("Q")):
            continue

        ent = entities.get(qid)
        if not isinstance(ent, dict):
            continue

        p31 = extract_p31(ent)
        if not (set(p31) & ALLOWED_P31_QIDS):
            continue

        p625 = extract_p625(ent)
        if p625 is not None:
            wd_lat, wd_lon = p625
            dist = haversine_m(osm_lat, osm_lon, wd_lat, wd_lon)
            if dist > DISTANCE_THRESHOLD_M:
                continue
            distance_m: Optional[float] = dist
            has_p625 = True
        else:
            distance_m = None
            has_p625 = False

        return {
            "qid": qid,
            "label": hit.get("label", qid),
            "description": hit.get("description", ""),
            "instance_of": p31,
            "has_p625": has_p625,
            "distance_m": distance_m,
            "search_language": lang_used,
        }

    return None


# -------------------- main --------------------

def parse_qid_list(s: str) -> List[str]:
    out: List[str] = []
    for part in (s or "").split(","):
        q = part.strip()
        if q and q.startswith("Q"):
            out.append(q)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-hits", type=int, default=10)
    ap.add_argument("--sleep-s", type=float, default=0.05)

    ap.add_argument("--timeout-s", type=float, default=30.0, help="HTTP timeout per request (default: 30)")
    ap.add_argument("--max-retries", type=int, default=0, help="0 = retry forever (default)")
    ap.add_argument("--backoff-base-s", type=float, default=2.0)
    ap.add_argument("--backoff-cap-s", type=float, default=120.0)

    ap.add_argument(
        "--distance-threshold-m",
        type=float,
        default=DISTANCE_THRESHOLD_M,
        help="Max allowed distance when Wikidata has P625 (default: 1000m)",
    )

    ap.add_argument(
        "--user-agent",
        default="osm-wikidata-name+coord-filter+nearby/1.4 (contact: you@example.com)",
        help="User-Agent string for polite Wikidata API usage",
    )

    # Nearby options
    ap.add_argument("--nearby", action="store_true", help="Also query Wikidata for items near each OSM point")
    ap.add_argument("--nearby-radius-km", type=float, default=1.0, help="Nearby search radius in km (default: 1.0)")
    ap.add_argument("--nearby-limit", type=int, default=25, help="Max nearby items per feature (default: 25)")
    ap.add_argument("--nearby-language", default="en", help='Label language for nearby items (default: "en")')
    ap.add_argument(
        "--nearby-p31",
        default="",
        help="Optional: comma-separated QIDs to restrict nearby items by type "
             "(item must have P31 subclass of any given QID). Example: Q33506 for museums.",
    )

    args = ap.parse_args()

    nearby_p31_roots = parse_qid_list(args.nearby_p31)

    with open(args.input, "r", encoding="utf-8") as f:
        gj = json.load(f)

    if not isinstance(gj, dict) or gj.get("type") != "FeatureCollection":
        raise SystemExit("Input must be a GeoJSON FeatureCollection.")

    features = gj.get("features", [])
    if not isinstance(features, list):
        raise SystemExit("GeoJSON FeatureCollection.features must be a list.")

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})

    out_features: List[Dict[str, Any]] = []

    iterator = tqdm(features, desc="Matching)") if tqdm else features

    for feat in iterator:
        if not isinstance(feat, dict):
            continue

        osm_name = get_osm_name(feat)
        if not osm_name:
            continue

        osm_point = extract_osm_point(feat)
        if osm_point is None:
            continue  # need OSM coordinates to compare with Wikidata P625

        match = find_match(
            session=session,
            osm_name=osm_name,
            osm_point=osm_point,
            limit=args.max_hits,
            sleep_s=args.sleep_s,
            timeout_s=args.timeout_s,
            max_retries=args.max_retries,
            backoff_base_s=args.backoff_base_s,
            backoff_cap_s=args.backoff_cap_s,
        )

        if not match:
            continue

        props = feat.setdefault("properties", {})
        props["wikidata_qid"] = match["qid"]
        props["wikidata_label"] = match["label"]
        props["wikidata_description"] = match["description"]
        props["wikidata_instance_of"] = match["instance_of"]
        props["wikidata_has_p625"] = match["has_p625"]
        props["wikidata_distance_m"] = None if match["distance_m"] is None else round(match["distance_m"], 1)
        props["wikidata_search_language"] = match["search_language"]

        # Nearby: query around OSM point (usually what you want for "next to")
        if args.nearby:
            osm_lat, osm_lon = osm_point
            nearby = sparql_nearby_items(
                session=session,
                center_lat=osm_lat,
                center_lon=osm_lon,
                radius_km=float(args.nearby_radius_km),
                limit=int(args.nearby_limit),
                language=str(args.nearby_language),
                p31_filter_roots=nearby_p31_roots if nearby_p31_roots else None,
                timeout_s=args.timeout_s,
                max_retries=args.max_retries,
                backoff_base_s=args.backoff_base_s,
                backoff_cap_s=args.backoff_cap_s,
            )
            if args.sleep_s:
                time.sleep(args.sleep_s)
            props["wikidata_nearby"] = nearby

        out_features.append(feat)

        dist_txt = ""
        if match["distance_m"] is not None:
            dist_txt = f", dist={match['distance_m']:.1f}m"
        else:
            dist_txt = ", dist=n/a (no P625)"

        nearby_txt = ""
        if args.nearby:
            n = len(props.get("wikidata_nearby") or [])
            nearby_txt = f", nearby={n}"

        print(f"[MATCH:{match['search_language']}] {osm_name} → {match['qid']} ({match['label']}){dist_txt}{nearby_txt}")

    output = {"type": "FeatureCollection", "features": out_features}

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Matched {len(out_features)}/{len(features)} features.")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()