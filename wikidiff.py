#!/usr/bin/env python3
"""
Name-only OSM → Wikidata matcher.

Features:
  - Searches in BOTH:
      1) local Wikidata language auto mode (no explicit language parameter)
      2) English ("en") fallback
  - Accept ONLY entities whose P31 ("instance of") intersects ALLOWED_P31_QIDS
  - Output ONLY accepted matches
  - Print matches to screen
  - Progress bar support (tqdm)

Install:
  pip install requests tqdm

Usage:
  python match_osm_powerplants_to_wikidata.py \
      --input input.geojson \
      --output matched_only.geojson \
      --max-hits 5 \
      --sleep-s 0.05
"""

from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional

import requests

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

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


def wd_api_get(session: requests.Session, params: Dict[str, Any]) -> Dict[str, Any]:
    r = session.get(WIKIDATA_API, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def wbsearchentities(session: requests.Session, query: str, language: Optional[str], limit: int):
    params = {
        "action": "wbsearchentities",
        "search": query,
        "limit": limit,
        "format": "json",
    }

    # If language is None, we do not send language/uselang at all.
    if language is not None:
        params["language"] = language
        params["uselang"] = language

    return wd_api_get(session, params).get("search", [])


def wbgetentities_p31(session: requests.Session, qids: List[str]) -> Dict[str, List[str]]:
    if not qids:
        return {}

    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "claims",
        "format": "json",
    }

    data = wd_api_get(session, params)
    entities = data.get("entities", {})

    result = {}
    for qid, entity in entities.items():
        claims = entity.get("claims", {})
        p31_claims = claims.get("P31", [])

        p31_values = []
        for claim in p31_claims:
            try:
                val = claim["mainsnak"]["datavalue"]["value"]
                if val.get("entity-type") == "item":
                    pid = val.get("id")
                    if pid:
                        p31_values.append(pid)
            except Exception:
                continue

        result[qid] = list(set(p31_values))

    return result


def get_osm_name(feature: Dict[str, Any]) -> Optional[str]:
    props = feature.get("properties", {})

    if isinstance(props.get("name"), str) and props["name"].strip():
        return props["name"].strip()

    tags = props.get("tags", {})
    if isinstance(tags, dict):
        for k in ("name", "name:en"):
            if isinstance(tags.get(k), str) and tags[k].strip():
                return tags[k].strip()

    return None


def find_match(
    session: requests.Session,
    osm_name: str,
    limit: int,
    sleep_s: float,
) -> Optional[Dict[str, Any]]:
    """
    Tries:
      1) wbsearchentities without language param (Wikidata default behavior)
      2) wbsearchentities in English
    Accepts first hit whose P31 intersects ALLOWED_P31_QIDS.
    """

    # 1) default language search (no language/uselang params)
    hits = wbsearchentities(session, osm_name, language=None, limit=limit)
    if sleep_s:
        time.sleep(sleep_s)

    # 2) fallback: english
    if not hits:
        hits = wbsearchentities(session, osm_name, language="en", limit=limit)
        if sleep_s:
            time.sleep(sleep_s)

    if not hits:
        return None

    qids = [h["id"] for h in hits if h.get("id", "").startswith("Q")]
    if not qids:
        return None

    p31_map = wbgetentities_p31(session, qids)
    if sleep_s:
        time.sleep(sleep_s)

    # Iterate in search ranking order
    for hit in hits:
        qid = hit.get("id")
        if not qid:
            continue

        instance_of = p31_map.get(qid, [])
        if set(instance_of) & ALLOWED_P31_QIDS:
            return {
                "qid": qid,
                "label": hit.get("label", qid),
                "description": hit.get("description", ""),
                "instance_of": instance_of,
            }

    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-hits", type=int, default=5)
    ap.add_argument("--sleep-s", type=float, default=0.40)
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        gj = json.load(f)

    features = gj.get("features", [])

    session = requests.Session()
    session.headers.update({
        "User-Agent": "osm-wikidata-name-instance-filter/1.1"
    })

    out_features = []

    iterator = tqdm(features, desc="Matching") if tqdm else features

    for feat in iterator:
        osm_name = get_osm_name(feat)
        if not osm_name:
            continue

        match = find_match(
            session=session,
            osm_name=osm_name,
            limit=args.max_hits,
            sleep_s=args.sleep_s,
        )

        if not match:
            continue

        props = feat.setdefault("properties", {})
        props["wikidata_qid"] = match["qid"]
        props["wikidata_label"] = match["label"]
        props["wikidata_description"] = match["description"]
        props["wikidata_instance_of"] = match["instance_of"]

        out_features.append(feat)

        print(f"[MATCH] {osm_name} → {match['qid']} ({match['label']})")

    output = {
        "type": "FeatureCollection",
        "features": out_features,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Matched {len(out_features)}/{len(features)} features.")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()
