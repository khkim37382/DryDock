#!/usr/bin/env python3
"""
build_satellite_sensitivity_dataset.py

Separate DryDock dataset-generator script.

Creates, in the folder you run it from:
  - satellite_classification_lookup.json
  - satellite_classification_lookup.csv

Each satellite gets:
  - norad_id
  - name
  - classification_label
  - confidence
  - operator/users/purpose when matched from UCS
  - optional mass fields from UCS when available

Data sources:
  - CelesTrak active satellite list: no API key needed
  - UCS Satellite Database Excel file: no API key needed

Install:
  python3 -m pip install requests pandas openpyxl

Run from inside DryDock:
  python3 build_satellite_sensitivity_dataset.py

Test:
  python3 build_satellite_sensitivity_dataset.py --limit 100

If CelesTrak blocks Python with 403 or rate/cache messages:
  1. Open this URL in your browser:
     https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json
  2. Save the page as data/celestrak_active.json
  3. Run:
     python3 build_satellite_sensitivity_dataset.py --celestrak-file data/celestrak_active.json

Important:
  This script only uses celestrak.org for CelesTrak to avoid SSL hostname mismatch errors.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

CELESTRAK_ACTIVE_JSON_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json"
CELESTRAK_ACTIVE_CSV_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=csv"
CELESTRAK_ACTIVE_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=tle"
CELESTRAK_OLD_ACTIVE_TLE_URL = "https://celestrak.org/NORAD/elements/active.txt"
UCS_XLSX_URL = "https://www.ucs.org/sites/default/files/2024-01/UCS-Satellite-Database%205-1-2023.xlsx"

DEFAULT_CACHE_FILE = Path("data/celestrak_active.json")

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36 DryDockDatasetGenerator/3.0"
    ),
    "Accept": "application/json,text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://celestrak.org/",
    "Connection": "close",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "n/a", "na", "unknown", "-"}:
            return None
        return int(float(text))
    except Exception:
        return None


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        text = str(value).strip().replace(",", "")
        if not text or text.lower() in {"nan", "n/a", "na", "unknown", "-"}:
            return None
        return float(text)
    except Exception:
        return None


def normalize_name(value: Any) -> str:
    text = clean(value).upper()
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def find_column(columns: List[str], candidates: List[str]) -> Optional[str]:
    exact = {c.lower().strip(): c for c in columns}
    for candidate in candidates:
        key = candidate.lower().strip()
        if key in exact:
            return exact[key]
    for col in columns:
        low = col.lower().strip()
        for candidate in candidates:
            if candidate.lower().strip() in low:
                return col
    return None


def get_with_retries(url: str, timeout: int = 90) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=BROWSER_HEADERS, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(1.5 * (attempt + 1))
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def parse_tle_text(text: str) -> List[Dict[str, Any]]:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    objects: List[Dict[str, Any]] = []
    i = 0
    while i < len(lines):
        if i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
            name = lines[i].strip()
            norad_id = safe_int(lines[i + 1][2:7])
            objects.append({"OBJECT_NAME": name, "NORAD_CAT_ID": norad_id})
            i += 3
        else:
            i += 1
    return objects


def load_celestrak_from_file(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    # CelesTrak sometimes returns a plain text "not updated" message. This is not usable as data.
    if "GP data has not updated" in text[:300] or "Data is updated once every" in text[:300]:
        raise ValueError(
            f"{path} appears to contain a CelesTrak cache/status message, not satellite JSON/CSV/TLE data. "
            "Open the CelesTrak URL in your browser and save the actual JSON list."
        )

    if suffix == ".json" or text.lstrip().startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("Local CelesTrak JSON file must contain a list of objects.")
        return data

    if suffix == ".csv" or "NORAD_CAT_ID" in text[:500]:
        df = pd.read_csv(io.StringIO(text))
        return df.to_dict(orient="records")

    objects = parse_tle_text(text)
    if objects:
        return objects

    raise ValueError("Could not read local CelesTrak file as JSON, CSV, or TLE text.")


def save_cache_if_json(objects: List[Dict[str, Any]], cache_file: Path = DEFAULT_CACHE_FILE) -> None:
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(objects, indent=2), encoding="utf-8")
    except Exception:
        # Cache is helpful but not required.
        pass


def fetch_celestrak_active(local_file: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], str]:
    if local_file:
        if not local_file.exists():
            raise FileNotFoundError(f"Local CelesTrak file not found: {local_file}")
        return load_celestrak_from_file(local_file), f"local_file:{local_file}"

    errors: List[str] = []

    # Try JSON first. Only use celestrak.org for CelesTrak.
    try:
        response = get_with_retries(CELESTRAK_ACTIVE_JSON_URL)
        if response.ok:
            text = response.text.strip()
            if "GP data has not updated" in text[:300] or "Data is updated once every" in text[:300]:
                errors.append(f"{CELESTRAK_ACTIVE_JSON_URL}: CelesTrak says data has not updated")
                if DEFAULT_CACHE_FILE.exists():
                    return load_celestrak_from_file(DEFAULT_CACHE_FILE), f"cached_file:{DEFAULT_CACHE_FILE}"
            else:
                data = response.json()
                if isinstance(data, list):
                    save_cache_if_json(data)
                    return data, CELESTRAK_ACTIVE_JSON_URL
                errors.append(f"{CELESTRAK_ACTIVE_JSON_URL}: JSON response was not a list")
        else:
            errors.append(f"{CELESTRAK_ACTIVE_JSON_URL}: HTTP {response.status_code}")
    except Exception as exc:
        errors.append(f"{CELESTRAK_ACTIVE_JSON_URL}: {exc}")

    # Try CSV endpoint.
    try:
        response = get_with_retries(CELESTRAK_ACTIVE_CSV_URL)
        if response.ok:
            df = pd.read_csv(io.StringIO(response.text))
            objects = df.to_dict(orient="records")
            save_cache_if_json(objects)
            return objects, CELESTRAK_ACTIVE_CSV_URL
        errors.append(f"{CELESTRAK_ACTIVE_CSV_URL}: HTTP {response.status_code}")
    except Exception as exc:
        errors.append(f"{CELESTRAK_ACTIVE_CSV_URL}: {exc}")

    # Try TLE endpoints.
    for url in [CELESTRAK_ACTIVE_TLE_URL, CELESTRAK_OLD_ACTIVE_TLE_URL]:
        try:
            response = get_with_retries(url)
            if response.ok:
                objects = parse_tle_text(response.text)
                if objects:
                    save_cache_if_json(objects)
                    return objects, url
                errors.append(f"{url}: TLE parse returned zero objects")
            else:
                errors.append(f"{url}: HTTP {response.status_code}")
        except Exception as exc:
            errors.append(f"{url}: {exc}")

    # Final automatic fallback: use previous cache if available.
    if DEFAULT_CACHE_FILE.exists():
        return load_celestrak_from_file(DEFAULT_CACHE_FILE), f"cached_file:{DEFAULT_CACHE_FILE}"

    raise RuntimeError(
        "Could not download CelesTrak active satellite data. Attempts:\n  - "
        + "\n  - ".join(errors)
        + "\n\nManual fallback:\n"
        + "Open https://celestrak.org/NORAD/elements/gp.php?GROUP=active&FORMAT=json in a browser, "
        + "save it as data/celestrak_active.json, then run:\n"
        + "python3 build_satellite_sensitivity_dataset.py --celestrak-file data/celestrak_active.json"
    )


def download_ucs_file(ucs_file: Path) -> None:
    ucs_file.parent.mkdir(parents=True, exist_ok=True)
    response = get_with_retries(UCS_XLSX_URL)
    response.raise_for_status()
    ucs_file.write_bytes(response.content)


def load_ucs_lookup(ucs_file: Path) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    if not ucs_file.exists():
        print(f"UCS file not found. Downloading to {ucs_file}...")
        download_ucs_file(ucs_file)

    df = pd.read_excel(ucs_file)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    df.columns = [str(c).strip() for c in df.columns]
    columns = list(df.columns)

    col_name = find_column(columns, ["Current Official Name of Satellite", "Name of Satellite", "Satellite"])
    col_alt_names = find_column(columns, ["Alternate Names"])
    col_norad = find_column(columns, ["NORAD Number", "NORAD Cat ID", "NORAD_CAT_ID", "Catalog Number"])
    col_operator = find_column(columns, ["Operator/Owner", "Operator", "Owner"])
    col_users = find_column(columns, ["Users"])
    col_purpose = find_column(columns, ["Purpose"])
    col_detailed_purpose = find_column(columns, ["Detailed Purpose"])
    col_country = find_column(columns, ["Country of Operator/Owner"])
    col_launch_mass = find_column(columns, ["Launch Mass", "Launch mass", "Mass at launch"])
    col_dry_mass = find_column(columns, ["Dry Mass", "dry mass"])
    col_power = find_column(columns, ["Power", "Watts"])

    by_norad: Dict[int, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}

    for _, row in df.iterrows():
        record = {
            "ucs_name": clean(row.get(col_name)) if col_name else "",
            "alternate_names": clean(row.get(col_alt_names)) if col_alt_names else "",
            "norad_id": safe_int(row.get(col_norad)) if col_norad else None,
            "operator": clean(row.get(col_operator)) if col_operator else "",
            "users": clean(row.get(col_users)) if col_users else "",
            "purpose": clean(row.get(col_purpose)) if col_purpose else "",
            "detailed_purpose": clean(row.get(col_detailed_purpose)) if col_detailed_purpose else "",
            "country": clean(row.get(col_country)) if col_country else "",
            "launch_mass_kg": safe_float(row.get(col_launch_mass)) if col_launch_mass else None,
            "dry_mass_kg": safe_float(row.get(col_dry_mass)) if col_dry_mass else None,
            "power_watts": safe_float(row.get(col_power)) if col_power else None,
        }

        if record["norad_id"] is not None:
            by_norad[record["norad_id"]] = record

        for raw_name in [record["ucs_name"], record["alternate_names"]]:
            if not raw_name:
                continue
            for part in re.split(r"[,;/]", raw_name):
                key = normalize_name(part)
                if key:
                    by_name[key] = record

    return by_norad, by_name


def estimate_mass_kg(name: str, ucs_record: Optional[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    if ucs_record:
        launch_mass = ucs_record.get("launch_mass_kg")
        dry_mass = ucs_record.get("dry_mass_kg")
        if launch_mass is not None:
            return launch_mass, "ucs_launch_mass"
        if dry_mass is not None:
            return dry_mass, "ucs_dry_mass"

    text = name.lower()
    if "cubesat" in text:
        return 12.0, "estimate_cubesat"
    if "starlink" in text:
        return 260.0, "estimate_starlink"
    if "oneweb" in text:
        return 150.0, "estimate_oneweb"
    return None, "unknown"


def match_ucs_record(celestrak_obj: Dict[str, Any], by_norad: Dict[int, Dict[str, Any]], by_name: Dict[str, Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], str]:
    norad_id = safe_int(
        celestrak_obj.get("NORAD_CAT_ID")
        or celestrak_obj.get("NORAD_CATID")
        or celestrak_obj.get("NORAD")
        or celestrak_obj.get("CATNR")
        or celestrak_obj.get("OBJECT_ID")
    )
    if norad_id is not None and norad_id in by_norad:
        return by_norad[norad_id], "norad_id"

    name = clean(celestrak_obj.get("OBJECT_NAME") or celestrak_obj.get("NAME") or celestrak_obj.get("OBJECT") or celestrak_obj.get("SATNAME"))
    name_key = normalize_name(name)
    if name_key in by_name:
        return by_name[name_key], "exact_name"

    for candidate_key, record in by_name.items():
        if len(candidate_key) >= 8 and (candidate_key in name_key or name_key in candidate_key):
            return record, "loose_name"

    return None, "unmatched"


def classify_satellite(name: str, ucs_record: Optional[Dict[str, Any]]) -> Tuple[str, float, str]:
    if not ucs_record:
        return "unknown_sensitive", 0.55, "No UCS match found, so operator/purpose are unknown from the public metadata used here."

    operator = ucs_record.get("operator", "")
    users = ucs_record.get("users", "")
    purpose = ucs_record.get("purpose", "")
    detailed_purpose = ucs_record.get("detailed_purpose", "")
    country = ucs_record.get("country", "")
    text = " ".join([name, operator, users, purpose, detailed_purpose, country]).lower()

    defense_keywords = [
        "military", "defense", "defence", "space force", "air force", "army", "navy",
        "missile", "early warning", "reconnaissance", "surveillance", "intelligence",
        "sigint", "elint", "military communications", "military surveillance",
        "optical imaging/military", "radar imaging/military", "communications/intelligence",
    ]
    government_keywords = [
        "government", "civil/military", "military/civil", "ministry", "agency",
        "national", "department of defense", "department of defence",
    ]
    commercial_sensitive_keywords = [
        "earth observation", "remote sensing", "imaging", "radar imaging",
        "synthetic aperture radar", "sar", "communications", "broadband", "internet",
        "iot", "data relay", "navigation", "positioning", "ais", "ship tracking",
        "signals",
    ]
    public_keywords = [
        "amateur", "education", "educational", "technology demonstration", "science",
        "scientific", "weather", "meteorology", "space science", "earth science",
    ]

    if any(keyword in text for keyword in defense_keywords):
        return "defense_sensitive", 0.90, "Public metadata suggests military/defense/intelligence-related use."
    if any(keyword in text for keyword in government_keywords):
        return "government_sensitive", 0.78, "Public metadata suggests government or mixed civil/military involvement."
    if any(keyword in text for keyword in commercial_sensitive_keywords):
        return "commercial_sensitive", 0.72, "Public metadata suggests commercially sensitive sensing/communications/navigation use."
    if any(keyword in text for keyword in public_keywords):
        return "public", 0.82, "Public metadata suggests civil/scientific/educational use."
    if not operator or not purpose:
        return "unknown_sensitive", 0.60, "UCS match found, but operator or purpose is incomplete."
    return "public", 0.70, "No sensitive keywords found in the public metadata used here."


def get_obj_name(obj: Dict[str, Any]) -> str:
    return clean(obj.get("OBJECT_NAME") or obj.get("NAME") or obj.get("OBJECT") or obj.get("SATNAME"))


def get_obj_norad(obj: Dict[str, Any]) -> Optional[int]:
    return safe_int(obj.get("NORAD_CAT_ID") or obj.get("NORAD_CATID") or obj.get("NORAD") or obj.get("CATNR"))


def build_classification_file(
    output_json: Path,
    output_csv: Path,
    ucs_file: Path,
    celestrak_file: Optional[Path],
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    print("Fetching active satellite list from CelesTrak...")
    celestrak_objects, celestrak_source = fetch_celestrak_active(celestrak_file)
    if limit is not None:
        celestrak_objects = celestrak_objects[:limit]

    print(f"Loaded {len(celestrak_objects)} CelesTrak objects from {celestrak_source}")
    print("Loading UCS metadata...")
    by_norad, by_name = load_ucs_lookup(ucs_file)

    objects: List[Dict[str, Any]] = []
    by_norad_output: Dict[str, Dict[str, Any]] = {}
    by_name_output: Dict[str, Dict[str, Any]] = {}
    label_counts: Dict[str, int] = {}
    match_counts: Dict[str, int] = {}

    for obj in celestrak_objects:
        norad_id = get_obj_norad(obj)
        name = get_obj_name(obj)
        ucs_record, match_method = match_ucs_record(obj, by_norad, by_name)
        label, confidence, reason = classify_satellite(name, ucs_record)
        mass_kg, mass_source = estimate_mass_kg(name, ucs_record)

        row = {
            "norad_id": norad_id,
            "name": name,
            "classification_label": label,
            "confidence": confidence,
            "match_method": match_method,
            "operator": ucs_record.get("operator", "") if ucs_record else "",
            "users": ucs_record.get("users", "") if ucs_record else "",
            "purpose": ucs_record.get("purpose", "") if ucs_record else "",
            "mass_kg": mass_kg,
            "mass_source": mass_source,
            "reason": reason,
        }

        objects.append(row)
        label_counts[label] = label_counts.get(label, 0) + 1
        match_counts[match_method] = match_counts.get(match_method, 0) + 1

        lookup_value = {
            "name": name,
            "classification_label": label,
            "confidence": confidence,
            "mass_kg": mass_kg,
            "mass_source": mass_source,
        }
        if norad_id is not None:
            by_norad_output[str(norad_id)] = lookup_value
        if name:
            by_name_output[name] = {
                "norad_id": norad_id,
                "classification_label": label,
                "confidence": confidence,
                "mass_kg": mass_kg,
                "mass_source": mass_source,
            }

    dataset = {
        "generated_at": now_utc(),
        "note": "Classification labels are conservative public-metadata information-handling labels, not official classified/unclassified determinations.",
        "source_urls": {"celestrak_active": celestrak_source, "ucs_database": UCS_XLSX_URL},
        "summary": {"total_satellites": len(objects), "label_counts": label_counts, "match_counts": match_counts},
        "objects": objects,
        "lookup_by_norad_id": by_norad_output,
        "lookup_by_name": by_name_output,
    }

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(dataset, indent=2, ensure_ascii=False), encoding="utf-8")

    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "norad_id", "name", "classification_label", "confidence", "match_method",
                "operator", "users", "purpose", "mass_kg", "mass_source", "reason",
            ],
        )
        writer.writeheader()
        writer.writerows(objects)

    print("Done.")
    print(f"JSON written to: {output_json}")
    print(f"CSV written to:  {output_csv}")
    print("Summary:")
    print(json.dumps(dataset["summary"], indent=2))
    return dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a DryDock satellite ID/name to classification-label lookup file.")
    parser.add_argument("--output-json", type=Path, default=Path("satellite_classification_lookup.json"), help="Output JSON file. Default writes into the current folder.")
    parser.add_argument("--output-csv", type=Path, default=Path("satellite_classification_lookup.csv"), help="Output CSV file. Default writes into the current folder.")
    parser.add_argument("--ucs-file", type=Path, default=Path("data/UCS-Satellite-Database-5-1-2023.xlsx"), help="Local UCS Excel path. If missing, script downloads it.")
    parser.add_argument("--celestrak-file", type=Path, default=None, help="Optional local CelesTrak JSON/CSV/TLE file, useful if the website blocks Python with 403.")
    parser.add_argument("--limit", type=int, default=None, help="Optional test limit, for example --limit 100.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        build_classification_file(args.output_json, args.output_csv, args.ucs_file, args.celestrak_file, args.limit)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
