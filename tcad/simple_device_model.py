"""
Improved TCAD / DEVSIM-ready device model helpers.

This file is imported by generate_tcad_lookup_table.py.

Design goal:
    Keep the live VPython satellite simulation fast. TCAD/DEVSIM runs offline
    before the simulation and generates tcad_lookup_table.json. The live sim
    only reads/interpolates that table and continuously integrates degradation.

Current status:
    This file provides deterministic TCAD-inspired device proxy models. It is
    structured so real DEVSIM solves can be added later without changing the
    output schema expected by the lookup-table generator.

How to upgrade to real DEVSIM later:
    Replace the internals of run_device_model(...) or the individual
    run_*_proxy_model(...) functions with DEVSIM mesh/device solves. Keep the
    returned dictionary fields the same.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def safe_sqrt(value: float) -> float:
    return math.sqrt(max(0.0, value))


def safe_log10(value: float) -> float:
    return math.log10(max(1.0e-12, value))


@dataclass(frozen=True)
class DeviceInputs:
    sensor_type: str
    cumulative_dose_msv: float
    dose_rate_msv_per_day: float
    particle_flux_pfu: float
    temperature_c: float
    shielding_mm_aluminum: float
    sun_exposure_factor: float = 1.0
    trapped_belt_factor: float = 1.0


SENSOR_DEVICE_FAMILY: Dict[str, str] = {
    "visible_camera_cmos": "cmos_imager",
    "star_tracker_cmos": "cmos_imager",
    "voltage_sensor_adc": "adc_readout",
    "thermal_sensor_readout": "adc_readout",
    "solar_panel_current_sensor": "adc_readout",
    "rf_frontend": "rf_mosfet",
    "communication_radio": "rf_mosfet",
    "command_decoder": "digital_cmos",
    "onboard_processor": "digital_cmos",
}


# Sensitivity values are normalized coefficients for the proxy model. They are
# deliberately conservative: enough variation for routing/sensor-fusion demos,
# not claiming mission-grade qualification data.
SENSOR_PROFILES: Dict[str, Dict[str, float]] = {
    "visible_camera_cmos": {
        "dose_sensitivity": 0.0032,
        "rate_sensitivity": 0.0014,
        "flux_sensitivity": 0.052,
        "thermal_sensitivity": 0.010,
        "noise_base": 1.06,
        "bit_error_scale": 0.85,
        "dark_current_sensitivity": 0.020,
        "hot_pixel_sensitivity": 0.000090,
        "dead_pixel_sensitivity": 0.000026,
        "confidence_floor": 0.22,
    },
    "star_tracker_cmos": {
        "dose_sensitivity": 0.0038,
        "rate_sensitivity": 0.0017,
        "flux_sensitivity": 0.063,
        "thermal_sensitivity": 0.012,
        "noise_base": 1.09,
        "bit_error_scale": 1.05,
        "dark_current_sensitivity": 0.024,
        "hot_pixel_sensitivity": 0.000120,
        "dead_pixel_sensitivity": 0.000032,
        "confidence_floor": 0.18,
    },
    "voltage_sensor_adc": {
        "dose_sensitivity": 0.0018,
        "rate_sensitivity": 0.0010,
        "flux_sensitivity": 0.030,
        "thermal_sensitivity": 0.006,
        "noise_base": 1.00,
        "bit_error_scale": 1.00,
        "bias_sensitivity": 0.006,
        "confidence_floor": 0.35,
    },
    "thermal_sensor_readout": {
        "dose_sensitivity": 0.0012,
        "rate_sensitivity": 0.0007,
        "flux_sensitivity": 0.018,
        "thermal_sensitivity": 0.009,
        "noise_base": 1.00,
        "bit_error_scale": 0.55,
        "bias_sensitivity": 0.009,
        "confidence_floor": 0.40,
    },
    "solar_panel_current_sensor": {
        "dose_sensitivity": 0.0016,
        "rate_sensitivity": 0.0008,
        "flux_sensitivity": 0.020,
        "thermal_sensitivity": 0.004,
        "noise_base": 1.00,
        "bit_error_scale": 0.45,
        "bias_sensitivity": 0.005,
        "confidence_floor": 0.38,
    },
    "rf_frontend": {
        "dose_sensitivity": 0.0024,
        "rate_sensitivity": 0.0013,
        "flux_sensitivity": 0.043,
        "thermal_sensitivity": 0.007,
        "noise_base": 1.05,
        "bit_error_scale": 0.75,
        "gain_sensitivity": 0.018,
        "confidence_floor": 0.28,
    },
    "communication_radio": {
        "dose_sensitivity": 0.0026,
        "rate_sensitivity": 0.0014,
        "flux_sensitivity": 0.048,
        "thermal_sensitivity": 0.008,
        "noise_base": 1.06,
        "bit_error_scale": 0.95,
        "gain_sensitivity": 0.020,
        "confidence_floor": 0.25,
    },
    "command_decoder": {
        "dose_sensitivity": 0.0014,
        "rate_sensitivity": 0.0010,
        "flux_sensitivity": 0.055,
        "thermal_sensitivity": 0.006,
        "noise_base": 1.00,
        "bit_error_scale": 1.35,
        "confidence_floor": 0.30,
    },
    "onboard_processor": {
        "dose_sensitivity": 0.0017,
        "rate_sensitivity": 0.0012,
        "flux_sensitivity": 0.060,
        "thermal_sensitivity": 0.007,
        "noise_base": 1.00,
        "bit_error_scale": 1.50,
        "confidence_floor": 0.25,
    },
}


def aluminum_shielding_factor(shielding_mm_aluminum: float, halving_thickness_mm: float = 7.0) -> float:
    """Simple shielding attenuation used by the live sim and lookup generator."""
    if shielding_mm_aluminum <= 0:
        return 1.0
    return 0.5 ** (shielding_mm_aluminum / halving_thickness_mm)


def effective_environment(inputs: DeviceInputs) -> Dict[str, float]:
    """
    Converts raw environment to device-effective environment.

    sun_exposure_factor reduces direct solar storm contribution, but does not
    eliminate trapped-belt or GCR-like exposure. trapped_belt_factor lets the
    table represent stronger degradation in Van Allen zones.
    """
    shield = aluminum_shielding_factor(inputs.shielding_mm_aluminum)
    sun_factor = clamp(inputs.sun_exposure_factor, 0.0, 1.0)
    belt_factor = max(0.0, inputs.trapped_belt_factor)

    # Keep some residual exposure in eclipse because belts/GCR remain.
    solar_exposure_modifier = 0.25 + 0.75 * sun_factor
    region_modifier = max(0.15, 0.35 + 0.65 * belt_factor)

    effective_dose = inputs.cumulative_dose_msv * shield * region_modifier
    effective_dose_rate = inputs.dose_rate_msv_per_day * shield * (0.55 * region_modifier + 0.45 * solar_exposure_modifier)
    effective_flux = inputs.particle_flux_pfu * shield * (0.60 * region_modifier + 0.40 * solar_exposure_modifier)

    return {
        "shielding_factor": shield,
        "solar_exposure_modifier": solar_exposure_modifier,
        "region_modifier": region_modifier,
        "effective_dose_msv": effective_dose,
        "effective_dose_rate_msv_per_day": effective_dose_rate,
        "effective_particle_flux_pfu": effective_flux,
    }


def base_semiconductor_effects(inputs: DeviceInputs) -> Dict[str, float]:
    profile = SENSOR_PROFILES[inputs.sensor_type]
    env = effective_environment(inputs)
    effective_dose = env["effective_dose_msv"]
    effective_dose_rate = env["effective_dose_rate_msv_per_day"]
    effective_flux = env["effective_particle_flux_pfu"]
    over_temp = max(0.0, inputs.temperature_c - 25.0)

    # Proxy for total ionizing dose / interface trap response.
    threshold_voltage_shift_mv = (
        0.42 * effective_dose
        + 0.18 * effective_dose_rate
        + 0.006 * safe_sqrt(effective_flux)
        + 0.015 * over_temp
    )

    # Proxy for leakage current growth. Temperature is exponential-ish.
    leakage_current_multiplier = 1.0 + (
        profile["dose_sensitivity"] * effective_dose
        + 0.012 * math.exp(over_temp / 55.0)
        + 0.00012 * safe_sqrt(effective_flux)
    )

    noise_multiplier = profile["noise_base"] + (
        0.0018 * threshold_voltage_shift_mv
        + 0.11 * (leakage_current_multiplier - 1.0)
        + profile["thermal_sensitivity"] * max(0.0, inputs.temperature_c - 40.0)
    )

    adc_bit_error_probability = clamp(
        profile["bit_error_scale"] * (
            0.000020 * effective_dose
            + 0.000050 * effective_dose_rate
            + 0.000004 * safe_sqrt(effective_flux)
            + 0.000700 * max(0.0, inputs.temperature_c - 60.0)
        ),
        0.0,
        0.35,
    )

    memory_bit_flip_probability = clamp(
        profile["bit_error_scale"] * (
            0.000010 * effective_dose
            + 0.000008 * effective_flux
            + 0.000300 * max(0.0, inputs.temperature_c - 70.0)
        ),
        0.0,
        0.65,
    )

    sensor_gain_drift_percent = clamp(
        -0.030 * threshold_voltage_shift_mv - 1.70 * (leakage_current_multiplier - 1.0),
        -45.0,
        0.0,
    )

    return {
        **env,
        "threshold_voltage_shift_mv": threshold_voltage_shift_mv,
        "leakage_current_multiplier": leakage_current_multiplier,
        "noise_multiplier": noise_multiplier,
        "adc_bit_error_probability": adc_bit_error_probability,
        "memory_bit_flip_probability": memory_bit_flip_probability,
        "sensor_gain_drift_percent": sensor_gain_drift_percent,
    }


def run_cmos_imager_proxy_model(inputs: DeviceInputs, base: Dict[str, float]) -> Dict[str, Any]:
    profile = SENSOR_PROFILES[inputs.sensor_type]
    effective_dose = base["effective_dose_msv"]
    effective_flux = base["effective_particle_flux_pfu"]
    over_temp = max(0.0, inputs.temperature_c - 25.0)

    dark_current_multiplier = 1.0 + (
        profile.get("dark_current_sensitivity", 0.0) * effective_dose
        + 0.018 * math.exp(over_temp / 35.0)
        + 0.00020 * safe_sqrt(effective_flux)
    )

    hot_pixel_fraction = clamp(
        profile.get("hot_pixel_sensitivity", 0.0) * effective_dose
        + 0.0000020 * effective_flux
        + 0.0000100 * max(0.0, inputs.temperature_c - 60.0),
        0.0,
        0.25,
    )

    dead_pixel_fraction = clamp(
        profile.get("dead_pixel_sensitivity", 0.0) * effective_dose
        + 0.0000004 * effective_flux,
        0.0,
        0.12,
    )

    image_noise_fraction = clamp(
        0.010 * (base["noise_multiplier"] - 1.0)
        + 0.018 * (dark_current_multiplier - 1.0)
        + 0.90 * hot_pixel_fraction
        + 1.20 * dead_pixel_fraction,
        0.0,
        0.75,
    )

    frame_corruption_probability = clamp(
        0.000020 * effective_flux
        + 0.000010 * effective_dose
        + 0.000500 * max(0.0, inputs.temperature_c - 70.0),
        0.0,
        0.60,
    )

    star_false_detection_probability = 0.0
    if inputs.sensor_type == "star_tracker_cmos":
        star_false_detection_probability = clamp(
            0.60 * image_noise_fraction + 8.0 * hot_pixel_fraction + frame_corruption_probability,
            0.0,
            0.95,
        )

    return {
        "camera_effects": {
            "dark_current_multiplier": dark_current_multiplier,
            "hot_pixel_fraction": hot_pixel_fraction,
            "dead_pixel_fraction": dead_pixel_fraction,
            "image_noise_fraction": image_noise_fraction,
            "frame_corruption_probability": frame_corruption_probability,
            "star_false_detection_probability": star_false_detection_probability,
        }
    }


def run_adc_readout_proxy_model(inputs: DeviceInputs, base: Dict[str, float]) -> Dict[str, Any]:
    profile = SENSOR_PROFILES[inputs.sensor_type]
    effective_dose = base["effective_dose_msv"]
    effective_flux = base["effective_particle_flux_pfu"]

    offset_drift_percent = clamp(
        profile.get("bias_sensitivity", 0.006) * effective_dose
        + 0.0008 * safe_sqrt(effective_flux)
        + 0.025 * max(0.0, inputs.temperature_c - 45.0),
        0.0,
        35.0,
    )

    readout_noise_fraction = clamp(
        0.006 * (base["noise_multiplier"] - 1.0)
        + 0.0005 * offset_drift_percent
        + base["adc_bit_error_probability"],
        0.0,
        0.65,
    )

    bad_sample_probability = clamp(
        base["adc_bit_error_probability"]
        + 0.000010 * effective_flux
        + 0.000300 * max(0.0, inputs.temperature_c - 75.0),
        0.0,
        0.60,
    )

    return {
        "readout_effects": {
            "offset_drift_percent": offset_drift_percent,
            "readout_noise_fraction": readout_noise_fraction,
            "bad_sample_probability": bad_sample_probability,
        }
    }


def run_rf_mosfet_proxy_model(inputs: DeviceInputs, base: Dict[str, float]) -> Dict[str, Any]:
    profile = SENSOR_PROFILES[inputs.sensor_type]
    effective_flux = base["effective_particle_flux_pfu"]
    threshold_shift = base["threshold_voltage_shift_mv"]
    leakage_delta = base["leakage_current_multiplier"] - 1.0

    rf_noise_floor_shift_db = clamp(10.0 * safe_log10(max(base["noise_multiplier"], 1.0)), 0.0, 14.0)
    gain_loss_db = clamp(profile.get("gain_sensitivity", 0.018) * threshold_shift + 0.55 * leakage_delta, 0.0, 10.0)
    phase_noise_growth_db = clamp(0.35 * rf_noise_floor_shift_db + 0.08 * safe_sqrt(effective_flux), 0.0, 12.0)
    packet_error_probability = clamp(
        0.02 * gain_loss_db
        + 0.018 * rf_noise_floor_shift_db
        + base["memory_bit_flip_probability"] * 0.30,
        0.0,
        0.85,
    )

    return {
        "rf_effects": {
            "rf_noise_floor_shift_db": rf_noise_floor_shift_db,
            "gain_loss_db": gain_loss_db,
            "phase_noise_growth_db": phase_noise_growth_db,
            "packet_error_probability": packet_error_probability,
        }
    }


def run_digital_cmos_proxy_model(inputs: DeviceInputs, base: Dict[str, float]) -> Dict[str, Any]:
    effective_flux = base["effective_particle_flux_pfu"]
    timing_margin_loss_percent = clamp(
        0.06 * base["threshold_voltage_shift_mv"]
        + 1.20 * (base["leakage_current_multiplier"] - 1.0)
        + 0.04 * max(0.0, inputs.temperature_c - 70.0),
        0.0,
        70.0,
    )
    reset_probability_per_day = clamp(
        0.40 * base["memory_bit_flip_probability"]
        + 0.000010 * effective_flux
        + 0.001000 * max(0.0, inputs.temperature_c - 85.0),
        0.0,
        0.90,
    )
    command_decode_error_probability = clamp(
        0.65 * base["memory_bit_flip_probability"]
        + 0.35 * base["adc_bit_error_probability"],
        0.0,
        0.75,
    )

    return {
        "digital_effects": {
            "timing_margin_loss_percent": timing_margin_loss_percent,
            "reset_probability_per_day": reset_probability_per_day,
            "command_decode_error_probability": command_decode_error_probability,
        }
    }


def classify_latchup_risk(error_probability: float, temperature_c: float, effective_flux_pfu: float) -> str:
    if error_probability > 0.08 or temperature_c >= 95 or effective_flux_pfu >= 5000:
        return "critical"
    if error_probability > 0.035 or temperature_c >= 75 or effective_flux_pfu >= 1000:
        return "high"
    if error_probability > 0.012 or temperature_c >= 55 or effective_flux_pfu >= 200:
        return "medium"
    return "low"


def compute_confidence(inputs: DeviceInputs, base: Dict[str, float], family_outputs: Dict[str, Any]) -> Dict[str, Any]:
    profile = SENSOR_PROFILES[inputs.sensor_type]
    effective_flux = base["effective_particle_flux_pfu"]

    risk_penalty = (
        profile["dose_sensitivity"] * base["effective_dose_msv"]
        + profile["rate_sensitivity"] * base["effective_dose_rate_msv_per_day"]
        + profile["flux_sensitivity"] * safe_log10(1.0 + effective_flux)
        + profile["thermal_sensitivity"] * max(0.0, inputs.temperature_c - 40.0)
        + 1.2 * base["adc_bit_error_probability"]
        + 0.9 * base["memory_bit_flip_probability"]
    )

    # Add sensor-family specific penalties.
    cam = family_outputs.get("camera_effects", {})
    if cam:
        risk_penalty += (
            0.85 * cam.get("image_noise_fraction", 0.0)
            + 1.40 * cam.get("frame_corruption_probability", 0.0)
            + 2.00 * cam.get("dead_pixel_fraction", 0.0)
        )

    rf = family_outputs.get("rf_effects", {})
    if rf:
        risk_penalty += 0.035 * rf.get("gain_loss_db", 0.0) + 0.025 * rf.get("rf_noise_floor_shift_db", 0.0)

    readout = family_outputs.get("readout_effects", {})
    if readout:
        risk_penalty += 0.8 * readout.get("readout_noise_fraction", 0.0) + 0.8 * readout.get("bad_sample_probability", 0.0)

    digital = family_outputs.get("digital_effects", {})
    if digital:
        risk_penalty += 0.010 * digital.get("timing_margin_loss_percent", 0.0) + 1.1 * digital.get("reset_probability_per_day", 0.0)

    floor = profile.get("confidence_floor", 0.05)
    sensor_confidence = clamp(1.0 - risk_penalty, floor, 1.0)

    if sensor_confidence >= 0.85:
        trust_level = "normal"
    elif sensor_confidence >= 0.65:
        trust_level = "slightly_downweighted"
    elif sensor_confidence >= 0.40:
        trust_level = "heavily_downweighted"
    else:
        trust_level = "quarantine_sensor_data"

    # Convert instantaneous condition into rates the live sim can integrate.
    health_loss_per_day = clamp((1.0 - sensor_confidence) * 2.4 + 0.012 * base["effective_dose_rate_msv_per_day"], 0.0, 20.0)
    noise_growth_per_day = clamp((base["noise_multiplier"] - 1.0) * 0.015 + base["adc_bit_error_probability"] * 0.25, 0.0, 1.0)
    bias_drift_per_day = clamp(abs(base["sensor_gain_drift_percent"]) * 0.002 + base["threshold_voltage_shift_mv"] * 0.00008, 0.0, 0.5)
    seu_probability_per_day = clamp(base["memory_bit_flip_probability"] + base["adc_bit_error_probability"], 0.0, 0.95)

    # Use digital/rf/camera-specific corruption when present.
    if cam:
        seu_probability_per_day = clamp(seu_probability_per_day + cam.get("frame_corruption_probability", 0.0) * 0.40, 0.0, 0.95)
    if rf:
        seu_probability_per_day = clamp(seu_probability_per_day + rf.get("packet_error_probability", 0.0) * 0.25, 0.0, 0.95)
    if digital:
        seu_probability_per_day = clamp(seu_probability_per_day + digital.get("reset_probability_per_day", 0.0) * 0.50, 0.0, 0.95)

    latchup_risk = classify_latchup_risk(seu_probability_per_day, inputs.temperature_c, effective_flux)

    return {
        "sensor_effects": {
            "sensor_confidence": sensor_confidence,
            "trust_level": trust_level,
            "requires_acknowledgement": bool(inputs.sensor_type in ["command_decoder", "onboard_processor"] and sensor_confidence < 0.78),
            "safe_to_use_for_autonomous_control": bool(sensor_confidence >= 0.65 and latchup_risk not in ["high", "critical"]),
            "health_loss_per_day": health_loss_per_day,
            "noise_growth_per_day": noise_growth_per_day,
            "bias_drift_per_day": bias_drift_per_day,
            "seu_probability_per_day": seu_probability_per_day,
            "confidence_floor": floor,
        },
        "risk_flags": {
            "latchup_risk": latchup_risk,
            "high_noise": bool(base["noise_multiplier"] > 1.8),
            "high_bit_flip_risk": bool(base["memory_bit_flip_probability"] > 0.05),
            "high_temperature": bool(inputs.temperature_c >= 75.0),
        },
    }


def round_nested(value: Any, digits: int = 8) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, dict):
        return {k: round_nested(v, digits) for k, v in value.items()}
    if isinstance(value, list):
        return [round_nested(v, digits) for v in value]
    return value


def run_device_model(inputs: DeviceInputs, full_devsim_requested: bool = False) -> Dict[str, Any]:
    """
    Runs the device model for one lookup-table case.

    full_devsim_requested is accepted so the generator can be wired to use real
    DEVSIM later. Current proxy model always returns full_devsim_solve_used=False.
    """
    if inputs.sensor_type not in SENSOR_PROFILES:
        raise ValueError(f"Unknown sensor_type: {inputs.sensor_type}")

    family = SENSOR_DEVICE_FAMILY.get(inputs.sensor_type, "generic")
    base = base_semiconductor_effects(inputs)

    family_outputs: Dict[str, Any] = {}
    if family == "cmos_imager":
        family_outputs.update(run_cmos_imager_proxy_model(inputs, base))
    elif family == "adc_readout":
        family_outputs.update(run_adc_readout_proxy_model(inputs, base))
    elif family == "rf_mosfet":
        family_outputs.update(run_rf_mosfet_proxy_model(inputs, base))
    elif family == "digital_cmos":
        family_outputs.update(run_digital_cmos_proxy_model(inputs, base))

    confidence_outputs = compute_confidence(inputs, base, family_outputs)

    semiconductor_effects = {
        "threshold_voltage_shift_mv": base["threshold_voltage_shift_mv"],
        "leakage_current_multiplier": base["leakage_current_multiplier"],
        "noise_multiplier": base["noise_multiplier"],
        "sensor_gain_drift_percent": base["sensor_gain_drift_percent"],
        "adc_bit_error_probability": base["adc_bit_error_probability"],
        "memory_bit_flip_probability": base["memory_bit_flip_probability"],
        "effective_dose_msv": base["effective_dose_msv"],
        "effective_dose_rate_msv_per_day": base["effective_dose_rate_msv_per_day"],
        "effective_particle_flux_pfu": base["effective_particle_flux_pfu"],
        "shielding_factor": base["shielding_factor"],
        "solar_exposure_modifier": base["solar_exposure_modifier"],
        "region_modifier": base["region_modifier"],
    }

    result = {
        "device_family": family,
        "model_runtime": {
            "model_source": "tcad_inspired_device_proxy_model_devsim_ready",
            "full_devsim_solve_used": False,
            "full_devsim_requested": bool(full_devsim_requested),
        },
        "semiconductor_effects": semiconductor_effects,
        **family_outputs,
        **confidence_outputs,
    }

    return round_nested(result, digits=8)
