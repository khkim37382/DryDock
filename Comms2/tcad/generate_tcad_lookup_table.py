"""
Improved TCAD / DEVSIM lookup table generator.

Run this BEFORE the VPython satellite simulation:

    python generate_tcad_lookup_table.py

It writes:
    tcad_lookup_table.json
    output/tcad_generation_log.json

The live satellite simulation should read tcad_lookup_table.json and use it to
continuously degrade every sensor as radiation, temperature, shielding, Sun
exposure, and cumulative dose change over time.

This version adds:
    - visible camera CMOS sensor
    - star tracker CMOS sensor
    - communication radio
    - solar exposure factor for eclipse / Sun geometry
    - trapped belt factor for Van Allen zone severity
    - degradation-rate outputs for continuous integration
    - camera-specific degradation outputs
    - readout/ADC-specific degradation outputs
    - RF-specific degradation outputs
    - digital processor / command decoder outputs

Current status:
    Uses TCAD-inspired proxy equations through simple_device_model.py.
    DEVSIM import is detected and logged, but full DEVSIM solves are not run yet.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, List

from simple_device_model import DeviceInputs, SENSOR_PROFILES, run_device_model

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "tcad_lookup_table.json")
LOG_PATH = os.path.join(BASE_DIR, "output", "tcad_generation_log.json")

try:
    import devsim  # noqa: F401
    DEVSIM_AVAILABLE = True
except Exception:
    DEVSIM_AVAILABLE = False


SENSOR_TYPES = list(SENSOR_PROFILES.keys())

# Grid values are intentionally coarse enough to generate quickly but broad
# enough to cover LEO, Van Allen belts, solar storms, eclipse, and hot/cold ops.
CUMULATIVE_DOSE_LEVELS_MSV = [0, 2, 5, 10, 25, 50, 100, 200, 400, 800]
DOSE_RATE_LEVELS_MSV_PER_DAY = [0.05, 0.35, 1, 2, 8, 20, 50, 100]
PARTICLE_FLUX_LEVELS_PFU = [1, 10, 50, 200, 1000, 5000, 20000]
TEMPERATURE_LEVELS_C = [-40, -20, 0, 25, 50, 75, 100]
SHIELDING_LEVELS_MM_AL = [0.0, 0.5, 2.5, 5.0, 10.0, 15.0]

# Sun exposure: 0.0 means fully eclipsed from direct solar storm/proton source;
# 1.0 means sunlit. Trapped belt and GCR-like terms still remain in the model.
SUN_EXPOSURE_LEVELS = [0.0, 0.25, 0.5, 1.0]

# Region factor approximates radiation-zone severity. The live sim can map:
#   low_earth_orbit -> 0.25
#   inner_van_allen_region -> 1.0
#   outer_van_allen_region -> 0.7
#   high_earth_orbit -> 0.35
TRAPPED_BELT_FACTORS = [0.15, 0.35, 0.70, 1.00]

RADIATION_REGION_HINTS = {
    0.15: "quiet_or_eclipse_low_belt",
    0.35: "low_earth_or_high_orbit",
    0.70: "outer_van_allen_region",
    1.00: "inner_van_allen_region",
}


def build_case_entry(
    sensor_type: str,
    cumulative_dose_msv: float,
    dose_rate_msv_per_day: float,
    particle_flux_pfu: float,
    temperature_c: float,
    shielding_mm_aluminum: float,
    sun_exposure_factor: float,
    trapped_belt_factor: float,
) -> Dict[str, Any]:
    inputs = DeviceInputs(
        sensor_type=sensor_type,
        cumulative_dose_msv=cumulative_dose_msv,
        dose_rate_msv_per_day=dose_rate_msv_per_day,
        particle_flux_pfu=particle_flux_pfu,
        temperature_c=temperature_c,
        shielding_mm_aluminum=shielding_mm_aluminum,
        sun_exposure_factor=sun_exposure_factor,
        trapped_belt_factor=trapped_belt_factor,
    )
    result = run_device_model(inputs, full_devsim_requested=False)

    return {
        "sensor_type": sensor_type,
        "inputs": {
            "cumulative_dose_msv": cumulative_dose_msv,
            "dose_rate_msv_per_day": dose_rate_msv_per_day,
            "particle_flux_pfu": particle_flux_pfu,
            "temperature_c": temperature_c,
            "shielding_mm_aluminum": shielding_mm_aluminum,
            "sun_exposure_factor": sun_exposure_factor,
            "trapped_belt_factor": trapped_belt_factor,
            "radiation_region_hint": RADIATION_REGION_HINTS.get(trapped_belt_factor, "custom"),
        },
        "model_runtime": {
            "devsim_import_available": DEVSIM_AVAILABLE,
            "model_source": "tcad_inspired_device_proxy_model_devsim_ready",
            "full_devsim_solve_used": False,
        },
        "outputs": result,
    }


def estimate_total_cases() -> int:
    return (
        len(SENSOR_TYPES)
        * len(CUMULATIVE_DOSE_LEVELS_MSV)
        * len(DOSE_RATE_LEVELS_MSV_PER_DAY)
        * len(PARTICLE_FLUX_LEVELS_PFU)
        * len(TEMPERATURE_LEVELS_C)
        * len(SHIELDING_LEVELS_MM_AL)
        * len(SUN_EXPOSURE_LEVELS)
        * len(TRAPPED_BELT_FACTORS)
    )


def generate_entries() -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    total_cases = estimate_total_cases()
    case_index = 0

    for sensor_type in SENSOR_TYPES:
        for cumulative_dose_msv in CUMULATIVE_DOSE_LEVELS_MSV:
            for dose_rate_msv_per_day in DOSE_RATE_LEVELS_MSV_PER_DAY:
                for particle_flux_pfu in PARTICLE_FLUX_LEVELS_PFU:
                    for temperature_c in TEMPERATURE_LEVELS_C:
                        for shielding_mm_aluminum in SHIELDING_LEVELS_MM_AL:
                            for sun_exposure_factor in SUN_EXPOSURE_LEVELS:
                                for trapped_belt_factor in TRAPPED_BELT_FACTORS:
                                    case_index += 1
                                    entries.append(
                                        build_case_entry(
                                            sensor_type=sensor_type,
                                            cumulative_dose_msv=cumulative_dose_msv,
                                            dose_rate_msv_per_day=dose_rate_msv_per_day,
                                            particle_flux_pfu=particle_flux_pfu,
                                            temperature_c=temperature_c,
                                            shielding_mm_aluminum=shielding_mm_aluminum,
                                            sun_exposure_factor=sun_exposure_factor,
                                            trapped_belt_factor=trapped_belt_factor,
                                        )
                                    )

                                    if case_index % 10000 == 0:
                                        print(f"  generated {case_index}/{total_cases} cases")

    return entries


def build_lookup_table(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    generated_utc_iso = datetime.now(timezone.utc).isoformat()
    return {
        "schema": "satellite_simulation.tcad_lookup_table.v2",
        "generated_utc_iso": generated_utc_iso,
        "model_source": "tcad_inspired_device_proxy_model_devsim_ready",
        "full_devsim_solve_used": False,
        "devsim_import_available": DEVSIM_AVAILABLE,
        "description": (
            "Offline lookup table for converting satellite radiation, particle flux, "
            "temperature, shielding, Sun exposure/eclipse, trapped radiation-belt severity, "
            "and sensor type into semiconductor degradation, per-sensor degradation rates, "
            "and confidence values. Live simulation should integrate the returned rates over time."
        ),
        "continuous_degradation_notes": {
            "intended_live_sim_formula": "sensor_state[t+dt] = sensor_state[t] + lookup_rate_outputs * dt_days",
            "important_fields": [
                "outputs.sensor_effects.health_loss_per_day",
                "outputs.sensor_effects.noise_growth_per_day",
                "outputs.sensor_effects.bias_drift_per_day",
                "outputs.sensor_effects.seu_probability_per_day",
                "outputs.sensor_effects.sensor_confidence",
            ],
            "eclipse_model_note": (
                "sun_exposure_factor reduces direct solar particle/storm contribution, "
                "but trapped-belt and residual exposure remain through trapped_belt_factor."
            ),
        },
        "input_grid": {
            "sensor_types": SENSOR_TYPES,
            "cumulative_dose_levels_msv": CUMULATIVE_DOSE_LEVELS_MSV,
            "dose_rate_levels_msv_per_day": DOSE_RATE_LEVELS_MSV_PER_DAY,
            "particle_flux_levels_pfu": PARTICLE_FLUX_LEVELS_PFU,
            "temperature_levels_c": TEMPERATURE_LEVELS_C,
            "shielding_levels_mm_al": SHIELDING_LEVELS_MM_AL,
            "sun_exposure_levels": SUN_EXPOSURE_LEVELS,
            "trapped_belt_factors": TRAPPED_BELT_FACTORS,
            "radiation_region_hints": RADIATION_REGION_HINTS,
        },
        "sensor_profile_summary": SENSOR_PROFILES,
        "entry_count": len(entries),
        "entries": entries,
    }


def write_json(path: str, data: Dict[str, Any]) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(temp_path, path)


def main() -> None:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    total_cases = estimate_total_cases()
    print("Generating improved TCAD lookup table...")
    print(f"DEVSIM import available: {DEVSIM_AVAILABLE}")
    print(f"Full DEVSIM solve used: False")
    print(f"Sensor types: {len(SENSOR_TYPES)}")
    print(f"Total cases: {total_cases}")

    entries = generate_entries()
    table = build_lookup_table(entries)
    write_json(OUTPUT_PATH, table)

    log = {
        "generated_utc_iso": table["generated_utc_iso"],
        "output_path": OUTPUT_PATH,
        "entry_count": len(entries),
        "devsim_import_available": DEVSIM_AVAILABLE,
        "full_devsim_solve_used": False,
        "schema": table["schema"],
        "sensor_types": SENSOR_TYPES,
    }
    write_json(LOG_PATH, log)

    print(f"Done. Wrote lookup table to: {OUTPUT_PATH}")
    print(f"Wrote log to: {LOG_PATH}")


if __name__ == "__main__":
    main()
