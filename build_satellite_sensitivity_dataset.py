#!/usr/bin/env python3
"""
build_celestrak_ucs_dataset.py

Pulls Earth-orbiting satellites and debris from CelesTrak, then enriches
satellites with UCS Satellite Database metadata when a NORAD match is found.

Outputs a single JSON file that your VPython simulation can load later.

Install requirements:
    pip install requests openpyxl

Example:
    python3 build_celestrak_ucs_dataset.py --sat-limit 100 --debris-limit 100

Notes:
- CelesTrak should not be hammered. It updates GP data roughly every 2 hours,
  and repeated requests can trigger temporary 403 blocks. This script caches
  downloads by default.
- UCS updates are paused, so UCS is used for classification/enrichment only.
  CelesTrak remains the orbit source.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

try:
    import requests
except ImportError:
    print("Missing dependency: requests. Install with: pip install requests", file=sys.stderr)
    raise

try:
    import openpyxl
except ImportError:
    openpyxl = None

CELESTRAK_GP_URL = "https://celestrak.org/NORAD/elements/gp.php"
UCS_XLSX_URL = "https://www.ucs.org/sites/default/files/2024-01/UCS-Satellite-Database%205-1-2023.xlsx"

DEFAULT_CACHE_DIR = Path("data_cache")
DEFAULT_OUTPUT_PATH = Path("celestrak_ucs_orbit_dataset.json")

# CelesTrak group names can occasionally change or some groups can 403 due to
# rate limits. The script tries several commonly used debris-event groups and
# keeps whichever ones successfully return JSON.
DEFAULT_DEBRIS_GROUPS = [
    "COSMOS 2251 DEB",
    "IRIDIUM 33 DEB",
    "FENGYUN 1C DEB",
]

ACTIVE_GROUP = "active"


@dataclass
class DownloadResult:
    ok: bool
    source: str
    text: str = ""
    status_code: Optional[int] = None
    error: Optional[str] = None
    from_cache: bool = False


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


def find_first_key(row: Dict[str, Any], candidates: Iterable[str]) -> Optional[str]:
    lower_to_key = {str(k).lower().strip(): k for k in row.keys()}
    for candidate in candidates:
        key = lower_to_key.get(candidate.lower().strip())
        if key is not None:
            return key
    # Fuzzy fallback: remove punctuation/spaces.
    compact_to_key = {re.sub(r"[^a-z0-9]", "", str(k).lower()): k for k in row.keys()}
    for candidate in candidates:
        compact = re.sub(r"[^a-z0-9]", "", candidate.lower())
        key = compact_to_key.get(compact)
        if key is not None:
            return key
    return None


def cached_get_text(url: str, cache_path: Path, ttl_seconds: int, force_refresh: bool = False) -> DownloadResult:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not force_refresh:
        age = time.time() - cache_path.stat().st_mtime
        if age <= ttl_seconds:
            return DownloadResult(ok=True, source=url, text=cache_path.read_text(errors="replace"), from_cache=True)

    try:
        headers = {
            "User-Agent": "DryDockSatelliteSimulation/1.0 (+student research; respectful caching)",
            "Accept": "application/json,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
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
        return DownloadResult(ok=True, source=url, text=text, status_code=response.status_code)
    except Exception as exc:
        cached = cache_path.read_text(errors="replace") if cache_path.exists() else ""
        return DownloadResult(ok=bool(cached), source=url, text=cached, error=str(exc), from_cache=bool(cached))


def cached_get_bytes(url: str, cache_path: Path, ttl_seconds: int, force_refresh: bool = False) -> Tuple[bool, bytes, Optional[str], bool]:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not force_refresh:
        age = time.time() - cache_path.stat().st_mtime
        if age <= ttl_seconds:
            return True, cache_path.read_bytes(), None, True

    try:
        headers = {
            "User-Agent": "DryDockSatelliteSimulation/1.0 (+student research; respectful caching)",
            "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*",
        }
        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code != 200:
            if cache_path.exists():
                return True, cache_path.read_bytes(), f"HTTP {response.status_code}: {response.text[:300]}", True
            return False, b"", f"HTTP {response.status_code}: {response.text[:300]}", False
        cache_path.write_bytes(response.content)
        return True, response.content, None, False
    except Exception as exc:
        if cache_path.exists():
            return True, cache_path.read_bytes(), str(exc), True
        return False, b"", str(exc), False


def celestrak_group_url(group: str, fmt: str = "json") -> str:
    return f"{CELESTRAK_GP_URL}?GROUP={quote_plus(group)}&FORMAT={fmt}"


def fetch_celestrak_group(group: str, cache_dir: Path, ttl_seconds: int, force_refresh: bool = False) -> Tuple[List[Dict[str, Any]], DownloadResult]:
    url = celestrak_group_url(group, "json")
    cache_path = cache_dir / f"celestrak_gp_{safe_filename(group)}.json"
    result = cached_get_text(url, cache_path, ttl_seconds, force_refresh)
    if not result.ok:
        return [], result

    try:
        data = json.loads(result.text)
        if isinstance(data, dict) and "error" in data:
            return [], DownloadResult(ok=False, source=url, status_code=result.status_code, error=str(data), from_cache=result.from_cache)
        if not isinstance(data, list):
            return [], DownloadResult(ok=False, source=url, status_code=result.status_code, error="CelesTrak response was not a JSON list", from_cache=result.from_cache)
        return data, result
    except Exception as exc:
        return [], DownloadResult(ok=False, source=url, status_code=result.status_code, error=f"JSON parse error: {exc}", from_cache=result.from_cache)


def classify_orbit_class_from_gp(gp: Dict[str, Any]) -> str:
    # Mean motion, if available, gives a rough orbital period. This is only a
    # convenience label. Your sim can still choose its own orbit buckets.
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


def normalize_celestrak_object(gp: Dict[str, Any], object_type: str, source_group: str) -> Dict[str, Any]:
    norad_id = coerce_int(gp.get("NORAD_CAT_ID") or gp.get("OBJECT_ID") or gp.get("CATNR"))
    name = gp.get("OBJECT_NAME") or gp.get("OBJECT_NAME ") or gp.get("NAME") or gp.get("OBJECT") or "UNKNOWN"
    normalized = {
        "object_id": f"NORAD-{norad_id}" if norad_id is not None else normalize_name(name),
        "norad_cat_id": norad_id,
        "name": str(name).strip(),
        "object_type": object_type,
        "orbit_source": "celestrak_gp_json",
        "celestrak_group": source_group,
        "orbit_class_estimate": classify_orbit_class_from_gp(gp),
        "classification_source": "ucs" if object_type == "satellite" else "celestrak",
        "raw_celestrak_gp": gp,
        "orbit_elements": {
            "epoch": gp.get("EPOCH"),
            "mean_motion_rev_per_day": coerce_float(gp.get("MEAN_MOTION")),
            "eccentricity": coerce_float(gp.get("ECCENTRICITY")),
            "inclination_deg": coerce_float(gp.get("INCLINATION")),
            "raan_deg": coerce_float(gp.get("RA_OF_ASC_NODE")),
            "argument_of_perigee_deg": coerce_float(gp.get("ARG_OF_PERICENTER")),
            "mean_anomaly_deg": coerce_float(gp.get("MEAN_ANOMALY")),
            "ephemeris_type": gp.get("EPHEMERIS_TYPE"),
            "element_set_no": gp.get("ELEMENT_SET_NO"),
            "rev_at_epoch": gp.get("REV_AT_EPOCH"),
            "bstar": coerce_float(gp.get("BSTAR")),
        },
    }
    return normalized


def load_ucs_database(cache_dir: Path, ttl_seconds: int, force_refresh: bool = False) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    meta = {
        "source_url": UCS_XLSX_URL,
        "loaded": False,
        "from_cache": False,
        "rows": 0,
        "matched_by": ["norad_cat_id", "normalized_name_fallback"],
        "warning": None,
    }
    if openpyxl is None:
        meta["warning"] = "openpyxl not installed; UCS enrichment disabled. Install with: pip install openpyxl"
        return {}, {}, meta

    ok, content, error, from_cache = cached_get_bytes(UCS_XLSX_URL, cache_dir / "ucs_satellite_database.xlsx", ttl_seconds, force_refresh)
    meta["from_cache"] = from_cache
    if not ok:
        meta["warning"] = f"Could not load UCS database: {error}"
        return {}, {}, meta
    if error:
        meta["warning"] = f"Using cached UCS database because refresh failed: {error}"

    by_norad: Dict[int, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        headers = [str(h).strip() if h is not None else "" for h in next(rows)]
    except StopIteration:
        meta["warning"] = "UCS workbook was empty"
        return {}, {}, meta

    for values in rows:
        row = {headers[i]: values[i] if i < len(values) else None for i in range(len(headers))}
        norad_key = find_first_key(row, ["NORAD Number", "NORAD", "NORAD Cat ID", "NORAD Catalog Number"])
        name_key = find_first_key(row, ["Current Official Name of Satellite", "Name of Satellite, Alternate Names", "Satellite Name", "Name"])
        norad = coerce_int(row.get(norad_key)) if norad_key else None
        sat_name = row.get(name_key) if name_key else None
        if norad is None and not sat_name:
            continue

        def get(candidates: List[str]) -> Any:
            k = find_first_key(row, candidates)
            return row.get(k) if k else None

        ucs = {
            "ucs_name": None if sat_name is None else str(sat_name).strip(),
            "norad_cat_id": norad,
            "country_of_operator_owner": get(["Country of Operator/Owner", "Country/Org of UN Registry", "Country"]),
            "operator_owner": get(["Operator/Owner", "Owner", "Operator"]),
            "users": get(["Users", "User"]),
            "purpose": get(["Purpose", "Detailed Purpose"]),
            "detailed_purpose": get(["Detailed Purpose", "Purpose"]),
            "orbit_class": get(["Class of Orbit", "Orbit Class"]),
            "orbit_type": get(["Type of Orbit", "Orbit Type"]),
            "launch_mass_kg": coerce_float(get(["Launch Mass (kg.)", "Launch Mass (kg)", "Mass (kg)"])),
            "dry_mass_kg": coerce_float(get(["Dry Mass (kg.)", "Dry Mass (kg)"])),
            "power_watts": coerce_float(get(["Power (watts)", "Power"])),
            "launch_date": get(["Date of Launch", "Launch Date"]),
            "expected_lifetime_years": coerce_float(get(["Expected Lifetime (yrs.)", "Expected Lifetime"])),
            "contractor": get(["Contractor"]),
            "launch_site": get(["Launch Site"]),
            "launch_vehicle": get(["Launch Vehicle"]),
            "cospar_number": get(["COSPAR Number", "COSPAR"]),
            "comments": get(["Comments"]),
        }
        if norad is not None:
            by_norad[norad] = ucs
        if sat_name:
            by_name[normalize_name(sat_name)] = ucs
        meta["rows"] += 1

    meta["loaded"] = True
    return by_norad, by_name, meta


def enrich_satellite_with_ucs(sat: Dict[str, Any], ucs_by_norad: Dict[int, Dict[str, Any]], ucs_by_name: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    norad = sat.get("norad_cat_id")
    match = ucs_by_norad.get(norad) if norad is not None else None
    match_method = "norad_cat_id" if match is not None else None
    if match is None:
        match = ucs_by_name.get(normalize_name(sat.get("name")))
        match_method = "normalized_name" if match is not None else None

    sat["ucs_match"] = {
        "matched": bool(match),
        "match_method": match_method,
    }
    if match:
        sat["ucs_metadata"] = match
        # These top-level fields make your sim/routing code easier to read.
        sat["purpose"] = match.get("purpose")
        sat["users"] = match.get("users")
        sat["operator_owner"] = match.get("operator_owner")
        sat["country_of_operator_owner"] = match.get("country_of_operator_owner")
        sat["ucs_orbit_class"] = match.get("orbit_class")
        sat["ucs_orbit_type"] = match.get("orbit_type")
        sat["mass_kg_from_ucs"] = match.get("launch_mass_kg") or match.get("dry_mass_kg")
    else:
        sat["ucs_metadata"] = None
        sat["purpose"] = None
        sat["users"] = None
        sat["operator_owner"] = None
        sat["country_of_operator_owner"] = None
        sat["mass_kg_from_ucs"] = None
    return sat


def dedupe_by_norad(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[Any] = set()
    deduped: List[Dict[str, Any]] = []
    for obj in objects:
        key = obj.get("norad_cat_id") or obj.get("object_id") or obj.get("name")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(obj)
    return deduped


def build_dataset(args: argparse.Namespace) -> Dict[str, Any]:
    cache_dir = Path(args.cache_dir)

    sat_rows, sat_download = fetch_celestrak_group(ACTIVE_GROUP, cache_dir, args.cache_ttl_seconds, args.force_refresh)
    satellites = [normalize_celestrak_object(row, "satellite", ACTIVE_GROUP) for row in sat_rows]

    if args.sat_limit is not None:
        satellites = satellites[: max(0, args.sat_limit)]

    debris_objects: List[Dict[str, Any]] = []
    debris_downloads: List[Dict[str, Any]] = []
    for group in args.debris_groups:
        rows, dl = fetch_celestrak_group(group, cache_dir, args.cache_ttl_seconds, args.force_refresh)
        debris_downloads.append({
            "group": group,
            "ok": dl.ok,
            "source": dl.source,
            "status_code": dl.status_code,
            "from_cache": dl.from_cache,
            "error": dl.error,
            "rows": len(rows),
        })
        debris_objects.extend(normalize_celestrak_object(row, "debris", group) for row in rows)

    debris_objects = dedupe_by_norad(debris_objects)
    if args.debris_limit is not None:
        debris_objects = debris_objects[: max(0, args.debris_limit)]

    ucs_by_norad, ucs_by_name, ucs_meta = load_ucs_database(cache_dir, args.ucs_cache_ttl_seconds, args.force_refresh)
    satellites = [enrich_satellite_with_ucs(sat, ucs_by_norad, ucs_by_name) for sat in satellites]

    matched_count = sum(1 for s in satellites if s.get("ucs_match", {}).get("matched"))

    dataset = {
        "schema": "drydock.celestrak_ucs_orbit_dataset.v1",
        "created_utc_iso": utc_now_iso(),
        "notes": [
            "CelesTrak is the orbit source for satellites and debris.",
            "UCS is used only to enrich/classify satellites, not debris.",
            "Debris objects usually do not have UCS metadata because UCS tracks operational satellites.",
            "Raw CelesTrak GP records are preserved under raw_celestrak_gp for traceability.",
        ],
        "sources": {
            "celestrak_gp_documentation": "https://celestrak.org/NORAD/documentation/gp-data-formats.php",
            "celestrak_active_group_url": celestrak_group_url(ACTIVE_GROUP, "json"),
            "ucs_database_url": UCS_XLSX_URL,
            "ucs_metadata": ucs_meta,
        },
        "download_status": {
            "active_satellites": {
                "ok": sat_download.ok,
                "source": sat_download.source,
                "status_code": sat_download.status_code,
                "from_cache": sat_download.from_cache,
                "error": sat_download.error,
                "rows_before_limit": len(sat_rows),
            },
            "debris_groups": debris_downloads,
        },
        "counts": {
            "satellites": len(satellites),
            "satellites_with_ucs_match": matched_count,
            "satellites_without_ucs_match": len(satellites) - matched_count,
            "debris": len(debris_objects),
            "total_objects": len(satellites) + len(debris_objects),
        },
        "satellites": satellites,
        "debris": debris_objects,
    }
    return dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a CelesTrak satellite/debris dataset enriched with UCS satellite metadata.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output JSON path.")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="Cache folder for CelesTrak/UCS downloads.")
    parser.add_argument("--cache-ttl-seconds", type=int, default=7200, help="CelesTrak cache TTL. 7200 seconds respects the roughly 2-hour update cadence.")
    parser.add_argument("--ucs-cache-ttl-seconds", type=int, default=7 * 24 * 3600, help="UCS cache TTL. UCS changes rarely, so default is 7 days.")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore cache and redownload. Do not spam CelesTrak with this.")
    parser.add_argument("--sat-limit", type=int, default=100, help="Limit active satellites included. Use -1 for no limit.")
    parser.add_argument("--debris-limit", type=int, default=100, help="Limit debris included. Use -1 for no limit.")
    parser.add_argument("--debris-groups", nargs="*", default=DEFAULT_DEBRIS_GROUPS, help="CelesTrak debris groups to try.")
    args = parser.parse_args()
    if args.sat_limit is not None and args.sat_limit < 0:
        args.sat_limit = None
    if args.debris_limit is not None and args.debris_limit < 0:
        args.debris_limit = None
    return args


def main() -> int:
    args = parse_args()
    dataset = build_dataset(args)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(dataset, indent=2, default=str) + "\n")
    temp_path.replace(output_path)

    print("Dataset written:", output_path)
    print(json.dumps(dataset["counts"], indent=2))
    if dataset["download_status"]["active_satellites"].get("error"):
        print("Active satellite download note:", dataset["download_status"]["active_satellites"]["error"])
    failed_debris = [d for d in dataset["download_status"]["debris_groups"] if not d["ok"]]
    if failed_debris:
        print("Some debris groups failed. The script kept the groups that worked:")
        for d in failed_debris:
            print(f"  - {d['group']}: {d.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
