import json
import math
import os
from datetime import datetime, timezone

# ============================================================
# TCAD / DEVSIM lookup table generator
# ============================================================
# This script runs BEFORE the VPython satellite simulation.
#
# Purpose:
#   Generate tcad_lookup_table.json.
#
# Live satellite sim will later read this table and use it to convert:
#   radiation + temperature + shielding + sensor type
# into:
#   per-sensor degradation + confidence values.
#
# This file has a fallback "TCAD-inspired equation" mode so it still works
# even if DEVSIM is not installed or the DEVSIM model is not ready yet.
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(BASE_DIR, "tcad_lookup_table.json")
LOG_PATH = os.path.join(BASE_DIR, "output", "tcad_generation_log.json")

# Try importing DEVSIM.
# For now, this script uses a TCAD-inspired fallback model.
# Later you can replace run_devsim_or_surrogate_case(...) with real DEVSIM calls.
try:
    import devsim  # noqa: F401
    DEVSIM_AVAILABLE = True
except Exception:
    DEVSIM_AVAILABLE = False


SENSOR_TYPES = [
    "voltage_sensor_adc",
    "thermal_sensor_readout",
    "rf_frontend",
    "solar_panel_current_sensor",
    "command_decoder",
    "onboard_processor",
]

CUMULATIVE_DOSE_LEVELS_MSV = [0, 5, 10, 25, 50, 100, 200, 400]
DOSE_RATE_LEVELS_MSV_PER_DAY = [0.35, 2, 8, 20, 50]
PARTICLE_FLUX_LEVELS_PFU = [1, 50, 200, 1000, 5000]
TEMPERATURE_LEVELS_C = [-20, 0, 25, 50, 75, 100]
SHIELDING_LEVELS_MM_AL = [0.5, 2.5, 5.0, 10.0]


SENSOR_PROFILES = {
    "voltage_sensor_adc": {
        "dose_sensitivity": 0.0018,
        "rate_sensitivity": 0.0010,
        "flux_sensitivity": 0.030,
        "thermal_sensitivity": 0.006,
        "noise_base": 1.00,
        "bit_error_scale": 1.00,
    },
    "thermal_sensor_readout": {
        "dose_sensitivity": 0.0012,
        "rate_sensitivity": 0.0007,
        "flux_sensitivity": 0.018,
        "thermal_sensitivity": 0.009,
        "noise_base": 1.00,
        "bit_error_scale": 0.55,
    },
    "rf_frontend": {
        "dose_sensitivity": 0.0022,
        "rate_sensitivity": 0.0013,
        "flux_sensitivity": 0.040,
        "thermal_sensitivity": 0.007,
        "noise_base": 1.05,
        "bit_error_scale": 0.75,
    },
    "solar_panel_current_sensor": {
        "dose_sensitivity": 0.0016,
        "rate_sensitivity": 0.0008,
        "flux_sensitivity": 0.020,
        "thermal_sensitivity": 0.004,
        "noise_base": 1.00,
        "bit_error_scale": 0.45,
    },
    "command_decoder": {
        "dose_sensitivity": 0.0014,
        "rate_sensitivity": 0.0010,
        "flux_sensitivity": 0.055,
        "thermal_sensitivity": 0.006,
        "noise_base": 1.00,
        "bit_error_scale": 1.35,
    },
    "onboard_processor": {
        "dose_sensitivity": 0.0017,
        "rate_sensitivity": 0.0012,
        "flux_sensitivity": 0.060,
        "thermal_sensitivity": 0.007,
        "noise_base": 1.00,
        "bit_error_scale": 1.50,
    },
}


def clamp(value, low, high):
    return max(low, min(high, value))


def shielding_factor(shielding_mm_al):
    # Same idea as your main sim: more aluminum reduces effective radiation.
    halving_thickness_mm = 7.0
    return 0.5 ** (shielding_mm_al / halving_thickness_mm)


def classify_latchup_risk(command_bit_error_probability, temperature_c, particle_flux_pfu):
    if command_bit_error_probability > 0.08 or temperature_c >= 95 or particle_flux_pfu >= 5000:
        return "critical"
    if command_bit_error_probability > 0.035 or temperature_c >= 75 or particle_flux_pfu >= 1000:
        return "high"
    if command_bit_error_probability > 0.012 or temperature_c >= 55 or particle_flux_pfu >= 200:
        return "medium"
    return "low"


def run_devsim_or_surrogate_case(
    sensor_type,
    cumulative_dose_msv,
    dose_rate_msv_per_day,
    particle_flux_pfu,
    temperature_c,
    shielding_mm_aluminum,
):
    """
    This is where DEVSIM would eventually come in.

    Right now this function generates TCAD-inspired semiconductor effects.
    Later, you can replace the internal equations with:
      - create/load DEVSIM device mesh
      - apply temperature
      - apply trapped charge/interface trap terms
      - solve device
      - extract leakage/current/threshold values

    The output format should stay the same so the main satellite sim does not break.
    """
    profile = SENSOR_PROFILES[sensor_type]
    shield = shielding_factor(shielding_mm_aluminum)

    effective_dose = cumulative_dose_msv * shield
    effective_dose_rate = dose_rate_msv_per_day * shield
    effective_flux = particle_flux_pfu * shield

    over_temp = max(0.0, temperature_c - 25.0)

    # TCAD-inspired semiconductor outputs.
    threshold_voltage_shift_mv = (
        0.42 * effective_dose
        + 0.18 * effective_dose_rate
        + 0.006 * math.sqrt(max(effective_flux, 0.0))
    )

    leakage_current_multiplier = 1.0 + (
        profile["dose_sensitivity"] * effective_dose
        + 0.012 * math.exp(over_temp / 55.0)
        + 0.00012 * math.sqrt(max(effective_flux, 0.0))
    )

    noise_multiplier = profile["noise_base"] + (
        0.0018 * threshold_voltage_shift_mv
        + 0.11 * (leakage_current_multiplier - 1.0)
        + profile["thermal_sensitivity"] * max(0.0, temperature_c - 40.0)
    )

    adc_bit_error_probability = clamp(
        profile["bit_error_scale"] * (
            0.00002 * effective_dose
            + 0.00005 * effective_dose_rate
            + 0.000004 * math.sqrt(max(effective_flux, 0.0))
            + 0.0007 * max(0.0, temperature_c - 60.0)
        ),
        0.0,
        0.35,
    )

    memory_bit_flip_probability = clamp(
        profile["bit_error_scale"] * (
            0.00001 * effective_dose
            + 0.000008 * effective_flux
            + 0.0003 * max(0.0, temperature_c - 70.0)
        ),
        0.0,
        0.65,
    )

    rf_noise_floor_shift_db = 0.0
    gain_loss_db = 0.0
    if sensor_type == "rf_frontend":
        rf_noise_floor_shift_db = clamp(10.0 * math.log10(max(noise_multiplier, 1.0)), 0.0, 12.0)
        gain_loss_db = clamp(0.018 * threshold_voltage_shift_mv + 0.55 * (leakage_current_multiplier - 1.0), 0.0, 8.0)

    sensor_gain_drift_percent = clamp(
        -0.03 * threshold_voltage_shift_mv - 1.7 * (leakage_current_multiplier - 1.0),
        -35.0,
        0.0,
    )

    command_bit_error_probability = adc_bit_error_probability
    if sensor_type in ["command_decoder", "onboard_processor"]:
        command_bit_error_probability = clamp(memory_bit_flip_probability * 0.65 + adc_bit_error_probability * 0.35, 0.0, 0.75)

    risk_penalty = (
        profile["dose_sensitivity"] * effective_dose
        + profile["rate_sensitivity"] * effective_dose_rate
        + profile["flux_sensitivity"] * math.log10(1.0 + effective_flux)
        + profile["thermal_sensitivity"] * max(0.0, temperature_c - 40.0)
        + 1.2 * adc_bit_error_probability
    )

    sensor_confidence = clamp(1.0 - risk_penalty, 0.05, 1.0)

    if sensor_confidence >= 0.85:
        trust_level = "normal"
    elif sensor_confidence >= 0.65:
        trust_level = "slightly_downweighted"
    elif sensor_confidence >= 0.40:
        trust_level = "heavily_downweighted"
    else:
        trust_level = "quarantine_sensor_data"

    latchup_risk = classify_latchup_risk(command_bit_error_probability, temperature_c, effective_flux)

    return {
        "semiconductor_effects": {
            "threshold_voltage_shift_mv": round(threshold_voltage_shift_mv, 5),
            "leakage_current_multiplier": round(leakage_current_multiplier, 6),
            "noise_multiplier": round(noise_multiplier, 6),
            "sensor_gain_drift_percent": round(sensor_gain_drift_percent, 5),
            "adc_bit_error_probability": round(adc_bit_error_probability, 8),
            "memory_bit_flip_probability": round(memory_bit_flip_probability, 8),
            "rf_noise_floor_shift_db": round(rf_noise_floor_shift_db, 5),
            "gain_loss_db": round(gain_loss_db, 5),
            "command_bit_error_probability": round(command_bit_error_probability, 8),
            "latchup_risk": latchup_risk,
        },
        "sensor_effects": {
            "sensor_confidence": round(sensor_confidence, 6),
            "trust_level": trust_level,
            "requires_acknowledgement": bool(sensor_type in ["command_decoder", "onboard_processor"] and sensor_confidence < 0.78),
            "safe_to_use_for_autonomous_control": bool(sensor_confidence >= 0.65 and latchup_risk not in ["high", "critical"]),
        },
    }


def main():
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    entries = []
    total_cases = (
        len(SENSOR_TYPES)
        * len(CUMULATIVE_DOSE_LEVELS_MSV)
        * len(DOSE_RATE_LEVELS_MSV_PER_DAY)
        * len(PARTICLE_FLUX_LEVELS_PFU)
        * len(TEMPERATURE_LEVELS_C)
        * len(SHIELDING_LEVELS_MM_AL)
    )

    print("Generating TCAD lookup table...")
    print(f"DEVSIM import available: {DEVSIM_AVAILABLE}")
    print(f"Total cases: {total_cases}")

    case_index = 0
    for sensor_type in SENSOR_TYPES:
        for cumulative_dose_msv in CUMULATIVE_DOSE_LEVELS_MSV:
            for dose_rate_msv_per_day in DOSE_RATE_LEVELS_MSV_PER_DAY:
                for particle_flux_pfu in PARTICLE_FLUX_LEVELS_PFU:
                    for temperature_c in TEMPERATURE_LEVELS_C:
                        for shielding_mm_aluminum in SHIELDING_LEVELS_MM_AL:
                            case_index += 1

                            result = run_devsim_or_surrogate_case(
                                sensor_type=sensor_type,
                                cumulative_dose_msv=cumulative_dose_msv,
                                dose_rate_msv_per_day=dose_rate_msv_per_day,
                                particle_flux_pfu=particle_flux_pfu,
                                temperature_c=temperature_c,
                                shielding_mm_aluminum=shielding_mm_aluminum,
                            )

                            entries.append({
                                "sensor_type": sensor_type,
                                "inputs": {
                                    "cumulative_dose_msv": cumulative_dose_msv,
                                    "dose_rate_msv_per_day": dose_rate_msv_per_day,
                                    "particle_flux_pfu": particle_flux_pfu,
                                    "temperature_c": temperature_c,
                                    "shielding_mm_aluminum": shielding_mm_aluminum,
                                },
                                "model_runtime": {
                                    "devsim_import_available": DEVSIM_AVAILABLE,
                                    "model_source": "tcad_inspired_parametric_model_with_devsim_ready_interface",
                                    "full_devsim_solve_used": False,
                                },
                                "outputs": result,
                            })

                            if case_index % 1000 == 0:
                                print(f"  generated {case_index}/{total_cases} cases")

    table = {
        "schema": "satellite_simulation.tcad_lookup_table.v1",
        "generated_utc_iso": datetime.now(timezone.utc).isoformat(),
        "model_source": "tcad_inspired_parametric_model_with_devsim_ready_interface",
        "full_devsim_solve_used": False,
        "devsim_import_available": DEVSIM_AVAILABLE,
        "description": (
            "Offline lookup table for converting satellite radiation, particle flux, "
            "temperature, shielding, and sensor type into semiconductor degradation "
            "and per-sensor confidence values. The interface is designed so real "
            "DEVSIM solves can replace the parametric equations later."
        ),
        "input_grid": {
            "sensor_types": SENSOR_TYPES,
            "cumulative_dose_levels_msv": CUMULATIVE_DOSE_LEVELS_MSV,
            "dose_rate_levels_msv_per_day": DOSE_RATE_LEVELS_MSV_PER_DAY,
            "particle_flux_levels_pfu": PARTICLE_FLUX_LEVELS_PFU,
            "temperature_levels_c": TEMPERATURE_LEVELS_C,
            "shielding_levels_mm_al": SHIELDING_LEVELS_MM_AL,
        },
        "entries": entries,
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(table, f, indent=2)
        f.write("\n")

    log = {
        "generated_utc_iso": table["generated_utc_iso"],
        "output_path": OUTPUT_PATH,
        "entry_count": len(entries),
        "devsim_import_available": DEVSIM_AVAILABLE,
        "full_devsim_solve_used": False,
    }

    with open(LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)
        f.write("\n")

    print(f"Done. Wrote lookup table to: {OUTPUT_PATH}")
    print(f"Wrote log to: {LOG_PATH}")


if __name__ == "__main__":
    main()