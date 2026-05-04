#!/usr/bin/env python3
"""
build_debris_dataset.py

Builds a debris-only dataset from CelesTrak GP data.

This script searches debris by NAME instead of GROUP because debris families like:
    COSMOS 2251 DEB
    IRIDIUM 33 DEB
    FENGYUN 1C DEB

are better queried through:
    https://celestrak.org/NORAD/elements/gp.php?NAME=COSMOS+2251+DEB&FORMAT=json

rather than:
    GROUP=debris

Install:
    pip install requests

Run:
    python3 build_debris_dataset.py

Examples:
    python3 build_debris_dataset.py --limit 100
    python3 build_debris_dataset.py --debris-names "COSMOS 2251 DEB" --limit 100
    python3 build_debris_dataset.py --force-refresh
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

try:
    import requests
except ImportError:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise


CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

DEFAULT_CACHE_DIR = Path("data_cache_debris")
DEFAULT_OUTPUT_PATH = Path("celestrak_debris_dataset.json")

DEFAULT_DEBRIS_NAMES = [
    "COSMOS 2251 DEB",
    "IRIDIUM 33 DEB",
    "FENGYUN 1C DEB",
]

# CelesTrak GP data updates roughly every 2 hours.
DEFAULT_CACHE_TTL_SECONDS = 7200


@dataclass
class DownloadResult:
    ok: bool
    source: str
    text: str = ""
    status_code: Optional[int] = None
    error: Optional[str] = None
    from_cache: bool = False
    format_used: Optional[str] = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(value: str) -> str:
    value = value.strip().replace(" ", "_")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def normalize_name(name: Any) -> str:
    if name is None:
        return ""
    s = str(name).upper().strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^A-Z0-9 ]", "", s)
    return s


def coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None

    s = str(value).strip()

    if s == "" or s.upper() in {"NR", "N/A", "NA", "NONE", "UNKNOWN"}:
        return None

    s = re.sub(r"\.0$", "", s)
    s = re.sub(r"[^0-9-]", "", s)

    if s in {"", "-"}:
        return None

    try:
        return int(s)
    except ValueError:
        return None


def coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None

    s = str(value).strip().replace(",", "")

    if s == "" or s.upper() in {"NR", "N/A", "NA", "NONE", "UNKNOWN"}:
        return None

    try:
        return float(s)
    except ValueError:
        return None


def celestrak_name_url(name: str, fmt: str) -> str:
    return f"{CELESTRAK_GP_URL}?NAME={quote_plus(name)}&FORMAT={fmt}"


def cached_get_text(
    url: str,
    cache_path: Path,
    ttl_seconds: int,
    force_refresh: bool = False,
) -> DownloadResult:
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.exists() and not force_refresh:
        age = time.time() - cache_path.stat().st_mtime

        if age <= ttl_seconds:
            return DownloadResult(
                ok=True,
                source=url,
                text=cache_path.read_text(errors="replace"),
                from_cache=True,
            )

    try:
        headers = {
            "User-Agent": "DryDockSatelliteSimulation/1.0 (+student research; respectful caching)",
            "Accept": "application/json,text/csv,text/plain,*/*",
        }

        response = requests.get(url, headers=headers, timeout=30)

        if response.status_code != 200:
            cached = cache_path.read_text(errors="replace") if cache_path.exists() else ""

            return DownloadResult(
                ok=bool(cached),
                source=url,
                text=cached,
                status_code=response.status_code,
                error=response.text[:500],
                from_cache=bool(cached),
            )

        text = response.text
        cache_path.write_text(text)

        return DownloadResult(
            ok=True,
            source=url,
            text=text,
            status_code=response.status_code,
        )

    except Exception as exc:
        cached = cache_path.read_text(errors="replace") if cache_path.exists() else ""

        return DownloadResult(
            ok=bool(cached),
            source=url,
            text=cached,
            error=str(exc),
            from_cache=bool(cached),
        )


def parse_json_rows(text: str) -> List[Dict[str, Any]]:
    data = json.loads(text)

    if not isinstance(data, list):
        raise ValueError("CelesTrak JSON response was not a list")

    return data


def parse_csv_rows(text: str) -> List[Dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        raise ValueError("CelesTrak CSV response had no rows")

    return rows


def fetch_celestrak_debris_name(
    debris_name: str,
    cache_dir: Path,
    ttl_seconds: int,
    force_refresh: bool = False,
) -> tuple[List[Dict[str, Any]], DownloadResult]:
    """
    Try JSON first.
    If JSON fails, try CSV.
    """

    json_url = celestrak_name_url(debris_name, "json")
    json_cache = cache_dir / f"celestrak_name_{safe_filename(debris_name)}.json"

    json_result = cached_get_text(
        url=json_url,
        cache_path=json_cache,
        ttl_seconds=ttl_seconds,
        force_refresh=force_refresh,
    )

    if json_result.ok:
        try:
            rows = parse_json_rows(json_result.text)
            json_result.format_used = "json"
            return rows, json_result
        except Exception as exc:
            json_result.error = f"JSON parse error: {exc}"

    csv_url = celestrak_name_url(debris_name, "csv")
    csv_cache = cache_dir / f"celestrak_name_{safe_filename(debris_name)}.csv"

    csv_result = cached_get_text(
        url=csv_url,
        cache_path=csv_cache,
        ttl_seconds=ttl_seconds,
        force_refresh=force_refresh,
    )

    if csv_result.ok:
        try:
            rows = parse_csv_rows(csv_result.text)
            csv_result.format_used = "csv"
            return rows, csv_result
        except Exception as exc:
            csv_result.error = f"CSV parse error: {exc}"

    return [], DownloadResult(
        ok=False,
        source=f"{json_url} OR {csv_url}",
        status_code=json_result.status_code or csv_result.status_code,
        error=f"JSON failed: {json_result.error}; CSV failed: {csv_result.error}",
        from_cache=json_result.from_cache or csv_result.from_cache,
    )


def classify_orbit_class_from_gp(gp: Dict[str, Any]) -> str:
    mean_motion = coerce_float(gp.get("MEAN_MOTION"))

    if mean_motion and mean_motion > 0:
        period_minutes = 1440.0 / mean_motion

        if period_minutes < 128:
            return "LEO"

        if 128 <= period_minutes < 700:
            return "MEO"

        if 1300 <= period_minutes <= 1500:
            return "GEO"

        return "HEO_OR_OTHER"

    return "unknown"


def estimate_semimajor_axis_km(mean_motion_rev_per_day: Optional[float]) -> Optional[float]:
    """
    Estimate semi-major axis from mean motion.

    n = rev/day converted to rad/sec
    a = (mu / n^2)^(1/3)
    """

    if mean_motion_rev_per_day is None or mean_motion_rev_per_day <= 0:
        return None

    mu_earth_km3_s2 = 398600.4418
    n_rad_s = mean_motion_rev_per_day * 2.0 * math.pi / 86400.0
    a_km = (mu_earth_km3_s2 / (n_rad_s * n_rad_s)) ** (1.0 / 3.0)

    return a_km


def normalize_debris_object(gp: Dict[str, Any], debris_name_query: str) -> Dict[str, Any]:
    norad_id = coerce_int(
        gp.get("NORAD_CAT_ID")
        or gp.get("NORAD_CAT_ID ")
        or gp.get("CATNR")
        or gp.get("OBJECT_ID")
    )

    name = (
        gp.get("OBJECT_NAME")
        or gp.get("OBJECT_NAME ")
        or gp.get("NAME")
        or gp.get("OBJECT")
        or "UNKNOWN_DEBRIS"
    )

    mean_motion = coerce_float(gp.get("MEAN_MOTION"))
    semimajor_axis_km = estimate_semimajor_axis_km(mean_motion)

    return {
        "object_id": f"NORAD-{norad_id}" if norad_id is not None else normalize_name(name),
        "norad_cat_id": norad_id,
        "name": str(name).strip(),
        "object_type": "debris",
        "orbit_source": "celestrak_gp",
        "celestrak_name_query": debris_name_query,
        "classification_source": "celestrak",
        "orbit_class_estimate": classify_orbit_class_from_gp(gp),

        # CelesTrak GP data usually does not include debris mass.
        # Keep this null instead of pretending it is zero.
        "mass_kg": None,
        "mass_source": None,

        "orbit_elements": {
            "epoch": gp.get("EPOCH"),
            "mean_motion_rev_per_day": mean_motion,
            "eccentricity": coerce_float(gp.get("ECCENTRICITY")),
            "inclination_deg": coerce_float(gp.get("INCLINATION")),
            "raan_deg": coerce_float(gp.get("RA_OF_ASC_NODE")),
            "argument_of_perigee_deg": coerce_float(gp.get("ARG_OF_PERICENTER")),
            "mean_anomaly_deg": coerce_float(gp.get("MEAN_ANOMALY")),
            "ephemeris_type": gp.get("EPHEMERIS_TYPE"),
            "element_set_no": gp.get("ELEMENT_SET_NO"),
            "rev_at_epoch": gp.get("REV_AT_EPOCH"),
            "bstar": coerce_float(gp.get("BSTAR")),
            "estimated_semimajor_axis_km": semimajor_axis_km,
        },

        "raw_celestrak_gp": gp,
    }


def dedupe_by_norad(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    deduped = []

    for obj in objects:
        key = obj.get("norad_cat_id") or obj.get("object_id") or obj.get("name")

        if key in seen:
            continue

        seen.add(key)
        deduped.append(obj)

    return deduped


def build_debris_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    cache_dir = Path(args.cache_dir)

    all_debris: List[Dict[str, Any]] = []
    download_status: List[Dict[str, Any]] = []

    for debris_name in args.debris_names:
        rows, dl = fetch_celestrak_debris_name(
            debris_name=debris_name,
            cache_dir=cache_dir,
            ttl_seconds=args.cache_ttl_seconds,
            force_refresh=args.force_refresh,
        )

        download_status.append({
            "debris_name": debris_name,
            "ok": dl.ok,
            "source": dl.source,
            "status_code": dl.status_code,
            "from_cache": dl.from_cache,
            "format_used": dl.format_used,
            "error": dl.error,
            "rows_before_limit": len(rows),
        })

        for row in rows:
            all_debris.append(normalize_debris_object(row, debris_name))

    all_debris = dedupe_by_norad(all_debris)

    if args.limit is not None:
        all_debris = all_debris[: max(0, args.limit)]

    dataset = {
        "schema": "drydock.celestrak_debris_dataset.v1",
        "created_utc_iso": utc_now_iso(),
        "notes": [
            "This is a debris-only dataset.",
            "CelesTrak GP data is the orbit source.",
            "Debris is queried by NAME, not GROUP.",
            "Debris mass is usually not available from CelesTrak GP data, so mass_kg is null.",
            "Use this file alongside the satellite dataset in the VPython simulation.",
            "This script uses caching to avoid repeatedly hitting CelesTrak.",
        ],
        "sources": {
            "celestrak_gp_base_url": CELESTRAK_GP_URL,
            "debris_name_urls": [
                celestrak_name_url(name, "json") for name in args.debris_names
            ],
        },
        "download_status": {
            "debris_names": download_status,
        },
        "counts": {
            "debris": len(all_debris),
            "total_objects": len(all_debris),
        },
        "debris": all_debris,
    }

    return dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a debris-only CelesTrak dataset."
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="Output JSON path.",
    )

    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help="Cache folder for debris downloads.",
    )

    parser.add_argument(
        "--cache-ttl-seconds",
        type=int,
        default=DEFAULT_CACHE_TTL_SECONDS,
        help="Cache TTL in seconds. Default is 7200 seconds.",
    )

    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore cache and redownload. Do not spam CelesTrak with this.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Limit debris included. Use -1 for no limit.",
    )

    parser.add_argument(
        "--debris-names",
        nargs="*",
        default=DEFAULT_DEBRIS_NAMES,
        help="Debris names to search on CelesTrak. Example: COSMOS 2251 DEB",
    )

    args = parser.parse_args()

    if args.limit is not None and args.limit < 0:
        args.limit = None

    return args


def main() -> int:
    args = parse_args()

    dataset = build_debris_dataset(args)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(dataset, indent=2, default=str) + "\n")
    temp_path.replace(output_path)

    print("Debris dataset written:", output_path)
    print(json.dumps(dataset["counts"], indent=2))

    failed = [
        d for d in dataset["download_status"]["debris_names"]
        if not d["ok"]
    ]

    if failed:
        print("Some debris names failed:")
        for d in failed:
            print(f"  - {d['debris_name']}: {d.get('error')}")

    successful = [
        d for d in dataset["download_status"]["debris_names"]
        if d["ok"]
    ]

    if successful:
        print("Successful debris names:")
        for d in successful:
            cache_note = "cache" if d.get("from_cache") else "network"
            fmt = d.get("format_used") or "unknown"
            rows = d.get("rows_before_limit")
            print(f"  - {d['debris_name']}: {rows} rows via {fmt} from {cache_note}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
