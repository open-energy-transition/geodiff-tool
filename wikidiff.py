#!/usr/bin/env python3
"""
Name-only OSM → Wikidata matcher.

Features:
  - Searches in BOTH:
      1) default Wikidata language behavior (no explicit language/uselang)
      2) English ("en") fallback
  - Accept ONLY entities whose P31 ("instance of") intersects ALLOWED_P31_QIDS
  - Output ONLY accepted matches
  - Print matches to screen
  - Progress bar support (tqdm)
  - Robust retry/backoff that waits and continues after transient network errors
    (including requests.exceptions.ReadTimeout)

Install:
  pip install requests tqdm

Usage:
  python match_osm_powerplants_to_wikidata.py \
      --input input.geojson \
      --output matched_only.geojson \
      --max-hits 5 \
      --sleep-s 0.05 \
      --timeout-s 30 \
      --max-retries 0

Notes:
  - By default, --max-retries 0 means "retry forever" on transient errors.
  - Backoff uses exponential growth with jitter and caps at --backoff-cap-s.
"""

from __future__ import annotations

import argparse
import json
import random
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


def is_transient_requests_error(exc: Exception) -> bool:
    # Network/timeouts/temporary DNS issues/etc.
    transient_types = (
        requests.exceptions.ReadTimeout,
        requests.exceptions.ConnectTimeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
    )
    if isinstance(exc, transient_types):
        return True
    return False


def is_retryable_http_status(status_code: int) -> bool:
    # Common transient server-side or rate-limit statuses
    return status_code in (429, 500, 502, 503, 504)


def sleep_backoff(attempt: int, base_s: float, cap_s: float) -> None:
    """
    Exponential backoff with jitter:
      sleep = min(cap, base * 2^(attempt-1)) * random(0.5..1.5)
    """
    exp = base_s * (2 ** max(0, attempt - 1))
    exp = min(cap_s, exp)
    jitter = random.uniform(0.5, 1.5)
    time.sleep(exp * jitter)


def wd_api_get(
    session: requests.Session,
    params: Dict[str, Any],
    timeout_s: float,
    max_retries: int,          # 0 => infinite
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Dict[str, Any]:
    """
    Robust GET with retry/backoff:
      - Retries forever by default on transient network errors and retryable HTTP statuses.
      - Prints a short message when backing off.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            r = session.get(WIKIDATA_API, params=params, timeout=timeout_s)

            # Retry on certain HTTP status codes
            if is_retryable_http_status(r.status_code):
                msg = f"HTTP {r.status_code} from Wikidata API"
                raise requests.exceptions.HTTPError(msg, response=r)

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
    params = {
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


def wbgetentities_p31(
    session: requests.Session,
    qids: List[str],
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Dict[str, List[str]]:
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
    entities = data.get("entities", {}) or {}

    result: Dict[str, List[str]] = {}
    for qid, entity in entities.items():
        claims = (entity or {}).get("claims", {}) or {}
        p31_claims = claims.get("P31", []) or []

        p31_values: List[str] = []
        for claim in p31_claims:
            try:
                val = claim["mainsnak"]["datavalue"]["value"]
                if val.get("entity-type") == "item":
                    pid = val.get("id")
                    if isinstance(pid, str) and pid.startswith("Q"):
                        p31_values.append(pid)
            except Exception:
                continue

        # de-dup
        result[qid] = sorted(set(p31_values))

    return result


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


def find_match(
    session: requests.Session,
    osm_name: str,
    limit: int,
    sleep_s: float,
    timeout_s: float,
    max_retries: int,
    backoff_base_s: float,
    backoff_cap_s: float,
) -> Optional[Dict[str, Any]]:
    """
    Tries:
      1) wbsearchentities without language param (Wikidata default behavior)
      2) wbsearchentities in English
    Accepts first hit whose P31 intersects ALLOWED_P31_QIDS.
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

    if not hits:
        return None

    qids = [h.get("id") for h in hits if isinstance(h.get("id"), str) and h["id"].startswith("Q")]
    if not qids:
        return None

    p31_map = wbgetentities_p31(
        session=session,
        qids=qids,
        timeout_s=timeout_s,
        max_retries=max_retries,
        backoff_base_s=backoff_base_s,
        backoff_cap_s=backoff_cap_s,
    )
    if sleep_s:
        time.sleep(sleep_s)

    for hit in hits:
        qid = hit.get("id")
        if not (isinstance(qid, str) and qid.startswith("Q")):
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-hits", type=int, default=5)
    ap.add_argument("--sleep-s", type=float, default=0.05)

    # Retry/backoff controls
    ap.add_argument("--timeout-s", type=float, default=30.0, help="HTTP timeout per request (default: 30)")
    ap.add_argument(
        "--max-retries",
        type=int,
        default=0,
        help="Max retries for transient errors (0 = retry forever; default: 0)",
    )
    ap.add_argument("--backoff-base-s", type=float, default=2.0, help="Backoff base seconds (default: 2)")
    ap.add_argument("--backoff-cap-s", type=float, default=120.0, help="Max backoff seconds (default: 120)")

    ap.add_argument(
        "--user-agent",
        default="osm-wikidata-name-instance-filter/1.2 (contact: you@example.com)",
        help="User-Agent string for polite Wikidata API usage",
    )
    args = ap.parse_args()

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

    iterator = tqdm(features, desc="Matching ") if tqdm else features

    for feat in iterator:
        osm_name = get_osm_name(feat)
        if not osm_name:
            continue

        match = find_match(
            session=session,
            osm_name=osm_name,
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

        out_features.append(feat)

        print(f"[MATCH] {osm_name} → {match['qid']} ({match['label']})")

    output = {"type": "FeatureCollection", "features": out_features}

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nDone. Matched {len(out_features)}/{len(features)} features.")
    print(f"Wrote: {args.output}")


if __name__ == "__main__":
    main()