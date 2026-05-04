#!/usr/bin/env python3
"""
estimate_debris_masses.py

Adds realistic estimated masses to a CelesTrak debris JSON dataset.

Input:
    celestrak_debris_dataset.json

Output:
    celestrak_debris_dataset_with_estimated_masses.json

Important:
- These are NOT measured masses.
- Public CelesTrak debris GP data usually does not include mass.
- This script estimates mass using orbital-drag proxy fields already present
  in the JSON, especially BSTAR, mean-motion-dot, eccentricity, and orbit height.
- The output labels every filled value as estimated with a confidence field.

Run:
    python3 estimate_debris_masses.py

Optional:
    python3 estimate_debris_masses.py --input celestrak_debris_dataset.json --output debris_with_mass.json
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_INPUT = Path("celestrak_debris_dataset.json")
DEFAULT_OUTPUT = Path("celestrak_debris_dataset_with_estimated_masses.json")

EARTH_RADIUS_KM = 6378.137


# ----------------------------------------------------------------------
# Family-level rough priors
# ----------------------------------------------------------------------
# These are not exact fragment masses. They are realistic engineering priors
# for tracked LEO debris fragments from these breakup/collision families.
#
# The ranges are intentionally broad because CelesTrak gives orbit data,
# not object dimensions or measured mass.
#
# min/max are clamp bounds.
# median is the center of the model before drag adjustment.
# density factor slightly adjusts family behavior:
#   higher -> heavier fragments on average
#   lower  -> lighter fragments on average
# ----------------------------------------------------------------------

DEBRIS_FAMILY_PRIORS = {
    "COSMOS 2251 DEB": {
        "median_mass_kg": 0.8,
        "min_mass_kg": 0.03,
        "max_mass_kg": 35.0,
        "density_factor": 1.05,
        "notes": "Estimated mass prior for tracked Cosmos 2251 collision debris.",
    },
    "IRIDIUM 33 DEB": {
        "median_mass_kg": 0.7,
        "min_mass_kg": 0.03,
        "max_mass_kg": 30.0,
        "density_factor": 0.95,
        "notes": "Estimated mass prior for tracked Iridium 33 collision debris.",
    },
    "FENGYUN 1C DEB": {
        "median_mass_kg": 0.5,
        "min_mass_kg": 0.02,
        "max_mass_kg": 25.0,
        "density_factor": 0.85,
        "notes": "Estimated mass prior for tracked Fengyun 1C fragmentation debris.",
    },
    "DEFAULT": {
        "median_mass_kg": 0.6,
        "min_mass_kg": 0.02,
        "max_mass_kg": 30.0,
        "density_factor": 1.0,
        "notes": "Generic tracked debris mass prior.",
    },
}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except (TypeError, ValueError):
        return None


def get_orbit_elements(obj: Dict[str, Any]) -> Dict[str, Any]:
    return obj.get("orbit_elements") or {}


def get_raw_gp(obj: Dict[str, Any]) -> Dict[str, Any]:
    return obj.get("raw_celestrak_gp") or {}


def get_field(obj: Dict[str, Any], field: str) -> Optional[float]:
    """
    Looks for a numeric field in orbit_elements first, then raw_celestrak_gp.
    """

    orbit = get_orbit_elements(obj)
    raw = get_raw_gp(obj)

    value = safe_float(orbit.get(field))
    if value is not None:
        return value

    # Convert our snake_case names to common CelesTrak uppercase names.
    raw_aliases = {
        "mean_motion_rev_per_day": "MEAN_MOTION",
        "eccentricity": "ECCENTRICITY",
        "inclination_deg": "INCLINATION",
        "raan_deg": "RA_OF_ASC_NODE",
        "argument_of_perigee_deg": "ARG_OF_PERICENTER",
        "mean_anomaly_deg": "MEAN_ANOMALY",
        "bstar": "BSTAR",
        "mean_motion_dot": "MEAN_MOTION_DOT",
        "estimated_semimajor_axis_km": "estimated_semimajor_axis_km",
    }

    raw_key = raw_aliases.get(field, field)
    return safe_float(raw.get(raw_key))


def get_family_name(obj: Dict[str, Any]) -> str:
    value = (
        obj.get("celestrak_name_query")
        or obj.get("name")
        or get_raw_gp(obj).get("OBJECT_NAME")
        or "DEFAULT"
    )
    value = str(value).upper().strip()

    if "COSMOS 2251" in value:
        return "COSMOS 2251 DEB"
    if "IRIDIUM 33" in value:
        return "IRIDIUM 33 DEB"
    if "FENGYUN 1C" in value:
        return "FENGYUN 1C DEB"

    return "DEFAULT"


def compute_altitude_km(obj: Dict[str, Any]) -> Optional[float]:
    a_km = get_field(obj, "estimated_semimajor_axis_km")
    if a_km is None:
        return None
    return a_km - EARTH_RADIUS_KM


def robust_percentile_rank(value: Optional[float], values: List[float]) -> Optional[float]:
    """
    Returns value's rank in [0, 1].
    0 means low, 1 means high.
    """

    if value is None or not values:
        return None

    sorted_values = sorted(values)
    count = len(sorted_values)

    below_or_equal = 0
    for x in sorted_values:
        if x <= value:
            below_or_equal += 1
        else:
            break

    return below_or_equal / count


def collect_distribution_values(objects: List[Dict[str, Any]]) -> Dict[str, List[float]]:
    bstars = []
    mm_dots = []
    altitudes = []
    eccentricities = []

    for obj in objects:
        bstar = get_field(obj, "bstar")
        if bstar is not None and bstar > 0:
            bstars.append(abs(bstar))

        mm_dot = get_field(obj, "mean_motion_dot")
        if mm_dot is not None and mm_dot > 0:
            mm_dots.append(abs(mm_dot))

        altitude = compute_altitude_km(obj)
        if altitude is not None and altitude > 0:
            altitudes.append(altitude)

        ecc = get_field(obj, "eccentricity")
        if ecc is not None and ecc >= 0:
            eccentricities.append(ecc)

    return {
        "bstar": bstars,
        "mean_motion_dot": mm_dots,
        "altitude_km": altitudes,
        "eccentricity": eccentricities,
    }


def estimate_area_to_mass_m2_per_kg(
    obj: Dict[str, Any],
    distributions: Dict[str, List[float]],
) -> float:
    """
    Estimate area-to-mass ratio.

    This is a proxy model:
    - Higher BSTAR usually means stronger drag response.
    - Higher positive mean-motion-dot usually means faster orbital decay.
    - Lower altitude increases drag, so we compensate slightly.
    - High eccentricity can make drag behavior less clean, so we damp confidence later.

    Typical tracked debris can span a very broad area-to-mass range.
    For simulation purposes, this function produces values around:
        0.005 to 0.20 m^2/kg

    Higher A/M means lighter or more sheet-like.
    Lower A/M means denser or more compact.
    """

    bstar = get_field(obj, "bstar")
    mm_dot = get_field(obj, "mean_motion_dot")
    altitude = compute_altitude_km(obj)

    bstar_rank = robust_percentile_rank(abs(bstar) if bstar is not None else None, distributions["bstar"])
    mm_dot_rank = robust_percentile_rank(abs(mm_dot) if mm_dot is not None else None, distributions["mean_motion_dot"])
    altitude_rank = robust_percentile_rank(altitude, distributions["altitude_km"])

    # Default midpoint if data is missing.
    if bstar_rank is None:
        bstar_rank = 0.5
    if mm_dot_rank is None:
        mm_dot_rank = 0.5
    if altitude_rank is None:
        altitude_rank = 0.5

    # Drag proxy:
    # More BSTAR and more mean-motion-dot imply higher area-to-mass.
    # Higher altitude means drag should be weaker, so if drag is still high there,
    # A/M may be higher. Give altitude a smaller weight.
    drag_score = (
        0.60 * bstar_rank
        + 0.30 * mm_dot_rank
        + 0.10 * altitude_rank
    )

    # Convert score to a broad physical-ish A/M range.
    # Exponential mapping gives more realistic spread than linear.
    min_am = 0.004
    max_am = 0.200
    am = min_am * ((max_am / min_am) ** drag_score)

    return clamp(am, min_am, max_am)


def estimate_cross_section_area_m2(
    obj: Dict[str, Any],
    family: str,
    distributions: Dict[str, List[float]],
) -> float:
    """
    Estimate projected cross-sectional area.

    Public GP data does not include radar cross-section, so this is a synthetic
    but realistic simulation estimate. It uses:
    - family prior
    - eccentricity
    - altitude
    - a deterministic NORAD-based variation

    Range is roughly 0.002 to 0.50 m^2.
    """

    norad = obj.get("norad_cat_id")
    ecc = get_field(obj, "eccentricity")
    altitude = compute_altitude_km(obj)

    ecc_rank = robust_percentile_rank(ecc, distributions["eccentricity"])
    altitude_rank = robust_percentile_rank(altitude, distributions["altitude_km"])

    if ecc_rank is None:
        ecc_rank = 0.5
    if altitude_rank is None:
        altitude_rank = 0.5

    # Deterministic pseudo-random variation from NORAD ID.
    # This keeps results stable every run.
    if norad is None:
        variation = 0.5
    else:
        variation = ((int(norad) * 1103515245 + 12345) % 10000) / 10000.0

    family_area_factor = {
        "COSMOS 2251 DEB": 1.10,
        "IRIDIUM 33 DEB": 1.00,
        "FENGYUN 1C DEB": 0.85,
        "DEFAULT": 1.00,
    }.get(family, 1.0)

    # Most fragments are small; a few are larger.
    # Exponential area range.
    min_area = 0.003
    max_area = 0.350

    size_score = (
        0.45 * variation
        + 0.25 * ecc_rank
        + 0.15 * altitude_rank
        + 0.15 * 0.5
    )

    area = min_area * ((max_area / min_area) ** size_score)
    area *= family_area_factor

    return clamp(area, 0.002, 0.50)


def estimate_mass_for_object(
    obj: Dict[str, Any],
    distributions: Dict[str, List[float]],
) -> Dict[str, Any]:
    """
    Estimate mass using:

        mass = projected_area / area_to_mass

    Then blend with a family prior so the result does not go insane due to
    noisy BSTAR or mean-motion-dot values.

    Returns metadata for JSON insertion.
    """

    family = get_family_name(obj)
    prior = DEBRIS_FAMILY_PRIORS.get(family, DEBRIS_FAMILY_PRIORS["DEFAULT"])

    am = estimate_area_to_mass_m2_per_kg(obj, distributions)
    area = estimate_cross_section_area_m2(obj, family, distributions)

    raw_mass = area / am

    median_mass = prior["median_mass_kg"] * prior["density_factor"]

    # Blend raw drag-derived estimate with family prior.
    # 65% data proxy, 35% prior.
    blended_mass = (0.65 * raw_mass) + (0.35 * median_mass)

    mass_kg = clamp(
        blended_mass,
        prior["min_mass_kg"],
        prior["max_mass_kg"],
    )

    confidence = estimate_confidence(obj)

    return {
        "mass_kg": round(mass_kg, 4),
        "mass_source": "estimated_from_orbital_drag_proxy",
        "mass_confidence": confidence,
        "mass_estimation_method": (
            "Estimated from CelesTrak GP fields using BSTAR, mean-motion-dot, "
            "altitude, eccentricity, debris-family priors, and a deterministic "
            "fragment-area proxy. This is not a measured mass."
        ),
        "estimated_area_to_mass_m2_per_kg": round(am, 6),
        "estimated_cross_section_area_m2": round(area, 6),
        "mass_estimation_family_prior": family,
    }


def estimate_confidence(obj: Dict[str, Any]) -> str:
    """
    Debris mass from public orbital data is inherently uncertain.
    Confidence is mostly low, but we distinguish cases with better drag fields.
    """

    bstar = get_field(obj, "bstar")
    mm_dot = get_field(obj, "mean_motion_dot")
    a_km = get_field(obj, "estimated_semimajor_axis_km")

    score = 0

    if bstar is not None and bstar > 0:
        score += 1
    if mm_dot is not None and mm_dot > 0:
        score += 1
    if a_km is not None and a_km > EARTH_RADIUS_KM:
        score += 1

    # Even with all fields, this is still not high confidence.
    if score >= 3:
        return "medium_low"
    if score == 2:
        return "low"
    return "very_low"


def add_mass_estimates(dataset: Dict[str, Any]) -> Dict[str, Any]:
    debris = dataset.get("debris")

    if not isinstance(debris, list):
        raise ValueError("Input JSON does not contain a top-level 'debris' list.")

    distributions = collect_distribution_values(debris)

    updated_count = 0

    for obj in debris:
        if not isinstance(obj, dict):
            continue

        # If an object already has a measured/non-null mass and the user does not
        # want to overwrite it, skip it. In your current dataset these are null.
        existing_mass = safe_float(obj.get("mass_kg"))

        if existing_mass is not None and existing_mass > 0:
            obj.setdefault("mass_source", "existing")
            continue

        estimate = estimate_mass_for_object(obj, distributions)

        obj["mass_kg"] = estimate["mass_kg"]
        obj["mass_source"] = estimate["mass_source"]
        obj["mass_confidence"] = estimate["mass_confidence"]
        obj["mass_estimation_method"] = estimate["mass_estimation_method"]
        obj["estimated_area_to_mass_m2_per_kg"] = estimate["estimated_area_to_mass_m2_per_kg"]
        obj["estimated_cross_section_area_m2"] = estimate["estimated_cross_section_area_m2"]
        obj["mass_estimation_family_prior"] = estimate["mass_estimation_family_prior"]

        updated_count += 1

    dataset.setdefault("notes", [])
    dataset["notes"].append(
        "Debris mass_kg values were estimated by estimate_debris_masses.py. "
        "They are not measured masses."
    )

    dataset["mass_estimation_summary"] = {
        "method": "orbital_drag_proxy_with_family_priors",
        "updated_debris_objects": updated_count,
        "warning": (
            "Estimated debris masses are suitable for visualization, collision-energy "
            "scaling, and simulation heuristics, but should not be treated as measured values."
        ),
        "fields_used": [
            "BSTAR",
            "MEAN_MOTION_DOT",
            "estimated_semimajor_axis_km",
            "eccentricity",
            "norad_cat_id",
            "debris family name",
        ],
        "output_fields_added": [
            "mass_kg",
            "mass_source",
            "mass_confidence",
            "mass_estimation_method",
            "estimated_area_to_mass_m2_per_kg",
            "estimated_cross_section_area_m2",
            "mass_estimation_family_prior",
        ],
    }

    dataset.setdefault("counts", {})
    dataset["counts"]["debris_with_estimated_mass"] = updated_count

    return dataset


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill debris JSON mass_kg fields with realistic estimated values."
    )

    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Input debris JSON path.",
    )

    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help="Output debris JSON path.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    dataset = json.loads(input_path.read_text())

    updated = add_mass_estimates(dataset)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json.dumps(updated, indent=2, default=str) + "\n")
    temp_path.replace(output_path)

    debris = updated.get("debris", [])
    masses = [
        safe_float(obj.get("mass_kg"))
        for obj in debris
        if isinstance(obj, dict) and safe_float(obj.get("mass_kg")) is not None
    ]

    print("Mass-estimated debris dataset written:", output_path)
    print(json.dumps(updated.get("counts", {}), indent=2))

    if masses:
        print("Mass estimate stats:")
        print(f"  objects with mass: {len(masses)}")
        print(f"  min kg: {min(masses):.4f}")
        print(f"  median kg: {statistics.median(masses):.4f}")
        print(f"  max kg: {max(masses):.4f}")
        print(f"  mean kg: {statistics.mean(masses):.4f}")

    print()
    print("Reminder: these are estimated masses, not measured debris masses.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())