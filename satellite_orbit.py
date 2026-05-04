from vpython import *
import json
import os
import time
import math
from datetime import datetime, timezone
import sys

# Allow this simulation to import the TCAD runtime helper from the DryDock/tcad folder.
# The helper loads tcad_lookup_table.json once and uses a fast indexed lookup during telemetry.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
_TCAD_DIR_CANDIDATES = [
    os.path.join(_THIS_DIR, "tcad"),
    os.path.join(os.getcwd(), "tcad"),
    "/Users/kyuhyunkim/DryDock/tcad",
]
for _tcad_dir in _TCAD_DIR_CANDIDATES:
    if os.path.isdir(_tcad_dir) and _tcad_dir not in sys.path:
        sys.path.insert(0, _tcad_dir)

try:
    from tcad_lookup_runtime import (
        load_tcad_lookup,
        lookup_sensor_degradation,
        region_to_trapped_belt_factor,
        sensor_type_for_sim_field,
    )
    TCAD_RUNTIME_IMPORT_AVAILABLE = True
except Exception as _tcad_import_error:
    load_tcad_lookup = None
    lookup_sensor_degradation = None
    region_to_trapped_belt_factor = None
    sensor_type_for_sim_field = None
    TCAD_RUNTIME_IMPORT_AVAILABLE = False
    TCAD_RUNTIME_IMPORT_ERROR = str(_tcad_import_error)

# ============================================================
# Earth + Sun + Moon Satellite Sensor-Fusion Simulation
# ============================================================
# Earth/Sun/Moon version: no Mars, no Mars satellites/asteroids, no transfer spacecraft.
# Outputs JSON snapshots into ready_to_send_telemetry/ for the SMS/MQTT bridge.
#
# Included sensor-fusion telemetry:
# - Earth orbit physics with J2 + drag + Sun/Moon third-body perturbations
# - Earth/Sun/Moon geometry with true Earth-Sun distance, Earth orbital motion, and Moon orbit
# - Sunlight/eclipse state
# - Passive RF detections using Maxwell-derived far-field link budget
# - Radiation belts + solar storm window starting at 25 visual seconds
# - Radiation/electronics health fields
# - Attitude/panel vectors for external roll/panel optimization
# - Solar panel, voltage, battery/power telemetry
# - Thermal sensor telemetry
# - Communication link telemetry
# - Sensor fusion risk/health scores, TCAD lookup-table degradation, per-sensor confidence scores, and decision_request JSON
#
# The simulation DOES NOT decide routing or optimize roll internally.
# It only applies external commands if another program writes them to quantum_commands.json.
# ============================================================

# -----------------------------
# Pre-simulation input
# -----------------------------
def read_nonnegative_int(prompt_text, default_value):
    while True:
        raw = input(f"{prompt_text} [{default_value}]: ").strip()
        if raw == "":
            return default_value
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number like 0, 1, 2, 3, etc.")
            continue
        if value < 0:
            print("Please enter 0 or a positive number.")
            continue
        return value


def read_float_value(prompt_text, default_value, low=None, high=None):
    while True:
        raw = input(f"{prompt_text} [{default_value}]: ").strip()
        if raw == "":
            return float(default_value)
        try:
            value = float(raw)
        except ValueError:
            print("Please enter a number like 36.1627 or -86.7816.")
            continue
        if low is not None and value < low:
            print(f"Please enter a value >= {low}.")
            continue
        if high is not None and value > high:
            print(f"Please enter a value <= {high}.")
            continue
        return value


def read_text_value(prompt_text, default_value):
    raw = input(f"{prompt_text} [{default_value}]: ").strip()
    return default_value if raw == "" else raw


print("\nEarth satellite constellation setup")
print("Enter how many satellites you want in each Earth orbit class.")
print("Press Enter to use the default values.\n")

REQUESTED_LEO_SATELLITES = read_nonnegative_int("Number of LEO satellites", 2)
REQUESTED_MEO_SATELLITES = read_nonnegative_int("Number of MEO satellites", 1)
REQUESTED_HEO_SATELLITES = read_nonnegative_int("Number of HEO satellites", 1)

print("\nEarth asteroid hazard setup")
print("Enter how many asteroid hazards you want in each Earth orbit class.")
print("Default is 1 LEO asteroid so the collision demo still happens.\n")

REQUESTED_LEO_ASTEROIDS = read_nonnegative_int("Number of LEO asteroids", 1)
REQUESTED_MEO_ASTEROIDS = read_nonnegative_int("Number of MEO asteroids", 0)
REQUESTED_HEO_ASTEROIDS = read_nonnegative_int("Number of HEO asteroids", 0)

if REQUESTED_LEO_SATELLITES + REQUESTED_MEO_SATELLITES + REQUESTED_HEO_SATELLITES == 0:
    print("You entered 0 total satellites, so the sim will create one default LEO satellite so the demo still runs.")
    REQUESTED_LEO_SATELLITES = 1

print(
    f"\nCreating Earth constellation: "
    f"{REQUESTED_LEO_SATELLITES} LEO, "
    f"{REQUESTED_MEO_SATELLITES} MEO, "
    f"{REQUESTED_HEO_SATELLITES} HEO satellites.\n"
)

print(
    f"Creating Earth asteroid hazards: "
    f"{REQUESTED_LEO_ASTEROIDS} LEO, "
    f"{REQUESTED_MEO_ASTEROIDS} MEO, "
    f"{REQUESTED_HEO_ASTEROIDS} HEO asteroids.\n"
)

print("\nCamera imaging target setup")
print("Enter the ground location the satellites should report camera distance to.")
print("This does not force a satellite to image it; it only marks the target and exports geometry for the external decision program.\n")

INITIAL_TARGET_NAME = read_text_value("Target name", "Nashville target")
INITIAL_TARGET_LAT_DEG = read_float_value("Target latitude degrees", 36.1627, -90.0, 90.0)
INITIAL_TARGET_LON_DEG = read_float_value("Target longitude degrees", -86.7816, -180.0, 180.0)
active_data_target = {"name": INITIAL_TARGET_NAME, "lat_deg": INITIAL_TARGET_LAT_DEG, "lon_deg": INITIAL_TARGET_LON_DEG}

# -----------------------------
# Scene setup
# -----------------------------
scene.title = "Earth + Sun + Moon Satellite Sensor-Fusion Simulation"
scene.width = 1200
scene.height = 800
scene.background = color.black
scene.forward = vector(-1, -0.35, -0.9)
scene.center = vector(0, 0, 0)
scene.range = 5.2
scene.userspin = True
scene.userzoom = False
scene.userpan = True

# -----------------------------
# Physical constants
# -----------------------------
MU_EARTH = 3.986004418e14          # m^3/s^2
R_EARTH = 6371008.4                # m
EARTH_ROTATION_RATE = 7.2921159e-5 # rad/s
J2_EARTH = 1.08262668e-3
DRAG_COEFFICIENT = 2.2
EARTH_SURFACE_DENSITY_KG_M3 = 1.225
EARTH_SCALE_HEIGHT_M = 8500.0
MAX_DRAG_ALTITUDE_M = 800000.0
ENABLE_J2_PERTURBATION = True
ENABLE_ATMOSPHERIC_DRAG = True

MU_SUN = 1.32712440018e20          # m^3/s^2
AU_M = 149597870700.0              # m
R_SUN = 695700000.0                # m
SOLAR_CONSTANT_W_M2 = 1361.0       # W/m^2 at 1 AU
EARTH_HELIOCENTRIC_RADIUS_M = AU_M
EARTH_ORBITAL_ANGULAR_RATE_RAD_S = math.sqrt(MU_SUN / EARTH_HELIOCENTRIC_RADIUS_M ** 3)
EARTH_ORBITAL_SPEED_MPS = math.sqrt(MU_SUN / EARTH_HELIOCENTRIC_RADIUS_M)

VISUAL_SCALE = 1.0 / R_EARTH       # 1 scene unit = 1 Earth radius
SUN_VISUAL_RADIUS_TRUE_SCALE = R_SUN / R_EARTH

# Moon constants. The simulation still uses an Earth-centered inertial frame for satellites,
# but the Moon position changes with time and contributes third-body perturbation gravity.
MU_MOON = 4.9048695e12              # m^3/s^2
R_MOON = 1737400.0                  # m
MOON_SEMI_MAJOR_AXIS_M = 384400000.0
MOON_ORBITAL_PERIOD_S = 27.321661 * 86400.0
MOON_ORBITAL_ANGULAR_RATE_RAD_S = 2.0 * pi / MOON_ORBITAL_PERIOD_S
MOON_ORBITAL_SPEED_MPS = math.sqrt(MU_EARTH / MOON_SEMI_MAJOR_AXIS_M)
MOON_ORBIT_INCLINATION_DEG = 5.145
MOON_INITIAL_PHASE_DEG = 40.0
MOON_VISUAL_RADIUS_TRUE_SCALE = R_MOON / R_EARTH
MOON_TRAIL_RETAIN = 2500
ENABLE_SOLAR_THIRD_BODY_GRAVITY = True
ENABLE_LUNAR_THIRD_BODY_GRAVITY = True

# Physics timestep
BASE_DT = 5.0
dt = BASE_DT
rate_value = 120
SIMULATION_SPEED_OPTIONS = [1, 2, 4, 8]
simulation_speed_index = 0
simulation_speed_multiplier = SIMULATION_SPEED_OPTIONS[simulation_speed_index]

# Live command / telemetry
COMMAND_FILE = "quantum_commands.json"
COMMAND_CHECK_INTERVAL_FRAMES = 30
TELEMETRY_OUTPUT_MODE = "mqtt_outbox"
BASE_TELEMETRY_SAMPLE_HZ = 10.0
MQTT_TELEMETRY_OUTPUT_DIR = "ready_to_send_telemetry"
os.makedirs(MQTT_TELEMETRY_OUTPUT_DIR, exist_ok=True)

# Old single-file fallback, not default
TELEMETRY_JSON_FILENAME = "satellite_sim_live_telemetry.json"
TELEMETRY_JSON_PATH = os.path.join(os.path.expanduser("~/Desktop"), TELEMETRY_JSON_FILENAME)
TELEMETRY_JSON_TEMP_PATH = TELEMETRY_JSON_PATH + ".tmp"

# -----------------------------
# Passive RF model
# -----------------------------
PASSIVE_RF_ENABLED = True
SPEED_OF_LIGHT_MPS = 299_792_458.0
RF_MODEL_TYPE = "maxwell_derived_far_field_link_budget"
RF_FREQUENCY_HZ = 2.20e9
RF_WAVELENGTH_M = SPEED_OF_LIGHT_MPS / RF_FREQUENCY_HZ
RF_NOISE_FLOOR_DBM = -110.0
RF_MIN_DETECTABLE_SNR_DB = 8.0
RF_DEFAULT_RANGE_M = 3_000_000.0
RF_ANTENNA_GAIN_DBI = 14.0
RF_PROCESSING_GAIN_DB = 6.0
RF_RANGE_NOISE_FRACTION = 0.015
RF_BEARING_NOISE_RAD = 0.0025
RF_SIGNATURE_DBM_AT_1M = {"asteroid": -5.0, "debris": -25.0, "satellite": 5.0}
MAX_RF_DETECTIONS_PER_SENSOR = 15
MAX_TOTAL_RF_DETECTIONS = 300
SHOW_RF_SENSOR_HIGHLIGHT = True

# -----------------------------
# Radiation / space weather model
# -----------------------------
RADIATION_MODEL_ENABLED = True
RADIATION_MODEL_TYPE = "earth_orbit_space_weather_and_radiation_belt_model"
RADIATION_DOSE_TRACKING_ENABLED = True
BASE_GCR_DOSE_RATE_MSV_PER_DAY = 1.8
EARTH_LEO_BASE_DOSE_RATE_MSV_PER_DAY = 0.35
VAN_ALLEN_INNER_BELT_FACTOR = 18.0
VAN_ALLEN_OUTER_BELT_FACTOR = 8.0
DEFAULT_SATELLITE_SHIELDING_MM_AL = 2.5
DEFAULT_ASTEROID_SHIELDING_MM_AL = 0.0
DEFAULT_DEBRIS_SHIELDING_MM_AL = 0.2
RADIATION_SHIELDING_HALVING_THICKNESS_MM_AL = 7.0
RADIATION_HIGH_DOSE_RATE_MSV_PER_DAY = 8.0
RADIATION_CRITICAL_DOSE_RATE_MSV_PER_DAY = 25.0
SEU_HIGH_RATE_PER_DAY = 0.05
SEU_CRITICAL_RATE_PER_DAY = 0.20
RADIATION_HEALTH_DOSE_DAMAGE_SCALE_MSV = 120.0
RADIATION_HEALTH_SEU_DAMAGE_WEIGHT = 18.0
RADIATION_HEALTH_PANEL_DAMAGE_WEIGHT = 3.0
RADIATION_FAULT_WATCH_SEU_PROBABILITY = 0.02
RADIATION_FAULT_DEGRADED_SEU_PROBABILITY = 0.08
RADIATION_FAULT_CRITICAL_SEU_PROBABILITY = 0.18

SOLAR_STORM_EVENTS = [
    {
        "name": "SOLAR-STORM-WINDOW-25S",
        "start_visual_time_s": 25.0,
        "duration_visual_time_s": 24.0,
        "severity": "moderate",
        "peak_solar_proton_flux_pfu": 120.0,
        "peak_kp_index": 6.0,
        "peak_solar_wind_speed_km_s": 650.0,
        "peak_dose_multiplier": 6.0,
        "rf_blackout_probability_peak": 0.18,
    },
    {
        "name": "SPE-DEMO-2",
        "start_visual_time_s": 62.0,
        "duration_visual_time_s": 24.0,
        "severity": "strong",
        "peak_solar_proton_flux_pfu": 850.0,
        "peak_kp_index": 8.0,
        "peak_solar_wind_speed_km_s": 880.0,
        "peak_dose_multiplier": 18.0,
        "rf_blackout_probability_peak": 0.42,
    },
]

radiation_cumulative_dose_msv_by_object = {}
radiation_electronics_health_by_object = {}
radiation_fault_state_by_object = {}
radiation_last_update_physical_time_s = None

# -----------------------------
# Solar / power / thermal sensor-fusion constants
# -----------------------------
SOLAR_SENSOR_MODEL_ENABLED = True
SAT_SOLAR_PANEL_AREA_M2 = 4.0
SAT_SOLAR_PANEL_EFFICIENCY = 0.28
SAT_MAX_SOLAR_GENERATION_W = SOLAR_CONSTANT_W_M2 * SAT_SOLAR_PANEL_AREA_M2 * SAT_SOLAR_PANEL_EFFICIENCY
SAT_BASE_LOAD_W = 260.0
SAT_RF_PAYLOAD_LOAD_W = 65.0
SAT_MAX_BATTERY_WH = 1200.0
SAT_NOMINAL_BUS_VOLTAGE_V = 28.0
SAT_PANEL_NOMINAL_VOLTAGE_V = 36.0
SAT_BATTERY_NOMINAL_VOLTAGE_V = 28.8
SAT_PANEL_TRACKING_NOISE_FRACTION = 0.015
SAT_VOLTAGE_SENSOR_NOISE_FRACTION = 0.008
SAT_TEMPERATURE_SENSOR_NOISE_C = 0.25
SAT_THERMAL_TIME_CONSTANT = 0.03
SAT_PANEL_ROTATION_RATE_LIMIT_DEG_PER_S = 1.2
SAT_ATTITUDE_EXTERNAL_CONTROL_DEFAULT = "default_simulated_attitude_or_external_commands"

satellite_power_state = {}
satellite_thermal_state = {}
satellite_attitude_state = {}

# Camera sensor continuous degradation state. The lookup-table/degradation layer returns
# rates, then the sim integrates those rates over time so longer radiation exposure keeps
# degrading sensor performance.
CAMERA_SENSOR_MODEL_ENABLED = True
CAMERA_BASE_IMAGE_NOISE_FRACTION = 0.018
CAMERA_BASE_HOT_PIXEL_FRACTION = 0.00002
CAMERA_BASE_DEAD_PIXEL_FRACTION = 0.000005
CAMERA_BASE_DARK_CURRENT_FACTOR = 1.0
CAMERA_BASE_FRAME_CORRUPTION_PROBABILITY = 0.001
CAMERA_MIN_USEFUL_HEALTH_PERCENT = 35.0
CAMERA_NADIR_HALF_ANGLE_DEG = 42.0
CAMERA_MAX_USEFUL_SLANT_RANGE_M = 2_500_000.0
CAMERA_DIFFRACTION_LIMIT_NOTE = "Simplified imaging model: geometry, radiation degradation, thermal noise, and frame corruption are exported for external tasking. It is not a full optical ray-trace."
camera_sensor_state = {}

# TCAD lookup-table runtime. The lookup table is generated offline by:
#   /Users/kyuhyunkim/DryDock/tcad/generate_tcad_lookup_table.py
# The simulation loads it once, builds/uses a SQLite index, and then continuously
# integrates degradation for every sensor type represented in the sim.
TCAD_LOOKUP_ENABLED = True
TCAD_LOOKUP_TABLE_PATH = os.environ.get(
    "TCAD_LOOKUP_TABLE_PATH",
    "/Users/kyuhyunkim/DryDock/tcad/tcad_lookup_table.json",
)
TCAD_LOOKUP_SQLITE_PATH = os.environ.get(
    "TCAD_LOOKUP_SQLITE_PATH",
    TCAD_LOOKUP_TABLE_PATH + ".sqlite",
)
tcad_lookup = None
sensor_degradation_state = {}

SIM_SENSOR_TO_TCAD_SENSOR = {
    "camera_sensor": "visible_camera_cmos",
    "attitude_state": "star_tracker_cmos",
    "solar_panel_system": "solar_panel_current_sensor",
    "voltage_sensors": "voltage_sensor_adc",
    "thermal_profile": "thermal_sensor_readout",
    "communication_link": "communication_radio",
    "passive_rf": "rf_frontend",
    "command_decoder": "command_decoder",
    "onboard_processor": "onboard_processor",
}

SENSOR_CONFIDENCE_SCORE_KEYS = {
    "camera_sensor": "camera_sensor_confidence",
    "attitude_state": "attitude_sensor_confidence",
    "solar_panel_system": "solar_panel_sensor_confidence",
    "voltage_sensors": "voltage_sensor_confidence",
    "thermal_profile": "thermal_sensor_confidence",
    "communication_link": "communication_sensor_confidence",
    "passive_rf": "passive_rf_sensor_confidence",
    "onboard_processor": "radiation_sensor_confidence",
}

# Demo timing target for guaranteed asteroid collision
DESIRED_VISUAL_COLLISION_TIME_S = 18.0

# -----------------------------
# Utility helpers
# -----------------------------
def meters_to_scene(v):
    return v * VISUAL_SCALE


def scene_to_meters(v):
    return v / VISUAL_SCALE


def vector_to_dict(v):
    return {"x": float(v.x), "y": float(v.y), "z": float(v.z)}


def speed_mps(v):
    return float(mag(v))


def clamp_value(value, low, high):
    return max(low, min(high, value))


def unit_vector_or_zero(v):
    if mag(v) == 0:
        return vector(0, 0, 0)
    return norm(v)


def random_unit_vector():
    d = vector(random() - 0.5, random() - 0.5, random() - 0.5)
    if mag(d) == 0:
        return vector(1, 0, 0)
    return norm(d)


def circular_speed(radius_m):
    return sqrt(MU_EARTH / radius_m)


def altitude_m(position_m):
    return mag(position_m) - R_EARTH


def orbital_energy_j_per_kg(position_m, velocity_mps):
    r = mag(position_m)
    if r == 0:
        return 0.0
    return float(0.5 * mag(velocity_mps) ** 2 - MU_EARTH / r)


def point_to_segment_distance(point, segment_a, segment_b):
    segment = segment_b - segment_a
    seg_len2 = mag(segment) ** 2
    if seg_len2 == 0:
        return mag(point - segment_a), 0.0
    t = dot(point - segment_a, segment) / seg_len2
    t_clamped = max(0.0, min(1.0, t))
    closest = segment_a + segment * t_clamped
    return mag(point - closest), t_clamped


def perpendicular_velocity_dir(position_m, velocity_mps):
    radial_dir = norm(position_m)
    tangential = velocity_mps - dot(velocity_mps, radial_dir) * radial_dir
    if mag(tangential) == 0:
        temp = cross(vector(0, 0, 1), radial_dir)
        if mag(temp) == 0:
            temp = cross(vector(0, 1, 0), radial_dir)
        return norm(temp)
    return norm(tangential)


def cross_section_area_from_radius(radius_m):
    if radius_m is None or radius_m <= 0:
        return None
    return pi * radius_m ** 2


def object_cross_section_area_m2(obj, default_radius_m=1.0):
    if "drag_area_m2" in obj:
        return obj["drag_area_m2"]
    return cross_section_area_from_radius(obj.get("physical_radius_m", default_radius_m))

# -----------------------------
# Sun / Earth heliocentric environment
# -----------------------------
def earth_heliocentric_state(physical_time_s):
    theta = EARTH_ORBITAL_ANGULAR_RATE_RAD_S * physical_time_s
    pos = vector(
        EARTH_HELIOCENTRIC_RADIUS_M * cos(theta),
        EARTH_HELIOCENTRIC_RADIUS_M * sin(theta),
        0,
    )
    vel = vector(
        -EARTH_ORBITAL_SPEED_MPS * sin(theta),
        EARTH_ORBITAL_SPEED_MPS * cos(theta),
        0,
    )
    return pos, vel, theta


def current_sun_position_eci_m(physical_time_s):
    earth_pos, _, _ = earth_heliocentric_state(physical_time_s)
    # Earth-centered inertial display frame: the Sun appears opposite Earth's heliocentric position.
    return -earth_pos


def moon_geocentric_state(physical_time_s):
    theta = MOON_INITIAL_PHASE_DEG * pi / 180.0 + MOON_ORBITAL_ANGULAR_RATE_RAD_S * physical_time_s
    pos = vector(
        MOON_SEMI_MAJOR_AXIS_M * cos(theta),
        MOON_SEMI_MAJOR_AXIS_M * sin(theta),
        0,
    )
    vel = vector(
        -MOON_ORBITAL_SPEED_MPS * sin(theta),
        MOON_ORBITAL_SPEED_MPS * cos(theta),
        0,
    )
    pos = rotate(pos, angle=radians(MOON_ORBIT_INCLINATION_DEG), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(MOON_ORBIT_INCLINATION_DEG), axis=vector(1, 0, 0))
    return pos, vel, theta


def current_moon_position_eci_m(physical_time_s):
    pos, _, _ = moon_geocentric_state(physical_time_s)
    return pos


def current_moon_marker_pos():
    return meters_to_scene(current_moon_position_eci_m(simulation_physical_time))


def current_sun_marker_pos():
    return meters_to_scene(current_sun_position_eci_m(simulation_physical_time))


def solar_irradiance_at_position_w_m2(position_m, sun_position_m):
    distance_to_sun_m = mag(sun_position_m - position_m)
    if distance_to_sun_m <= 0:
        return SOLAR_CONSTANT_W_M2
    return SOLAR_CONSTANT_W_M2 * (AU_M / distance_to_sun_m) ** 2


def planet_shadow_state_for_object(position_m, sun_position_m):
    distance_to_earth_line_m, t_earth = point_to_segment_distance(vector(0, 0, 0), position_m, sun_position_m)
    earth_shadow_margin_m = distance_to_earth_line_m - R_EARTH
    if 0.001 < t_earth < 0.999 and earth_shadow_margin_m <= 0:
        return {
            "in_eclipse": True,
            "eclipse_body": "Earth",
            "shadow_margin_m": float(earth_shadow_margin_m),
            "distance_to_shadow_axis_m": float(distance_to_earth_line_m),
            "sun_exposure_factor": 0.0,
        }

    moon_position_m = current_moon_position_eci_m(simulation_physical_time)
    distance_to_moon_line_m, t_moon = point_to_segment_distance(moon_position_m, position_m, sun_position_m)
    moon_shadow_margin_m = distance_to_moon_line_m - R_MOON
    if 0.001 < t_moon < 0.999 and moon_shadow_margin_m <= 0:
        return {
            "in_eclipse": True,
            "eclipse_body": "Moon",
            "shadow_margin_m": float(moon_shadow_margin_m),
            "distance_to_shadow_axis_m": float(distance_to_moon_line_m),
            "sun_exposure_factor": 0.0,
        }

    nearest_body = "Earth"
    nearest_margin = earth_shadow_margin_m if 0.001 < t_earth < 0.999 else None
    nearest_axis = distance_to_earth_line_m if 0.001 < t_earth < 0.999 else None
    if 0.001 < t_moon < 0.999:
        if nearest_margin is None or moon_shadow_margin_m < nearest_margin:
            nearest_body = "Moon"
            nearest_margin = moon_shadow_margin_m
            nearest_axis = distance_to_moon_line_m

    return {
        "in_eclipse": False,
        "eclipse_body": None,
        "nearest_shadow_body": nearest_body,
        "shadow_margin_m": float(nearest_margin) if nearest_margin is not None else None,
        "distance_to_shadow_axis_m": float(nearest_axis) if nearest_axis is not None else None,
        "sun_exposure_factor": 1.0,
    }

def build_sunlight_state_payload(position_m, sun_position_m):
    state = planet_shadow_state_for_object(position_m, sun_position_m)
    return {
        "in_sunlight": bool(not state["in_eclipse"]),
        "in_eclipse": bool(state["in_eclipse"]),
        "eclipse_body": state.get("eclipse_body"),
        "nearest_shadow_body": state.get("nearest_shadow_body"),
        "sun_exposure_factor": float(state.get("sun_exposure_factor", 1.0)),
        "shadow_margin_m": state.get("shadow_margin_m"),
        "distance_to_shadow_axis_m": state.get("distance_to_shadow_axis_m"),
        "model_type": "cylindrical_earth_and_moon_shadow_geometry",
        "used_for": ["solar_panel_power_inputs", "thermal_sensor_inputs", "radiation_sun_exposure_inputs"],
    }


def build_environment_vectors_payload(object_id, object_type, position_m, velocity_mps, physical_time_s):
    sun_position_m = current_sun_position_eci_m(physical_time_s)
    moon_position_m, moon_velocity_mps, moon_theta = moon_geocentric_state(physical_time_s)
    earth_pos_helio, earth_vel_helio, theta = earth_heliocentric_state(physical_time_s)
    sun_vec = unit_vector_or_zero(sun_position_m - position_m)
    moon_vec = unit_vector_or_zero(moon_position_m - position_m)
    earth_vec = unit_vector_or_zero(vector(0, 0, 0) - position_m)
    velocity_unit = unit_vector_or_zero(velocity_mps)
    shadow_state = planet_shadow_state_for_object(position_m, sun_position_m)
    irradiance_no_eclipse = solar_irradiance_at_position_w_m2(position_m, sun_position_m)
    irradiance = irradiance_no_eclipse * shadow_state.get("sun_exposure_factor", 1.0)
    return {
        "schema": "satellite_simulation.environment_vectors.v2",
        "object_id": object_id,
        "object_type": object_type,
        "central_body": "Earth",
        "sun_position_m_eci": vector_to_dict(sun_position_m),
        "moon_position_m_eci": vector_to_dict(moon_position_m),
        "moon_velocity_mps_eci": vector_to_dict(moon_velocity_mps),
        "sun_vector_from_object_eci": vector_to_dict(sun_vec),
        "moon_vector_from_object_eci": vector_to_dict(moon_vec),
        "earth_vector_from_object_eci": vector_to_dict(earth_vec),
        "nadir_vector_eci": vector_to_dict(earth_vec),
        "velocity_unit_vector_eci": vector_to_dict(velocity_unit),
        "distance_to_sun_m": float(mag(sun_position_m - position_m)),
        "distance_to_moon_m": float(mag(moon_position_m - position_m)),
        "distance_to_earth_center_m": float(mag(position_m)),
        "altitude_over_earth_m": float(altitude_m(position_m)),
        "solar_irradiance_w_m2": float(irradiance),
        "solar_irradiance_without_eclipse_w_m2": float(irradiance_no_eclipse),
        "earth_heliocentric_position_m": vector_to_dict(earth_pos_helio),
        "earth_heliocentric_velocity_mps": vector_to_dict(earth_vel_helio),
        "earth_orbital_true_anomaly_rad": float(theta),
        "earth_orbital_speed_mps": float(EARTH_ORBITAL_SPEED_MPS),
        "earth_orbital_angular_rate_rad_s": float(EARTH_ORBITAL_ANGULAR_RATE_RAD_S),
        "earth_rotation_rate_rad_s": float(EARTH_ROTATION_RATE),
        "moon_orbital_true_anomaly_rad": float(moon_theta),
        "moon_orbital_speed_mps": float(MOON_ORBITAL_SPEED_MPS),
        "moon_orbital_angular_rate_rad_s": float(MOON_ORBITAL_ANGULAR_RATE_RAD_S),
        "external_control_note": "External controller can use sun/nadir/velocity vectors for roll, attitude, antenna, and solar panel optimization. The sim does not choose the control action.",
    }


def build_solar_environment_payload(physical_time_s):
    sun_pos = current_sun_position_eci_m(physical_time_s)
    moon_pos, moon_vel, moon_theta = moon_geocentric_state(physical_time_s)
    earth_pos_helio, earth_vel_helio, theta = earth_heliocentric_state(physical_time_s)
    return {
        "schema": "satellite_simulation.solar_environment.v2",
        "sun_position_m_eci": vector_to_dict(sun_pos),
        "sun_scene_position": vector_to_dict(meters_to_scene(sun_pos)),
        "sun_visual_radius_scene_units": float(SUN_VISUAL_RADIUS_TRUE_SCALE),
        "moon_position_m_eci": vector_to_dict(moon_pos),
        "moon_velocity_mps_eci": vector_to_dict(moon_vel),
        "moon_scene_position": vector_to_dict(meters_to_scene(moon_pos)),
        "moon_visual_radius_scene_units": float(MOON_VISUAL_RADIUS_TRUE_SCALE),
        "moon_distance_from_earth_m": float(mag(moon_pos)),
        "sun_direction_from_earth_unit": vector_to_dict(unit_vector_or_zero(sun_pos)),
        "earth_sun_distance_m": float(AU_M),
        "solar_constant_w_m2_at_1au": float(SOLAR_CONSTANT_W_M2),
        "earth_heliocentric_position_m": vector_to_dict(earth_pos_helio),
        "earth_heliocentric_velocity_mps": vector_to_dict(earth_vel_helio),
        "earth_orbital_speed_mps": float(EARTH_ORBITAL_SPEED_MPS),
        "earth_orbital_angular_rate_rad_s": float(EARTH_ORBITAL_ANGULAR_RATE_RAD_S),
        "earth_orbital_true_anomaly_rad": float(theta),
        "earth_rotation_rate_rad_s": float(EARTH_ROTATION_RATE),
        "moon_orbital_true_anomaly_rad": float(moon_theta),
        "moon_orbital_speed_mps": float(MOON_ORBITAL_SPEED_MPS),
        "moon_orbital_angular_rate_rad_s": float(MOON_ORBITAL_ANGULAR_RATE_RAD_S),
        "model_note": "Earth-centered display frame with true-distance Sun vector, Earth heliocentric orbit state, Moon geocentric orbit state, and Sun/Moon third-body perturbation gravity. Control decisions are external.",
    }



def build_lunar_environment_payload(physical_time_s):
    moon_pos, moon_vel, theta = moon_geocentric_state(physical_time_s)
    earth_pos_helio, earth_vel_helio, _ = earth_heliocentric_state(physical_time_s)
    moon_helio_pos = earth_pos_helio + moon_pos
    moon_helio_vel = earth_vel_helio + moon_vel
    return {
        "schema": "satellite_simulation.lunar_environment.v1",
        "moon_position_m_eci": vector_to_dict(moon_pos),
        "moon_velocity_mps_eci": vector_to_dict(moon_vel),
        "moon_heliocentric_position_m": vector_to_dict(moon_helio_pos),
        "moon_heliocentric_velocity_mps": vector_to_dict(moon_helio_vel),
        "moon_distance_from_earth_m": float(mag(moon_pos)),
        "moon_radius_m": float(R_MOON),
        "moon_mu_m3_s2": float(MU_MOON),
        "moon_orbital_true_anomaly_rad": float(theta),
        "moon_orbital_period_s": float(MOON_ORBITAL_PERIOD_S),
        "moon_orbital_speed_mps": float(MOON_ORBITAL_SPEED_MPS),
        "moon_orbit_inclination_deg": float(MOON_ORBIT_INCLINATION_DEG),
        "lunar_third_body_gravity_enabled": bool(ENABLE_LUNAR_THIRD_BODY_GRAVITY),
    }

# -----------------------------
# Scene objects
# -----------------------------
earth = sphere(pos=vector(0, 0, 0), radius=1, texture=textures.earth, shininess=0.4)
earth_label = label(pos=vector(0, -1.30, 0), text="Earth", height=16, box=False, color=color.white)

inner_radiation_belt_ring = ring(
    pos=earth.pos, axis=vector(0, 0, 1), radius=(R_EARTH + 4_000_000.0) / R_EARTH,
    thickness=0.025, color=color.yellow, opacity=0.28
)
outer_radiation_belt_ring = ring(
    pos=earth.pos, axis=vector(0, 0, 1), radius=(R_EARTH + 22_000_000.0) / R_EARTH,
    thickness=0.035, color=color.orange, opacity=0.22
)
radiation_belt_label = label(pos=vector(0, 4.25, 0), text="Earth radiation belts", height=11, box=False, color=color.yellow)

sun_marker = sphere(
    pos=meters_to_scene(current_sun_position_eci_m(0.0)),
    radius=SUN_VISUAL_RADIUS_TRUE_SCALE,
    color=color.yellow,
    emissive=True,
    opacity=0.92,
)
sun_label = label(
    pos=sun_marker.pos + vector(0, -SUN_VISUAL_RADIUS_TRUE_SCALE * 1.15, 0),
    text=f"Sun | true distance 1 AU | true-size scaled radius {SUN_VISUAL_RADIUS_TRUE_SCALE:.1f} Earth radii",
    height=16,
    box=False,
    color=color.yellow,
)

moon_marker = sphere(
    pos=meters_to_scene(current_moon_position_eci_m(0.0)),
    radius=max(0.08, MOON_VISUAL_RADIUS_TRUE_SCALE * 3.0),
    color=color.white,
    emissive=False,
    opacity=0.95,
    make_trail=True,
    trail_color=color.gray(0.65),
    retain=MOON_TRAIL_RETAIN,
)
moon_marker.trail_radius = 0.004
moon_label = label(
    pos=moon_marker.pos + vector(0.16, 0.16, 0),
    text="Moon | orbiting Earth | gravity included",
    height=12,
    box=False,
    color=color.white,
)
moon_orbit_curve = curve(color=color.gray(0.38), radius=0.004)
for i in range(721):
    theta = 2.0 * pi * i / 720.0 + radians(MOON_INITIAL_PHASE_DEG)
    p = vector(MOON_SEMI_MAJOR_AXIS_M * cos(theta), MOON_SEMI_MAJOR_AXIS_M * sin(theta), 0)
    p = rotate(p, angle=radians(MOON_ORBIT_INCLINATION_DEG), axis=vector(1, 0, 0))
    moon_orbit_curve.append(pos=meters_to_scene(p))

warning_label = label(pos=vector(0, 2.0, 0), text="", height=16, box=False, color=color.red)
timer_label = label(pos=vector(-3.6, 3.0, 0), text="Visual Time: 0.0 s | Physical Time: 0 s", height=12, box=False, color=color.white)
physics_label = label(pos=vector(-3.6, 2.78, 0), text="Physics: Earth J2 + drag + Sun/Moon third-body gravity, sensor fusion telemetry", height=10, box=False, color=color.cyan)
command_label = label(pos=vector(-3.6, 2.58, 0), text="Command input: waiting for quantum_commands.json", height=10, box=False, color=color.green)
telemetry_label = label(pos=vector(-3.6, 2.40, 0), text="Telemetry outbox: ready_to_send_telemetry, 10 Hz | speed 1x", height=10, box=False, color=color.white)
selected_label = label(pos=vector(0, 2.35, 0), text="Selected data satellite: none", height=12, box=False, color=color.yellow)
summary_label = label(pos=vector(0, -2.35, 0), text="", height=12, box=True, border=8, opacity=0.18, color=color.white)

# -----------------------------
# Cursor-centered zoom + views
# -----------------------------
CUSTOM_CURSOR_ZOOM_ENABLED = True
ZOOM_IN_FACTOR = 0.82
ZOOM_OUT_FACTOR = 1.22
MIN_SCENE_RANGE = 0.20
MAX_SCENE_RANGE = 60000.0


def get_cursor_focus_point():
    try:
        picked = scene.mouse.pick
        if picked is not None and hasattr(picked, "pos"):
            return picked.pos
    except Exception:
        pass
    try:
        projected = scene.mouse.project(normal=scene.forward, point=scene.center)
        if projected is not None:
            return projected
    except Exception:
        pass
    return scene.center


def wheel_event_means_zoom_in(event):
    for attr in ["deltaY", "dy"]:
        try:
            value = getattr(event, attr)
            if value is not None and value != 0:
                return value < 0
        except Exception:
            pass
    for attr in ["wheelDelta", "delta"]:
        try:
            value = getattr(event, attr)
            if value is not None and value != 0:
                return value > 0
        except Exception:
            pass
    return True


def zoom_to_cursor(event=None):
    if not CUSTOM_CURSOR_ZOOM_ENABLED:
        return
    focus_point = get_cursor_focus_point()
    zoom_factor = ZOOM_IN_FACTOR if wheel_event_means_zoom_in(event) else ZOOM_OUT_FACTOR
    old_center = scene.center
    new_range = clamp_value(scene.range * zoom_factor, MIN_SCENE_RANGE, MAX_SCENE_RANGE)
    effective_factor = new_range / scene.range if scene.range != 0 else zoom_factor
    scene.center = focus_point + (old_center - focus_point) * effective_factor
    scene.range = new_range


for wheel_event_name in ["wheel", "scroll"]:
    try:
        scene.bind(wheel_event_name, zoom_to_cursor)
    except Exception:
        pass


def focus_earth_view(button_event=None):
    scene.center = earth.pos
    scene.range = 5.0


def focus_sun_view(button_event=None):
    scene.center = sun_marker.pos
    scene.range = SUN_VISUAL_RADIUS_TRUE_SCALE * 2.8


def focus_earth_sun_view(button_event=None):
    scene.center = (earth.pos + sun_marker.pos) / 2
    scene.range = max(10.0, mag(sun_marker.pos - earth.pos) * 0.62)


def focus_moon_view(button_event=None):
    scene.center = moon_marker.pos
    scene.range = 2.2


def focus_earth_moon_view(button_event=None):
    scene.center = (earth.pos + moon_marker.pos) / 2
    scene.range = max(5.0, mag(moon_marker.pos - earth.pos) * 0.68)


def focus_default_view(button_event=None):
    focus_earth_view(button_event)

# -----------------------------
# Controls
# -----------------------------
simulation_running = True
simulation_ended = False
final_summary_printed = False
simulation_physical_time = 0.0
frame_count = 0


def effective_telemetry_sample_hz():
    return BASE_TELEMETRY_SAMPLE_HZ * simulation_speed_multiplier


def telemetry_export_interval_frames():
    return max(1, int(round(rate_value / effective_telemetry_sample_hz())))


def set_control_status(text, label_color=color.white):
    telemetry_label.text = text
    telemetry_label.color = label_color


def speed_status_text():
    return f"speed {simulation_speed_multiplier}x | telemetry {effective_telemetry_sample_hz():.1f} Hz | dt {dt:.1f} s"


def refresh_running_status():
    if simulation_running and not simulation_ended:
        set_control_status(f"Simulation running | {speed_status_text()}", color.green)


def set_simulation_speed(multiplier):
    global simulation_speed_multiplier, dt
    simulation_speed_multiplier = multiplier
    dt = BASE_DT * simulation_speed_multiplier
    print(f"Fast forward set to {simulation_speed_multiplier}x | telemetry {effective_telemetry_sample_hz():.1f} Hz | dt {dt:.1f} s")
    refresh_running_status()


def fast_forward_simulation(button_event=None):
    global simulation_speed_index
    if simulation_ended:
        set_control_status("Simulation ended. Restart the Python file to run again.", color.red)
        return
    simulation_speed_index = (simulation_speed_index + 1) % len(SIMULATION_SPEED_OPTIONS)
    set_simulation_speed(SIMULATION_SPEED_OPTIONS[simulation_speed_index])


def start_simulation(button_event=None):
    global simulation_running
    if simulation_ended:
        set_control_status("Simulation ended. Restart the Python file to run again.", color.red)
        return
    simulation_running = True
    set_control_status(f"Simulation running | {speed_status_text()}", color.green)


def stop_simulation(button_event=None):
    global simulation_running
    if simulation_ended:
        return
    simulation_running = False
    set_control_status("Simulation paused | physics and telemetry stopped", color.yellow)


def count_active_debris():
    return sum(1 for d in debris_particles if d["active"])


def count_active_visual_events():
    return sum(1 for ev in active_visual_events if len(ev) > 0)


def build_final_summary(frame_count_value, visual_time_value, physical_time_value):
    active_satellite_names = [s["name"] for s in satellites if s["active"]]
    destroyed_satellite_names = [s["name"] for s in satellites if not s["active"]]
    active_asteroid_count = sum(1 for a in asteroids if a["active"])
    active_debris_count = count_active_debris()
    return {
        "schema": "satellite_simulation.final_summary.v2.earth_sun_only",
        "ended_at_unix_time_s": time.time(),
        "ended_at_utc_iso": datetime.now(timezone.utc).isoformat(),
        "frame": int(frame_count_value),
        "visual_time_s": float(visual_time_value),
        "physical_time_s": float(physical_time_value),
        "satellite_summary": {
            "starting_satellites": len(satellites),
            "satellites_left": len(active_satellite_names),
            "active_satellites": active_satellite_names,
            "destroyed_satellites": destroyed_satellite_names,
        },
        "hazard_summary": {
            "active_asteroids": active_asteroid_count,
            "active_debris": active_debris_count,
            "total_active_hazards": active_asteroid_count + active_debris_count,
            "active_visual_impact_events": count_active_visual_events(),
        },
        "system_summary": {
            "mars_removed": True,
            "sun_true_distance_included": True,
            "base_telemetry_sample_hz": BASE_TELEMETRY_SAMPLE_HZ,
            "effective_telemetry_sample_hz": effective_telemetry_sample_hz(),
            "speed_multiplier": simulation_speed_multiplier,
            "physics_timestep_s": dt,
            "earth_mu_m3_s2": MU_EARTH,
            "earth_radius_m": R_EARTH,
            "earth_j2": J2_EARTH,
            "earth_sun_distance_m": AU_M,
            "moon_distance_from_earth_m": MOON_SEMI_MAJOR_AXIS_M,
            "moon_gravity_included": ENABLE_LUNAR_THIRD_BODY_GRAVITY,
            "sun_third_body_gravity_included": ENABLE_SOLAR_THIRD_BODY_GRAVITY,
        },
    }


def format_final_summary_for_screen(summary):
    sats_left = summary["satellite_summary"]["active_satellites"]
    sats_destroyed = summary["satellite_summary"]["destroyed_satellites"]
    sats_left_text = ", ".join(sats_left) if sats_left else "none"
    sats_destroyed_text = ", ".join(sats_destroyed) if sats_destroyed else "none"
    return (
        "SIMULATION ENDED\n"
        f"Visual time: {summary['visual_time_s']:.1f} s | Physical time: {summary['physical_time_s']:.0f} s\n"
        f"Satellites left: {summary['satellite_summary']['satellites_left']} / {summary['satellite_summary']['starting_satellites']} ({sats_left_text})\n"
        f"Destroyed satellites: {sats_destroyed_text}\n"
        f"Active asteroid hazards: {summary['hazard_summary']['active_asteroids']}\n"
        f"Active debris hazards: {summary['hazard_summary']['active_debris']}\n"
        f"Total active hazards: {summary['hazard_summary']['total_active_hazards']}"
    )


def end_simulation(button_event=None):
    global simulation_running, simulation_ended, final_summary_printed
    if simulation_ended:
        return
    simulation_running = False
    simulation_ended = True
    current_visual_time = frame_count / rate_value
    summary = build_final_summary(frame_count, current_visual_time, simulation_physical_time)
    summary_label.text = format_final_summary_for_screen(summary)
    warning_label.text = "SIMULATION ENDED - FINAL SUMMARY GENERATED"
    set_control_status("Simulation ended | final summary printed in terminal", color.red)
    print("\n========== FINAL SIMULATION SUMMARY ==========")
    print(json.dumps(summary, indent=2))
    print("=============================================\n")
    final_summary_printed = True


scene.append_to_caption("\n\nSimulation controls: ")
button(text="Start", bind=start_simulation)
scene.append_to_caption("  ")
button(text="Stop", bind=stop_simulation)
scene.append_to_caption("  ")
button(text="Fast Forward", bind=fast_forward_simulation)
scene.append_to_caption("  ")
button(text="End + Summary", bind=end_simulation)
scene.append_to_caption("\nView controls: ")
button(text="Earth View", bind=focus_earth_view)
scene.append_to_caption("  ")
button(text="Sun View", bind=focus_sun_view)
scene.append_to_caption("  ")
button(text="Earth + Sun View", bind=focus_earth_sun_view)
scene.append_to_caption("  ")
button(text="Moon View", bind=focus_moon_view)
scene.append_to_caption("  ")
button(text="Earth + Moon View", bind=focus_earth_moon_view)
scene.append_to_caption("  ")
button(text="Default View", bind=focus_default_view)
scene.append_to_caption("\nMars, Mars satellites, Mars asteroids, and spacecraft transfer mission are removed. Moon orbit and Moon gravity are included. Scroll over an object to zoom toward your cursor.\n")

# -----------------------------
# Gravity / drag physics
# -----------------------------
def get_gravity_acc_j2(rel_pos, mu, radius, j2_coeff):
    r = mag(rel_pos)
    if r == 0:
        return vector(0, 0, 0)
    acc_point = -mu * rel_pos / (r ** 3)
    if not ENABLE_J2_PERTURBATION or j2_coeff == 0:
        return acc_point
    z = rel_pos.z
    r2 = r ** 2
    factor = (1.5 * j2_coeff * mu * radius ** 2) / (r ** 5)
    j2_acc = vector(
        rel_pos.x * (5 * (z ** 2 / r2) - 1),
        rel_pos.y * (5 * (z ** 2 / r2) - 1),
        rel_pos.z * (5 * (z ** 2 / r2) - 3),
    )
    return acc_point + factor * j2_acc


def third_body_perturbation_acc(object_position_m, third_body_position_m, third_body_mu):
    object_to_body = third_body_position_m - object_position_m
    earth_to_body = third_body_position_m
    if mag(object_to_body) == 0 or mag(earth_to_body) == 0:
        return vector(0, 0, 0)
    return third_body_mu * (object_to_body / mag(object_to_body) ** 3 - earth_to_body / mag(earth_to_body) ** 3)


def get_drag_acc(rel_pos, rel_vel, mass_kg, area_m2):
    if not ENABLE_ATMOSPHERIC_DRAG:
        return vector(0, 0, 0)
    if mass_kg is None or mass_kg <= 0 or area_m2 is None or area_m2 <= 0:
        return vector(0, 0, 0)
    alt = mag(rel_pos) - R_EARTH
    if alt < 0 or alt > MAX_DRAG_ALTITUDE_M:
        return vector(0, 0, 0)
    rho = EARTH_SURFACE_DENSITY_KG_M3 * math.exp(-alt / EARTH_SCALE_HEIGHT_M)
    v_mag = mag(rel_vel)
    if v_mag == 0 or rho <= 0:
        return vector(0, 0, 0)
    drag_mag = (0.5 * rho * (v_mag ** 2) * DRAG_COEFFICIENT * area_m2) / mass_kg
    return -drag_mag * norm(rel_vel)


def physics_acceleration_for_object(position_m, velocity_mps, mass_kg=None, area_m2=None):
    rel_pos = position_m
    gravity = get_gravity_acc_j2(rel_pos, MU_EARTH, R_EARTH, J2_EARTH)
    if ENABLE_SOLAR_THIRD_BODY_GRAVITY:
        gravity += third_body_perturbation_acc(rel_pos, current_sun_position_eci_m(simulation_physical_time), MU_SUN)
    if ENABLE_LUNAR_THIRD_BODY_GRAVITY:
        gravity += third_body_perturbation_acc(rel_pos, current_moon_position_eci_m(simulation_physical_time), MU_MOON)
    atmosphere_velocity_mps = cross(vector(0, 0, EARTH_ROTATION_RATE), rel_pos)
    rel_atmosphere_velocity_mps = velocity_mps - atmosphere_velocity_mps
    drag = get_drag_acc(rel_pos, rel_atmosphere_velocity_mps, mass_kg, area_m2)
    return gravity + drag

# -----------------------------
# Orbit helpers
# -----------------------------
def make_orbit_state(altitude, inclination_deg=0, raan_deg=0, phase_deg=0, prograde=True):
    radius_m = R_EARTH + altitude
    pos = vector(radius_m, 0, 0)
    vel = vector(0, circular_speed(radius_m), 0)
    if not prograde:
        vel = -vel
    pos = rotate(pos, angle=radians(phase_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(phase_deg), axis=vector(0, 0, 1))
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))
    return pos, vel


def draw_circular_orbit(altitude, orbit_color, inclination_deg=0, raan_deg=0):
    radius_m = R_EARTH + altitude
    points = []
    for i in range(721):
        theta = 2 * pi * i / 720
        p = vector(radius_m * cos(theta), radius_m * sin(theta), 0)
        p = rotate(p, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        p = rotate(p, angle=radians(raan_deg), axis=vector(0, 0, 1))
        points.append(meters_to_scene(p))
    return curve(pos=points, color=orbit_color, radius=0.006)


def make_elliptical_orbit_state(perigee_altitude, apogee_altitude, inclination_deg=63.4, raan_deg=0, argument_of_perigee_deg=270, true_anomaly_deg=0, prograde=True):
    rp = R_EARTH + perigee_altitude
    ra = R_EARTH + apogee_altitude
    a = 0.5 * (rp + ra)
    e = (ra - rp) / (ra + rp)
    p_orbit = a * (1 - e ** 2)
    nu = radians(true_anomaly_deg)
    r = p_orbit / (1 + e * cos(nu))
    pos = vector(r * cos(nu), r * sin(nu), 0)
    velocity_factor = sqrt(MU_EARTH / p_orbit)
    vel = vector(-sin(nu), e + cos(nu), 0) * velocity_factor
    if not prograde:
        vel = -vel
    pos = rotate(pos, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))
    return pos, vel


def draw_elliptical_orbit(perigee_altitude, apogee_altitude, orbit_color, inclination_deg=63.4, raan_deg=0, argument_of_perigee_deg=270):
    rp = R_EARTH + perigee_altitude
    ra = R_EARTH + apogee_altitude
    a = 0.5 * (rp + ra)
    e = (ra - rp) / (ra + rp)
    p_orbit = a * (1 - e ** 2)
    points = []
    for i in range(721):
        nu = 2 * pi * i / 720
        r = p_orbit / (1 + e * cos(nu))
        pos = vector(r * cos(nu), r * sin(nu), 0)
        pos = rotate(pos, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
        pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
        points.append(meters_to_scene(pos))
    return curve(pos=points, color=orbit_color, radius=0.006)

# -----------------------------
# Commands
# -----------------------------
last_command_mtime = None
last_command_text = ""
target_marker = None
target_label = None


def create_sample_command_file_if_missing():
    if os.path.exists(COMMAND_FILE):
        return
    sample = {
        "selected_satellite": "SAT-3",
        "data_collection_target": {"name": "Nashville target", "lat_deg": 36.1627, "lon_deg": -86.7816},
        "maneuvers": [{"satellite": "SAT-2", "type": "radial_out", "delta_v_mps": 0}],
        "attitude_commands": [
            {"satellite": "SAT-2", "roll_deg": 0, "pitch_deg": 0, "yaw_deg": 0, "panel_rotation_deg": 0}
        ],
    }
    try:
        with open(COMMAND_FILE, "w") as f:
            json.dump(sample, f, indent=2)
    except Exception:
        pass


def lat_lon_to_position(lat_deg, lon_deg, physical_time_s=0.0, include_earth_rotation=True):
    lat = radians(lat_deg)
    lon = radians(lon_deg)
    if include_earth_rotation:
        lon += EARTH_ROTATION_RATE * physical_time_s
    return vector(cos(lat) * cos(lon), cos(lat) * sin(lon), sin(lat))


def target_surface_position_m(target_data, physical_time_s):
    if target_data is None:
        return None
    lat_deg = float(target_data.get("lat_deg", 0.0))
    lon_deg = float(target_data.get("lon_deg", 0.0))
    return lat_lon_to_position(lat_deg, lon_deg, physical_time_s) * R_EARTH


def refresh_data_target_marker(physical_time_s):
    if active_data_target is None or target_marker is None:
        return
    surface_pos_scene = meters_to_scene(target_surface_position_m(active_data_target, physical_time_s)) * 1.025
    target_marker.pos = surface_pos_scene
    target_label.pos = surface_pos_scene + vector(0.05, 0.05, 0)


def update_data_target_marker(target_data):
    global target_marker, target_label, active_data_target
    if target_data is None:
        return
    try:
        name = target_data.get("name", "Data target")
        lat_deg = float(target_data.get("lat_deg", 0))
        lon_deg = float(target_data.get("lon_deg", 0))
    except Exception:
        return
    active_data_target = {"name": name, "lat_deg": lat_deg, "lon_deg": lon_deg}
    surface_pos = meters_to_scene(target_surface_position_m(active_data_target, simulation_physical_time)) * 1.025
    if target_marker is None:
        target_marker = sphere(pos=surface_pos, radius=0.035, color=color.yellow, emissive=True)
        target_label = label(pos=surface_pos + vector(0.05, 0.05, 0), text=name, height=10, box=False, color=color.yellow)
    else:
        target_marker.pos = surface_pos
        target_label.pos = surface_pos + vector(0.05, 0.05, 0)
        target_label.text = name


def set_satellite_highlight(sat, highlighted):
    if not sat["active"]:
        return
    if highlighted:
        sat["marker"].color = color.yellow
        sat["body"].color = color.yellow
    else:
        sat["marker"].color = sat["base_color"]
        sat["body"].color = color.white


def apply_delta_v(sat, maneuver_type, delta_v_mps):
    if not sat["active"]:
        return
    position_m = sat["position_m"]
    velocity_mps = sat["velocity_mps"]
    radial_dir = norm(position_m)
    tangential_dir = perpendicular_velocity_dir(position_m, velocity_mps)
    plane_dir = cross(radial_dir, tangential_dir)
    plane_dir = norm(plane_dir) if mag(plane_dir) > 0 else vector(0, 0, 1)
    if maneuver_type == "boost_prograde":
        dv = tangential_dir * delta_v_mps
    elif maneuver_type == "boost_retrograde":
        dv = -tangential_dir * delta_v_mps
    elif maneuver_type == "radial_out":
        dv = radial_dir * delta_v_mps
    elif maneuver_type == "radial_in":
        dv = -radial_dir * delta_v_mps
    elif maneuver_type == "plane_up":
        dv = plane_dir * delta_v_mps
    elif maneuver_type == "plane_down":
        dv = -plane_dir * delta_v_mps
    else:
        return
    sat["velocity_mps"] += dv


def ensure_satellite_state_defaults(sat):
    sid = sat["name"]
    satellite_power_state.setdefault(sid, {"battery_percent": 82.0 + random() * 12.0})
    satellite_thermal_state.setdefault(sid, {
        "bus_temperature_c": 22.0 + random() * 3.0,
        "battery_temperature_c": 20.0 + random() * 2.0,
        "processor_temperature_c": 34.0 + random() * 4.0,
        "power_amp_temperature_c": 40.0 + random() * 5.0,
    })
    satellite_attitude_state.setdefault(sid, {
        "roll_deg": 0.0,
        "pitch_deg": 0.0,
        "yaw_deg": (random() * 360.0),
        "panel_rotation_deg": 0.0,
        "angular_rate_dps": {"x": 0.0, "y": 0.0, "z": 0.0},
        "attitude_control_source": SAT_ATTITUDE_EXTERNAL_CONTROL_DEFAULT,
    })


def apply_attitude_command(sat, command):
    ensure_satellite_state_defaults(sat)
    state = satellite_attitude_state[sat["name"]]
    for key in ["roll_deg", "pitch_deg", "yaw_deg", "panel_rotation_deg"]:
        if key in command:
            state[key] = float(command[key])
    state["attitude_control_source"] = "external_command"
    state["last_command_utc_iso"] = datetime.now(timezone.utc).isoformat()


def apply_command_data(command_data, satellites):
    selected_name = command_data.get("selected_satellite", None)
    for sat in satellites:
        sat["selected_for_data"] = (sat["name"] == selected_name)
        set_satellite_highlight(sat, sat["selected_for_data"])
    selected_label.text = "Selected data satellite: none" if selected_name is None else f"Selected data satellite: {selected_name}"
    update_data_target_marker(command_data.get("data_collection_target", None))

    for maneuver in command_data.get("maneuvers", []):
        sat_name = maneuver.get("satellite", "")
        maneuver_type = maneuver.get("type", "")
        delta_v_mps = float(maneuver.get("delta_v_mps", 0))
        if abs(delta_v_mps) <= 0:
            continue
        for sat in satellites:
            if sat["name"] == sat_name:
                apply_delta_v(sat, maneuver_type, delta_v_mps)
                warning_label.text = f"External command: {sat_name} {maneuver_type} delta-v={delta_v_mps:.1f} m/s"
                warning_label.pos = sat["marker"].pos + vector(0, 0.5, 0)

    for attitude_cmd in command_data.get("attitude_commands", []):
        sat_name = attitude_cmd.get("satellite", "")
        for sat in satellites:
            if sat["name"] == sat_name and sat.get("active", False):
                apply_attitude_command(sat, attitude_cmd)
                warning_label.text = f"External attitude command applied to {sat_name}"
                warning_label.pos = sat["marker"].pos + vector(0, 0.5, 0)


def check_for_command_update(satellites):
    global last_command_mtime, last_command_text
    create_sample_command_file_if_missing()
    if not os.path.exists(COMMAND_FILE):
        command_label.text = "Command input: no command file found"
        return
    try:
        mtime = os.path.getmtime(COMMAND_FILE)
        if last_command_mtime is not None and mtime == last_command_mtime:
            return
        with open(COMMAND_FILE, "r") as f:
            text = f.read()
        if text.strip() == "" or text == last_command_text:
            last_command_mtime = mtime
            return
        data = json.loads(text)
        apply_command_data(data, satellites)
        last_command_mtime = mtime
        last_command_text = text
        command_label.text = "Command input: live update applied"
    except Exception as e:
        command_label.text = f"Command input: JSON error or unreadable file ({e})"

# -----------------------------
# Satellite / asteroid creation
# -----------------------------
def create_satellite_from_state(name, position_m, velocity_mps, sat_color, trail_color, orbit_class="custom", orbit_description="custom orbit"):
    marker = sphere(pos=meters_to_scene(position_m), radius=0.045, color=sat_color, make_trail=True, trail_color=trail_color, retain=1800)
    marker.trail_radius = 0.006
    body = box(pos=marker.pos, length=0.13, height=0.055, width=0.055, color=color.white)
    panel_left = box(pos=marker.pos + vector(0, 0.09, 0), length=0.22, height=0.018, width=0.006, color=vector(0.03, 0.10, 0.40))
    panel_right = box(pos=marker.pos + vector(0, -0.09, 0), length=0.22, height=0.018, width=0.006, color=vector(0.03, 0.10, 0.40))
    sat_label = label(pos=marker.pos + vector(0.13, 0.13, 0), text=name, height=11, box=False, color=sat_color)
    sat = {
        "name": name,
        "position_m": position_m,
        "velocity_mps": velocity_mps,
        "marker": marker,
        "body": body,
        "panel_left": panel_left,
        "panel_right": panel_right,
        "label": sat_label,
        "active": True,
        "base_color": sat_color,
        "selected_for_data": False,
        "mass_kg": 500.0,
        "physical_radius_m": 1.5,
        "orbit_class": orbit_class,
        "orbit_description": orbit_description,
        "central_body": "Earth",
        "shielding_mm_al": DEFAULT_SATELLITE_SHIELDING_MM_AL,
    }
    ensure_satellite_state_defaults(sat)
    return sat


def create_satellite(name, altitude, inclination_deg, raan_deg, phase_deg, sat_color, trail_color, prograde=True, orbit_class="circular", orbit_description=None):
    pos, vel = make_orbit_state(altitude, inclination_deg, raan_deg, phase_deg, prograde)
    if orbit_description is None:
        orbit_description = f"circular orbit, altitude {altitude / 1000.0:.0f} km"
    return create_satellite_from_state(name, pos, vel, sat_color, trail_color, orbit_class, orbit_description)


def create_heo_satellite(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, sat_color, trail_color):
    pos, vel = make_elliptical_orbit_state(perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg)
    return create_satellite_from_state(
        name, pos, vel, sat_color, trail_color, "HEO",
        f"highly elliptical orbit, perigee {perigee_altitude / 1000.0:.0f} km, apogee {apogee_altitude / 1000.0:.0f} km"
    )


def hide_satellite(sat):
    sat["active"] = False
    sat["marker"].make_trail = False
    sat["marker"].clear_trail()
    for key in ["marker", "body", "panel_left", "panel_right", "label"]:
        sat[key].visible = False


def update_satellite_physics(sat):
    if not sat["active"]:
        return
    area_m2 = object_cross_section_area_m2(sat, 1.5)
    sat["velocity_mps"] += physics_acceleration_for_object(sat["position_m"], sat["velocity_mps"], sat.get("mass_kg", 500.0), area_m2) * dt
    sat["position_m"] += sat["velocity_mps"] * dt
    if mag(sat["position_m"]) <= R_EARTH:
        hide_satellite(sat)


def update_satellite_visuals(sat):
    if not sat["active"]:
        return
    p = meters_to_scene(sat["position_m"])
    sat["marker"].pos = p
    sat["body"].pos = p
    sat["panel_left"].pos = p + vector(0, 0.09, 0)
    sat["panel_right"].pos = p + vector(0, -0.09, 0)
    sat["label"].pos = p + vector(0.13, 0.13, 0)
    sat["body"].rotate(angle=0.01, axis=vector(0, 1, 0), origin=p)
    sat["panel_left"].rotate(angle=0.01, axis=vector(0, 1, 0), origin=p)
    sat["panel_right"].rotate(angle=0.01, axis=vector(0, 1, 0), origin=p)


def create_asteroid_from_state(name, position_m, velocity_mps, orbit_class, orbit_description, asteroid_color=color.red):
    marker = sphere(pos=meters_to_scene(position_m), radius=0.075, color=asteroid_color, emissive=True, make_trail=True, trail_color=asteroid_color, retain=600)
    marker.trail_radius = 0.006
    asteroid_label = label(pos=marker.pos + vector(0.14, 0.14, 0), text=name, height=11, box=False, color=asteroid_color)
    return {
        "name": name,
        "position_m": position_m,
        "velocity_mps": velocity_mps,
        "marker": marker,
        "label": asteroid_label,
        "active": True,
        "mass_kg": 1000.0,
        "physical_radius_m": 2.0,
        "orbit_class": orbit_class,
        "orbit_description": orbit_description,
        "central_body": "Earth",
        "shielding_mm_al": DEFAULT_ASTEROID_SHIELDING_MM_AL,
    }


def create_physical_asteroid(target_sat, target_collision_visual_time, name="AST-1"):
    target_position_m = target_sat["position_m"]
    target_velocity_mps = target_sat["velocity_mps"]
    target_radius_m = mag(target_position_m)
    target_speed_mps = circular_speed(target_radius_m)
    omega_rad_per_s = target_speed_mps / target_radius_m
    target_physical_time = target_collision_visual_time * rate_value * BASE_DT
    separation_angle = (2.0 * omega_rad_per_s * target_physical_time) % (2.0 * pi)
    orbit_normal = cross(target_position_m, target_velocity_mps)
    orbit_normal = norm(orbit_normal) if mag(orbit_normal) > 0 else vector(0, 0, 1)
    asteroid_position_m = rotate(target_position_m, angle=separation_angle, axis=orbit_normal)
    prograde_dir = cross(orbit_normal, norm(asteroid_position_m))
    prograde_dir = norm(prograde_dir) if mag(prograde_dir) > 0 else perpendicular_velocity_dir(asteroid_position_m, target_velocity_mps)
    asteroid_velocity_mps = -prograde_dir * target_speed_mps
    asteroid = create_asteroid_from_state(
        name, asteroid_position_m, asteroid_velocity_mps, target_sat.get("orbit_class", "LEO"),
        f"guaranteed retrograde collision-course orbit targeting {target_sat['name']}; predicted impact in about {target_collision_visual_time:.1f} visual seconds",
        color.red,
    )
    asteroid["target_satellite"] = target_sat["name"]
    asteroid["predicted_collision_visual_time_s"] = target_collision_visual_time
    asteroid["predicted_collision_physical_time_s"] = target_physical_time
    asteroid["guaranteed_collision_course"] = True
    print(f"{name} placed on guaranteed collision course with {target_sat['name']} in about {target_collision_visual_time:.1f} visual seconds.")
    return asteroid


def create_circular_asteroid(name, altitude, inclination_deg, raan_deg, phase_deg, orbit_class, prograde=False):
    pos, vel = make_orbit_state(altitude, inclination_deg, raan_deg, phase_deg, prograde)
    return create_asteroid_from_state(name, pos, vel, orbit_class, f"{orbit_class} circular asteroid orbit, altitude {altitude / 1000.0:.0f} km", color.red)


def create_heo_asteroid(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, prograde=False):
    pos, vel = make_elliptical_orbit_state(perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, prograde)
    return create_asteroid_from_state(
        name, pos, vel, "HEO",
        f"HEO elliptical asteroid orbit, perigee {perigee_altitude / 1000.0:.0f} km, apogee {apogee_altitude / 1000.0:.0f} km",
        color.red,
    )


def hide_object(obj):
    obj["active"] = False
    obj["marker"].visible = False
    if obj.get("label") is not None:
        obj["label"].visible = False


def update_asteroid(ast):
    if not ast["active"]:
        return
    area_m2 = object_cross_section_area_m2(ast, 2.0)
    ast["velocity_mps"] += physics_acceleration_for_object(ast["position_m"], ast["velocity_mps"], ast.get("mass_kg", 1000.0), area_m2) * dt
    ast["position_m"] += ast["velocity_mps"] * dt
    if mag(ast["position_m"]) <= R_EARTH:
        hide_object(ast)
        return
    p = meters_to_scene(ast["position_m"])
    ast["marker"].pos = p
    ast["label"].pos = p + vector(0.14, 0.14, 0)

# -----------------------------
# Debris and collision visuals
# -----------------------------
INITIAL_BREAKUP_FRAGMENTS = 45
SECONDARY_BREAKUP_MIN_FRAGMENTS = 2
SECONDARY_BREAKUP_MAX_FRAGMENTS = 5
MAX_DEBRIS_PARTICLES = 180
CATASTROPHIC_ENERGY_J_PER_KG = 40000.0
debris_debris_collision_distance_m = 90000.0
MAX_DEBRIS_DEBRIS_COLLISIONS_PER_FRAME = 2


def random_debris_mass_kg():
    return 0.25 + random() * 8.0


def random_debris_radius_scene(mass_kg):
    return 0.006 + min(0.010, 0.0015 * sqrt(mass_kg))


def create_breakup_event(position_m, base_velocity_mps):
    visual_parts = []
    debris_parts = []
    scene_pos = meters_to_scene(position_m)
    flash = sphere(pos=scene_pos, radius=0.05, color=color.white, emissive=True, opacity=0.95)
    visual_parts.append({"obj": flash, "growth": 0.012, "shrink_factor": 0.93, "life": 18, "max_life": 18, "grow_for": 5, "start_opacity": 0.95})
    glow = sphere(pos=scene_pos, radius=0.08, color=color.orange, emissive=True, opacity=0.35)
    visual_parts.append({"obj": glow, "growth": 0.010, "shrink_factor": 0.94, "life": 26, "max_life": 26, "grow_for": 7, "start_opacity": 0.35})
    for i in range(INITIAL_BREAKUP_FRAGMENTS):
        direction = random_unit_vector()
        start_position_m = position_m + direction * (3000.0 + random() * 18000.0)
        fragment_velocity_mps = base_velocity_mps + direction * (20.0 + random() * 130.0)
        frag_color = color.gray(0.8)
        if i % 6 == 0:
            frag_color = color.orange
        elif i % 6 == 1:
            frag_color = color.yellow
        elif i % 6 == 2:
            frag_color = color.red
        elif i % 6 == 3:
            frag_color = color.white
        mass_kg = random_debris_mass_kg()
        marker = sphere(pos=meters_to_scene(start_position_m), radius=random_debris_radius_scene(mass_kg), color=frag_color, opacity=0.88, make_trail=False)
        debris_parts.append({
            "marker": marker,
            "position_m": start_position_m,
            "velocity_mps": fragment_velocity_mps,
            "active": True,
            "age": 0,
            "life": 12000,
            "can_collide_after": 8,
            "mass_kg": mass_kg,
            "physical_radius_m": 0.03 + min(0.20, 0.025 * sqrt(mass_kg)),
            "central_body": "Earth",
            "shielding_mm_al": DEFAULT_DEBRIS_SHIELDING_MM_AL,
            "recent_collision_cooldown": 0,
        })
    return visual_parts, debris_parts


def update_visual_event(visual_parts):
    alive = []
    for part in visual_parts:
        obj = part["obj"]
        age = part["max_life"] - part["life"]
        if age < part["grow_for"]:
            obj.radius += part["growth"]
        else:
            obj.radius *= part["shrink_factor"]
        fade = max(0, part["life"] / part["max_life"])
        obj.opacity = part["start_opacity"] * fade
        part["life"] -= 1
        if part["life"] > 0 and obj.radius > 0.003:
            alive.append(part)
        else:
            obj.visible = False
    return alive


def hide_debris(debris):
    debris["active"] = False
    debris["marker"].visible = False


def update_debris_particle(debris):
    if not debris["active"]:
        return
    area_m2 = object_cross_section_area_m2(debris, 0.08)
    debris["velocity_mps"] += physics_acceleration_for_object(debris["position_m"], debris["velocity_mps"], debris.get("mass_kg", 1.0), area_m2) * dt
    debris["position_m"] += debris["velocity_mps"] * dt
    debris["marker"].pos = meters_to_scene(debris["position_m"])
    debris["age"] += 1
    debris["life"] -= 1
    if debris.get("recent_collision_cooldown", 0) > 0:
        debris["recent_collision_cooldown"] -= 1
    if debris["life"] < 1500:
        debris["marker"].opacity = max(0, debris["life"] / 1500)
    if debris["life"] <= 0 or mag(debris["position_m"]) <= R_EARTH:
        hide_debris(debris)


def create_secondary_debris(position_m, center_velocity_mps, relative_speed_mps, parent_mass_kg):
    new_parts = []
    if len(debris_particles) >= MAX_DEBRIS_PARTICLES:
        return new_parts
    count = int(SECONDARY_BREAKUP_MIN_FRAGMENTS + random() * (SECONDARY_BREAKUP_MAX_FRAGMENTS - SECONDARY_BREAKUP_MIN_FRAGMENTS + 1))
    count = min(count, MAX_DEBRIS_PARTICLES - len(debris_particles))
    for i in range(count):
        direction = random_unit_vector()
        eject_speed = min(250.0, 15.0 + random() * max(20.0, 0.025 * relative_speed_mps))
        start_position_m = position_m + direction * (1000.0 + random() * 6000.0)
        fragment_velocity_mps = center_velocity_mps + direction * eject_speed
        mass_kg = max(0.05, parent_mass_kg * (0.08 + random() * 0.18))
        frag_color = color.orange if i == 0 else color.white if i == 1 else color.gray(0.75)
        marker = sphere(pos=meters_to_scene(start_position_m), radius=random_debris_radius_scene(mass_kg), color=frag_color, opacity=0.82, make_trail=False)
        new_parts.append({
            "marker": marker,
            "position_m": start_position_m,
            "velocity_mps": fragment_velocity_mps,
            "active": True,
            "age": 0,
            "life": 8000,
            "can_collide_after": 10,
            "mass_kg": mass_kg,
            "physical_radius_m": 0.03 + min(0.20, 0.025 * sqrt(mass_kg)),
            "central_body": "Earth",
            "shielding_mm_al": DEFAULT_DEBRIS_SHIELDING_MM_AL,
            "recent_collision_cooldown": 20,
        })
    return new_parts


def deflect_debris_pair(d1, d2):
    normal = d1["position_m"] - d2["position_m"]
    normal = norm(normal) if mag(normal) > 0 else random_unit_vector()
    m1 = d1.get("mass_kg", 1.0)
    m2 = d2.get("mass_kg", 1.0)
    v1 = d1["velocity_mps"]
    v2 = d2["velocity_mps"]
    closing_speed = dot(v1 - v2, normal)
    if closing_speed >= 0:
        normal = -normal
        closing_speed = dot(v1 - v2, normal)
    restitution = 0.15 + random() * 0.35
    impulse = -(1.0 + restitution) * closing_speed / ((1.0 / m1) + (1.0 / m2))
    d1["velocity_mps"] = v1 + (impulse / m1) * normal
    d2["velocity_mps"] = v2 - (impulse / m2) * normal
    sideways = cross(normal, random_unit_vector())
    if mag(sideways) > 0:
        sideways = norm(sideways)
        scatter_speed = 2.0 + random() * 12.0
        d1["velocity_mps"] += sideways * scatter_speed
        d2["velocity_mps"] -= sideways * scatter_speed
    d1["recent_collision_cooldown"] = 20
    d2["recent_collision_cooldown"] = 20


def handle_debris_debris_collisions():
    created_parts = []
    collisions_this_frame = 0
    active_debris = [d for d in debris_particles if d["active"]]
    for i in range(len(active_debris)):
        d1 = active_debris[i]
        if not d1["active"] or d1["age"] < d1["can_collide_after"] or d1.get("recent_collision_cooldown", 0) > 0:
            continue
        for j in range(i + 1, len(active_debris)):
            d2 = active_debris[j]
            if not d2["active"] or d2["age"] < d2["can_collide_after"] or d2.get("recent_collision_cooldown", 0) > 0:
                continue
            distance_m = mag(d1["position_m"] - d2["position_m"])
            if distance_m >= debris_debris_collision_distance_m:
                continue
            collision_pos_m = (d1["position_m"] + d2["position_m"]) / 2
            rel_speed = mag(d1["velocity_mps"] - d2["velocity_mps"])
            m1 = d1.get("mass_kg", 1.0)
            m2 = d2.get("mass_kg", 1.0)
            smaller_mass = min(m1, m2)
            reduced_mass = (m1 * m2) / (m1 + m2)
            energy_per_smaller_mass = 0.5 * reduced_mass * rel_speed ** 2 / max(smaller_mass, 0.001)
            catastrophic = energy_per_smaller_mass > CATASTROPHIC_ENERGY_J_PER_KG
            if catastrophic and random() < 0.35 and len(debris_particles) + len(created_parts) < MAX_DEBRIS_PARTICLES:
                center_velocity = (d1["velocity_mps"] * m1 + d2["velocity_mps"] * m2) / (m1 + m2)
                if m1 <= m2:
                    hide_debris(d1)
                    d2["velocity_mps"] = center_velocity + random_unit_vector() * min(80.0, rel_speed * 0.01)
                    d2["recent_collision_cooldown"] = 25
                else:
                    hide_debris(d2)
                    d1["velocity_mps"] = center_velocity + random_unit_vector() * min(80.0, rel_speed * 0.01)
                    d1["recent_collision_cooldown"] = 25
                created_parts.extend(create_secondary_debris(collision_pos_m, center_velocity, rel_speed, smaller_mass))
                warning_label.text = "DEBRIS-DEBRIS FRAGMENTATION"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.35, 0)
            else:
                deflect_debris_pair(d1, d2)
                warning_label.text = "DEBRIS-DEBRIS DEFLECTION"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.35, 0)
            collisions_this_frame += 1
            if collisions_this_frame >= MAX_DEBRIS_DEBRIS_COLLISIONS_PER_FRAME:
                debris_particles.extend(created_parts)
                return
            break
    debris_particles.extend(created_parts)

# -----------------------------
# RF helpers
# -----------------------------
def free_space_path_loss_db(distance_m, frequency_hz):
    distance_m = max(1.0, distance_m)
    return 20.0 * math.log10(distance_m) + 20.0 * math.log10(frequency_hz) - 147.55


def rf_propagation_delay_s(distance_m):
    return max(0.0, distance_m) / SPEED_OF_LIGHT_MPS


def rf_far_field_boundary_m(largest_antenna_dimension_m, wavelength_m):
    return 0.0 if wavelength_m <= 0 else 2.0 * (largest_antenna_dimension_m ** 2) / wavelength_m


def rf_doppler_shift_hz(relative_velocity_mps, bearing_from_sensor_to_target, frequency_hz):
    if mag(bearing_from_sensor_to_target) == 0:
        return 0.0, 0.0
    closing_speed = max(0.0, -dot(relative_velocity_mps, norm(bearing_from_sensor_to_target)))
    return float((closing_speed / SPEED_OF_LIGHT_MPS) * frequency_hz), float(closing_speed)


def rf_detection_confidence(snr_db):
    margin_db = snr_db - RF_MIN_DETECTABLE_SNR_DB
    return float(clamp_value(1.0 / (1.0 + math.exp(-0.22 * margin_db)), 0.0, 1.0))


def add_small_direction_noise(unit_vector, noise_rad):
    if mag(unit_vector) == 0:
        return vector(1, 0, 0)
    base = norm(unit_vector)
    side = cross(base, random_unit_vector())
    if mag(side) == 0:
        return base
    noisy = base + norm(side) * ((random() - 0.5) * 2.0 * noise_rad)
    return norm(noisy) if mag(noisy) > 0 else base


def blocked_by_earth(sensor_pos_m, target_pos_m):
    dist, t = point_to_segment_distance(vector(0, 0, 0), sensor_pos_m, target_pos_m)
    if 0.02 < t < 0.98 and dist < R_EARTH * 1.02:
        return True, "Earth"
    return False, None


def make_rf_sensor_objects(satellites):
    return [
        {"id": s["name"], "name": s["name"], "type": "satellite", "central_body": "Earth", "position_m": s["position_m"], "velocity_mps": s["velocity_mps"], "base_color": s.get("base_color", color.white)}
        for s in satellites if s.get("active", False)
    ]


def make_rf_target_objects(asteroids, debris_particles):
    targets = []
    for ast in asteroids:
        if ast.get("active", False):
            targets.append({"id": ast["name"], "name": ast["name"], "type": "asteroid", "central_body": "Earth", "position_m": ast["position_m"], "velocity_mps": ast["velocity_mps"], "source": "asteroids"})
    active_debris = [d for d in debris_particles if d.get("active", False)]
    for idx, debris in enumerate(active_debris):
        targets.append({"id": f"DEBRIS-{idx + 1:03d}", "name": f"DEBRIS-{idx + 1:03d}", "type": "debris", "central_body": "Earth", "position_m": debris["position_m"], "velocity_mps": debris["velocity_mps"], "source": "debris"})
    return targets


def classify_rf_threat(distance_m, closing_speed_mps, snr_db):
    if distance_m < 300000.0 or closing_speed_mps > 2500.0:
        return "critical"
    if distance_m < 900000.0 or closing_speed_mps > 1200.0:
        return "high"
    if distance_m < 1800000.0 or snr_db > 25.0:
        return "medium"
    return "low"


def estimate_passive_rf_detection(sensor_obj, target_obj, measurement_timestamp):
    if not PASSIVE_RF_ENABLED:
        return None
    rel_pos = target_obj["position_m"] - sensor_obj["position_m"]
    distance_m = mag(rel_pos)
    if distance_m <= 0 or distance_m > RF_DEFAULT_RANGE_M:
        return None
    blocked, blocking_body = blocked_by_earth(sensor_obj["position_m"], target_obj["position_m"])
    if blocked:
        return None
    signature = RF_SIGNATURE_DBM_AT_1M.get(target_obj.get("type", "unknown"), -30.0)
    path_loss = free_space_path_loss_db(distance_m, RF_FREQUENCY_HZ)
    received_power_dbm = signature + RF_ANTENNA_GAIN_DBI + RF_PROCESSING_GAIN_DB - path_loss
    snr_db = received_power_dbm - RF_NOISE_FLOOR_DBM
    if snr_db < RF_MIN_DETECTABLE_SNR_DB:
        return None
    bearing_true = norm(rel_pos)
    bearing_est = add_small_direction_noise(bearing_true, RF_BEARING_NOISE_RAD)
    snr_quality_scale = max(0.20, min(1.0, 20.0 / max(snr_db, 1.0)))
    range_noise_m = distance_m * RF_RANGE_NOISE_FRACTION * snr_quality_scale
    estimated_range_m = max(0.0, distance_m + (random() - 0.5) * 2.0 * range_noise_m)
    estimated_position_m = sensor_obj["position_m"] + bearing_est * estimated_range_m
    rel_vel = target_obj["velocity_mps"] - sensor_obj["velocity_mps"]
    doppler_shift_hz, closing_speed_mps = rf_doppler_shift_hz(rel_vel, bearing_true, RF_FREQUENCY_HZ)
    tca = None
    rel_speed2 = mag(rel_vel) ** 2
    closest_approach_m = None
    if rel_speed2 > 0:
        tca_calc = -dot(rel_pos, rel_vel) / rel_speed2
        if tca_calc >= 0:
            tca = float(tca_calc)
            closest_approach_m = float(mag(rel_pos + rel_vel * tca_calc))
    return {
        "schema": "satellite_simulation.passive_rf_detection.v2.earth_only",
        "sensor_id": sensor_obj["id"],
        "sensor_type": sensor_obj.get("type", "satellite"),
        "sensor_central_body": "Earth",
        "detected_object_id": target_obj["id"],
        "detected_object_type": target_obj.get("type", "unknown"),
        "detected_object_central_body": "Earth",
        "distance_m": float(distance_m),
        "estimated_range_m": float(estimated_range_m),
        "range_uncertainty_m": float(range_noise_m),
        "bearing_unit_vector": vector_to_dict(bearing_est),
        "true_bearing_unit_vector_debug": vector_to_dict(bearing_true),
        "estimated_position_m_eci": vector_to_dict(estimated_position_m),
        "relative_speed_mps": float(mag(rel_vel)),
        "closing_speed_mps": float(closing_speed_mps),
        "time_to_closest_approach_s": tca,
        "closest_approach_distance_m": closest_approach_m,
        "received_power_dbm": float(received_power_dbm),
        "snr_db": float(snr_db),
        "detection_confidence": rf_detection_confidence(snr_db),
        "sensor_confidence": rf_detection_confidence(snr_db),
        "maxwell_based": True,
        "rf_model_type": RF_MODEL_TYPE,
        "frequency_hz": float(RF_FREQUENCY_HZ),
        "wavelength_m": float(RF_WAVELENGTH_M),
        "propagation_speed_mps": float(SPEED_OF_LIGHT_MPS),
        "propagation_delay_s": float(rf_propagation_delay_s(distance_m)),
        "free_space_path_loss_db": float(path_loss),
        "doppler_shift_hz": float(doppler_shift_hz),
        "far_field_boundary_m": float(rf_far_field_boundary_m(2.0, RF_WAVELENGTH_M)),
        "is_far_field_assumption_valid": bool(distance_m >= rf_far_field_boundary_m(2.0, RF_WAVELENGTH_M)),
        "antenna_gain_dbi": float(RF_ANTENNA_GAIN_DBI),
        "processing_gain_db": float(RF_PROCESSING_GAIN_DB),
        "source_signature_dbm_at_1m": float(signature),
        "noise_floor_dbm": float(RF_NOISE_FLOOR_DBM),
        "line_of_sight": True,
        "blocking_body": blocking_body,
        "threat_level": classify_rf_threat(distance_m, closing_speed_mps, snr_db),
        "measurement_timestamp": measurement_timestamp,
    }


def build_passive_rf_detections(satellites, asteroids, debris_particles, measurement_timestamp):
    sensors = make_rf_sensor_objects(satellites)
    targets = make_rf_target_objects(asteroids, debris_particles)
    detections = []
    threat_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for sensor in sensors:
        sensor_detections = []
        for target in targets:
            detection = estimate_passive_rf_detection(sensor, target, measurement_timestamp)
            if detection is not None:
                sensor_detections.append(detection)
        sensor_detections.sort(key=lambda d: (threat_rank.get(d["threat_level"], 9), d["distance_m"]))
        detections.extend(sensor_detections[:MAX_RF_DETECTIONS_PER_SENSOR])
        if len(detections) >= MAX_TOTAL_RF_DETECTIONS:
            return detections[:MAX_TOTAL_RF_DETECTIONS]
    return detections


def update_rf_sensor_visual_highlights(detections):
    if not SHOW_RF_SENSOR_HIGHLIGHT:
        return
    detecting_ids = set(d["sensor_id"] for d in detections)
    for sat in satellites:
        if not sat.get("active", False):
            continue
        if sat["name"] in detecting_ids and not sat.get("selected_for_data", False):
            sat["marker"].color = color.yellow
        elif not sat.get("selected_for_data", False):
            sat["marker"].color = sat.get("base_color", color.white)

# -----------------------------
# Radiation helpers
# -----------------------------
def radiation_storm_shape(visual_time_s, event):
    start = event["start_visual_time_s"]
    duration = event["duration_visual_time_s"]
    if visual_time_s < start or visual_time_s > start + duration:
        return 0.0
    phase = (visual_time_s - start) / max(duration, 0.001)
    return max(0.0, sin(pi * phase))


def current_solar_weather_state(visual_time_s):
    active_event = None
    active_shape = 0.0
    for event in SOLAR_STORM_EVENTS:
        shape = radiation_storm_shape(visual_time_s, event)
        if shape > active_shape:
            active_shape = shape
            active_event = event
    if active_event is None or active_shape <= 0:
        return {
            "solar_storm_active": False,
            "active_event_name": None,
            "severity": "quiet",
            "storm_phase": 0.0,
            "solar_proton_flux_pfu_gt_10mev": 1.0,
            "solar_wind_speed_km_s": 420.0,
            "kp_index": 2.0,
            "dst_index_nt": -8.0,
            "dose_rate_multiplier": 1.0,
            "rf_blackout_probability": 0.02,
        }
    return {
        "solar_storm_active": True,
        "active_event_name": active_event["name"],
        "severity": active_event["severity"],
        "storm_phase": float(active_shape),
        "solar_proton_flux_pfu_gt_10mev": float(1.0 + active_shape * (active_event["peak_solar_proton_flux_pfu"] - 1.0)),
        "solar_wind_speed_km_s": float(420.0 + active_shape * (active_event["peak_solar_wind_speed_km_s"] - 420.0)),
        "kp_index": float(2.0 + active_shape * (active_event["peak_kp_index"] - 2.0)),
        "dst_index_nt": float(-8.0 - active_shape * 18.0 * active_event["peak_kp_index"]),
        "dose_rate_multiplier": float(1.0 + active_shape * (active_event["peak_dose_multiplier"] - 1.0)),
        "rf_blackout_probability": float(0.02 + active_shape * (active_event["rf_blackout_probability_peak"] - 0.02)),
    }


def radiation_shielding_factor(shielding_mm_al):
    if shielding_mm_al is None or shielding_mm_al <= 0:
        return 1.0
    return 0.5 ** (shielding_mm_al / RADIATION_SHIELDING_HALVING_THICKNESS_MM_AL)


def radiation_region_for_position(position_m):
    alt = altitude_m(position_m)
    if alt < 1000000.0:
        return "low_earth_orbit"
    if alt <= 12000000.0:
        return "inner_van_allen_region"
    if alt <= 60000000.0:
        return "outer_van_allen_region"
    return "high_earth_orbit"


def baseline_dose_rate_for_region(region):
    if region == "low_earth_orbit":
        return EARTH_LEO_BASE_DOSE_RATE_MSV_PER_DAY
    if region == "inner_van_allen_region":
        return BASE_GCR_DOSE_RATE_MSV_PER_DAY * VAN_ALLEN_INNER_BELT_FACTOR
    if region == "outer_van_allen_region":
        return BASE_GCR_DOSE_RATE_MSV_PER_DAY * VAN_ALLEN_OUTER_BELT_FACTOR
    return BASE_GCR_DOSE_RATE_MSV_PER_DAY


def solar_storm_exposure_factor_for_position(position_m, sun_position_m, solar_weather_state):
    if not solar_weather_state.get("solar_storm_active", False):
        return 1.0
    storm_multiplier = solar_weather_state.get("dose_rate_multiplier", 1.0)
    shadow_state = planet_shadow_state_for_object(position_m, sun_position_m)
    sun_exposure = shadow_state.get("sun_exposure_factor", 1.0)
    region = radiation_region_for_position(position_m)
    if region == "low_earth_orbit":
        magnetosphere_factor = 0.20
    elif region in ["inner_van_allen_region", "outer_van_allen_region", "high_earth_orbit"]:
        magnetosphere_factor = 0.65
    else:
        magnetosphere_factor = 1.0
    # Eclipse reduces direct solar proton/radiation exposure, but trapped-belt/GCR terms remain.
    eclipse_shielding_factor = 0.25 + 0.75 * sun_exposure
    return 1.0 + magnetosphere_factor * eclipse_shielding_factor * (storm_multiplier - 1.0)


def radiation_risk_level(dose_rate_msv_per_day, seu_rate_per_day):
    if dose_rate_msv_per_day >= RADIATION_CRITICAL_DOSE_RATE_MSV_PER_DAY or seu_rate_per_day >= SEU_CRITICAL_RATE_PER_DAY:
        return "critical"
    if dose_rate_msv_per_day >= RADIATION_HIGH_DOSE_RATE_MSV_PER_DAY or seu_rate_per_day >= SEU_HIGH_RATE_PER_DAY:
        return "high"
    if dose_rate_msv_per_day >= 2.0 or seu_rate_per_day >= 0.01:
        return "medium"
    return "low"


def estimate_single_event_upset_rate_per_day(dose_rate_msv_per_day, shielding_mm_al, solar_weather_state):
    proton_flux = solar_weather_state.get("solar_proton_flux_pfu_gt_10mev", 1.0)
    shielding = radiation_shielding_factor(shielding_mm_al)
    return max(0.0, 0.0015 * dose_rate_msv_per_day * shielding + 0.00002 * proton_flux * shielding)


def estimate_solar_panel_degradation_per_day(dose_rate_msv_per_day):
    return max(0.0, dose_rate_msv_per_day * 0.00008)


def radiation_particle_flux_for_record(region, storm_factor, solar_weather_state):
    solar_flux = solar_weather_state.get("solar_proton_flux_pfu_gt_10mev", 1.0)
    if region == "inner_van_allen_region":
        trapped_flux = 2500.0
    elif region == "outer_van_allen_region":
        trapped_flux = 950.0
    elif region == "low_earth_orbit":
        trapped_flux = 35.0
    else:
        trapped_flux = 60.0
    return float(trapped_flux + solar_flux * max(0.1, storm_factor))


def radiation_fault_state(risk_level, seu_probability_per_day, electronics_health_percent):
    if risk_level == "critical" or seu_probability_per_day >= RADIATION_FAULT_CRITICAL_SEU_PROBABILITY or electronics_health_percent < 45.0:
        return "critical_fault_risk"
    if risk_level == "high" or seu_probability_per_day >= RADIATION_FAULT_DEGRADED_SEU_PROBABILITY or electronics_health_percent < 70.0:
        return "degraded"
    if risk_level == "medium" or seu_probability_per_day >= RADIATION_FAULT_WATCH_SEU_PROBABILITY or electronics_health_percent < 90.0:
        return "watch"
    return "nominal"


def update_radiation_electronics_health(record, delta_days):
    oid = record["object_id"]
    prev = radiation_electronics_health_by_object.get(oid, 100.0)
    dose_damage = (record["dose_rate_msv_per_day"] * delta_days / RADIATION_HEALTH_DOSE_DAMAGE_SCALE_MSV) * 100.0
    seu_damage = record["single_event_upset_probability_per_day"] * delta_days * RADIATION_HEALTH_SEU_DAMAGE_WEIGHT * 100.0
    panel_damage = record["solar_panel_degradation_fraction_per_day"] * delta_days * RADIATION_HEALTH_PANEL_DAMAGE_WEIGHT * 100.0
    storm_damage = 0.015 if record["flags"].get("solar_storm_active", False) else 0.0
    health = clamp_value(prev - dose_damage - seu_damage - panel_damage - storm_damage, 0.0, 100.0)
    fault = radiation_fault_state(record["radiation_risk_level"], record["single_event_upset_probability_per_day"], health)
    radiation_electronics_health_by_object[oid] = health
    radiation_fault_state_by_object[oid] = fault
    record["electronics_health_percent"] = float(health)
    record["radiation_fault_state"] = fault
    record["radiation_fault_active"] = bool(fault in ["degraded", "critical_fault_risk"])
    return record


def build_radiation_events_payload(visual_time_s):
    events = []
    for event in SOLAR_STORM_EVENTS:
        shape = radiation_storm_shape(visual_time_s, event)
        start = event["start_visual_time_s"]
        end = start + event["duration_visual_time_s"]
        status = "scheduled" if visual_time_s < start else "active" if visual_time_s <= end else "completed"
        events.append({
            "name": event["name"],
            "status": status,
            "start_visual_time_s": float(start),
            "duration_visual_time_s": float(event["duration_visual_time_s"]),
            "end_visual_time_s": float(end),
            "severity": event["severity"],
            "current_phase": float(shape),
            "peak_solar_proton_flux_pfu": float(event["peak_solar_proton_flux_pfu"]),
            "peak_kp_index": float(event["peak_kp_index"]),
            "peak_solar_wind_speed_km_s": float(event["peak_solar_wind_speed_km_s"]),
            "peak_dose_multiplier": float(event["peak_dose_multiplier"]),
            "rf_blackout_probability_peak": float(event["rf_blackout_probability_peak"]),
        })
    return events


def build_radiation_exposure_record(object_id, object_type, position_m, velocity_mps, mass_kg, shielding_mm_al, solar_weather_state, physical_time_s):
    sun_position_m = current_sun_position_eci_m(physical_time_s)
    region = radiation_region_for_position(position_m)
    baseline = baseline_dose_rate_for_region(region)
    storm_factor = solar_storm_exposure_factor_for_position(position_m, sun_position_m, solar_weather_state)
    shielding_factor = radiation_shielding_factor(shielding_mm_al)
    dose_rate = baseline * storm_factor * shielding_factor
    seu_rate = estimate_single_event_upset_rate_per_day(dose_rate, shielding_mm_al, solar_weather_state)
    panel_degradation = estimate_solar_panel_degradation_per_day(dose_rate)
    shadow_state = planet_shadow_state_for_object(position_m, sun_position_m)
    rf_blackout_probability = solar_weather_state.get("rf_blackout_probability", 0.02)
    if region == "low_earth_orbit":
        rf_blackout_probability *= 0.55
    rf_blackout_probability = clamp_value(float(rf_blackout_probability), 0.0, 0.95)
    risk = radiation_risk_level(dose_rate, seu_rate)
    particle_flux = radiation_particle_flux_for_record(region, storm_factor, solar_weather_state)
    return {
        "object_id": object_id,
        "object_type": object_type,
        "central_body": "Earth",
        "radiation_region": region,
        "position_m_eci": vector_to_dict(position_m),
        "speed_mps": speed_mps(velocity_mps),
        "mass_kg": None if mass_kg is None else float(mass_kg),
        "shielding_mm_aluminum": float(shielding_mm_al),
        "shielding_factor": float(shielding_factor),
        "baseline_dose_rate_msv_per_day": float(baseline),
        "solar_storm_exposure_factor": float(storm_factor),
        "sun_exposure_factor": float(shadow_state.get("sun_exposure_factor", 1.0)),
        "in_eclipse": bool(shadow_state.get("in_eclipse", False)),
        "dose_rate_msv_per_day": float(dose_rate),
        "particle_flux_pfu_gt_10mev": float(particle_flux),
        "particle_flux_pfu": float(particle_flux),
        "estimated_single_event_upset_rate_per_day": float(seu_rate),
        "single_event_upset_probability_per_day": float(clamp_value(seu_rate, 0.0, 0.95)),
        "solar_panel_degradation_fraction_per_day": float(panel_degradation),
        "communications_blackout_probability": float(rf_blackout_probability),
        "radiation_risk_level": risk,
        "flags": {
            "solar_storm_active": bool(solar_weather_state.get("solar_storm_active", False)),
            "sunlit_side": bool(not shadow_state.get("in_eclipse", False)),
            "earth_shadow_shielded": bool(shadow_state.get("in_eclipse", False)),
            "high_dose_rate": bool(dose_rate >= RADIATION_HIGH_DOSE_RATE_MSV_PER_DAY),
            "critical_dose_rate": bool(dose_rate >= RADIATION_CRITICAL_DOSE_RATE_MSV_PER_DAY),
            "high_single_event_upset_risk": bool(seu_rate >= SEU_HIGH_RATE_PER_DAY),
            "critical_single_event_upset_risk": bool(seu_rate >= SEU_CRITICAL_RATE_PER_DAY),
        },
    }


def update_satellite_radiation_visuals(exposure_by_id):
    for sat in satellites:
        if not sat.get("active", False):
            continue
        exposure = exposure_by_id.get(sat["name"])
        if exposure is None:
            continue
        risk = exposure.get("radiation_risk_level", "low")
        if sat.get("selected_for_data", False):
            tint = color.yellow
        elif risk == "critical":
            tint = color.red
        elif risk == "high":
            tint = color.orange
        elif risk == "medium":
            tint = color.yellow
        else:
            tint = sat.get("base_color", color.white)
        sat["marker"].color = tint
        sat["body"].color = tint if risk in ["medium", "high", "critical"] else color.white


def build_radiation_aware_decision_request(radiation_payload):
    alerts = radiation_payload.get("alerts", [])
    solar_weather = radiation_payload.get("solar_weather", {})
    high_priority = [a for a in alerts if a.get("risk_level") in ["high", "critical"]]
    return {
        "enabled": True,
        "request_type": "radiation_aware_asset_selection_and_maneuver_planning",
        "router_layer_included_in_simulation": False,
        "note": "Simulation only exports radiation/RF/collision/solar/thermal/power telemetry. External system should decide routing, satellite selection, roll optimization, or avoidance actions.",
        "solar_storm_active": bool(solar_weather.get("solar_storm_active", False)),
        "active_solar_event": solar_weather.get("active_event_name"),
        "recommended_external_considerations": [
            "avoid selecting satellites with high or critical radiation risk",
            "down-rank assets with degraded electronics health or radiation fault state",
            "use sunlight, solar panel, voltage, thermal, RF, and radiation fields together for sensor fusion",
            "use passive_rf_detections plus radiation alerts before issuing thruster commands",
        ],
        "priority_objects": high_priority[:12],
        "requires_external_decision": bool(len(high_priority) > 0 or solar_weather.get("solar_storm_active", False)),
    }


def build_radiation_environment_payload(frame_count_value, visual_time_s, physical_time_s, satellites, asteroids, debris_particles):
    global radiation_last_update_physical_time_s
    solar_weather = current_solar_weather_state(visual_time_s)
    monitored = []
    for sat in satellites:
        if sat.get("active", False):
            monitored.append({"object_id": sat["name"], "object_type": "satellite", "position_m": sat["position_m"], "velocity_mps": sat["velocity_mps"], "mass_kg": sat.get("mass_kg"), "shielding_mm_al": sat.get("shielding_mm_al", DEFAULT_SATELLITE_SHIELDING_MM_AL)})
    for ast in asteroids:
        if ast.get("active", False):
            monitored.append({"object_id": ast["name"], "object_type": "asteroid", "position_m": ast["position_m"], "velocity_mps": ast["velocity_mps"], "mass_kg": ast.get("mass_kg"), "shielding_mm_al": ast.get("shielding_mm_al", DEFAULT_ASTEROID_SHIELDING_MM_AL)})
    active_debris = [d for d in debris_particles if d.get("active", False)]
    for idx, debris in enumerate(active_debris):
        monitored.append({"object_id": f"DEBRIS-{idx + 1:03d}", "object_type": "debris", "position_m": debris["position_m"], "velocity_mps": debris["velocity_mps"], "mass_kg": debris.get("mass_kg"), "shielding_mm_al": debris.get("shielding_mm_al", DEFAULT_DEBRIS_SHIELDING_MM_AL)})
    if radiation_last_update_physical_time_s is None:
        delta_days = 0.0
    else:
        delta_days = max(0.0, (physical_time_s - radiation_last_update_physical_time_s) / 86400.0)
    exposures = []
    for obj in monitored:
        record = build_radiation_exposure_record(obj["object_id"], obj["object_type"], obj["position_m"], obj["velocity_mps"], obj.get("mass_kg"), obj["shielding_mm_al"], solar_weather, physical_time_s)
        if RADIATION_DOSE_TRACKING_ENABLED:
            prev = radiation_cumulative_dose_msv_by_object.get(record["object_id"], 0.0)
            updated = prev + record["dose_rate_msv_per_day"] * delta_days
            radiation_cumulative_dose_msv_by_object[record["object_id"]] = updated
            record["cumulative_estimated_dose_msv"] = float(updated)
            update_radiation_electronics_health(record, delta_days)
        else:
            record["cumulative_estimated_dose_msv"] = 0.0
            record["electronics_health_percent"] = 100.0
            record["radiation_fault_state"] = "nominal"
            record["radiation_fault_active"] = False
        exposures.append(record)
    exposure_by_id = {r["object_id"]: r for r in exposures}
    update_satellite_radiation_visuals(exposure_by_id)
    radiation_last_update_physical_time_s = physical_time_s
    high_or_critical = [r for r in exposures if r["radiation_risk_level"] in ["high", "critical"]]
    payload = {
        "enabled": RADIATION_MODEL_ENABLED,
        "model_type": RADIATION_MODEL_TYPE,
        "description": "Earth-only radiation belts and solar-storm telemetry layer. Exports space-weather risk fields for external decision systems; no routing inside the sim.",
        "router_layer_included": False,
        "frame": int(frame_count_value),
        "visual_time_s": float(visual_time_s),
        "physical_time_s": float(physical_time_s),
        "solar_weather": solar_weather,
        "radiation_model_parameters": {
            "base_gcr_dose_rate_msv_per_day": BASE_GCR_DOSE_RATE_MSV_PER_DAY,
            "earth_leo_base_dose_rate_msv_per_day": EARTH_LEO_BASE_DOSE_RATE_MSV_PER_DAY,
            "van_allen_inner_belt_factor": VAN_ALLEN_INNER_BELT_FACTOR,
            "van_allen_outer_belt_factor": VAN_ALLEN_OUTER_BELT_FACTOR,
            "shielding_halving_thickness_mm_aluminum": RADIATION_SHIELDING_HALVING_THICKNESS_MM_AL,
            "sun_vector_controls_solar_storm_exposure": True,
            "earth_eclipse_reduces_direct_solar_storm_exposure": True,
        },
        "counts": {
            "monitored_objects": len(exposures),
            "high_radiation_risk_objects": sum(1 for r in exposures if r["radiation_risk_level"] == "high"),
            "critical_radiation_risk_objects": sum(1 for r in exposures if r["radiation_risk_level"] == "critical"),
            "objects_with_high_or_critical_seu_risk": sum(1 for r in exposures if r["flags"]["high_single_event_upset_risk"] or r["flags"]["critical_single_event_upset_risk"]),
        },
        "radiation_events": build_radiation_events_payload(visual_time_s),
        "alerts": [
            {
                "object_id": r["object_id"],
                "object_type": r["object_type"],
                "risk_level": r["radiation_risk_level"],
                "dose_rate_msv_per_day": r["dose_rate_msv_per_day"],
                "estimated_single_event_upset_rate_per_day": r["estimated_single_event_upset_rate_per_day"],
                "communications_blackout_probability": r["communications_blackout_probability"],
                "particle_flux_pfu": r.get("particle_flux_pfu"),
                "single_event_upset_probability_per_day": r.get("single_event_upset_probability_per_day"),
                "electronics_health_percent": r.get("electronics_health_percent"),
                "radiation_fault_state": r.get("radiation_fault_state"),
            }
            for r in high_or_critical
        ],
        "object_exposures": exposures,
    }
    payload["decision_request"] = build_radiation_aware_decision_request(payload)
    return payload, exposure_by_id

# -----------------------------
# Sensor confidence helpers
# -----------------------------
def confidence_from_noise_fraction(noise_fraction, ideal=0.005, worst=0.08):
    return float(clamp_value(1.0 - (noise_fraction - ideal) / max(0.001, worst - ideal), 0.35, 0.99))


def confidence_from_noise_c(noise_c, ideal=0.10, worst=3.0):
    return float(clamp_value(1.0 - (noise_c - ideal) / max(0.001, worst - ideal), 0.35, 0.99))


def confidence_from_snr(link_snr_db, low_db=4.0, high_db=24.0):
    return float(clamp_value((link_snr_db - low_db) / max(0.001, high_db - low_db), 0.05, 0.99))


def confidence_from_geometry(factor, floor=0.45):
    return float(clamp_value(floor + (1.0 - floor) * clamp_value(factor, 0.0, 1.0), 0.0, 0.99))


def confidence_from_health(health_percent):
    return float(clamp_value(0.25 + 0.74 * clamp_value(health_percent / 100.0, 0.0, 1.0), 0.25, 0.99))


def build_sensor_confidence_summary(solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, radiation_record, rf_detections_for_sat, camera_sensor=None):
    rf_values = [d.get("detection_confidence", 0.0) for d in rf_detections_for_sat]
    rf_confidence = max(rf_values) if rf_values else 0.55
    confidence = {
        "attitude_sensor_confidence": confidence_from_geometry(solar_panel_system.get("panel_efficiency_factor", 0.0), floor=0.50),
        "solar_panel_sensor_confidence": confidence_from_geometry(solar_panel_system.get("panel_efficiency_factor", 0.0), floor=0.42) if solar_panel_system.get("in_sunlight", False) else 0.48,
        "voltage_sensor_confidence": confidence_from_noise_fraction(voltage_sensors.get("sensor_noise_fraction", SAT_VOLTAGE_SENSOR_NOISE_FRACTION)),
        "battery_power_sensor_confidence": confidence_from_geometry(power_system.get("battery_percent", 0.0) / 100.0, floor=0.60),
        "thermal_sensor_confidence": confidence_from_noise_c(thermal_profile.get("temperature_sensor_noise_c", SAT_TEMPERATURE_SENSOR_NOISE_C)),
        "communication_sensor_confidence": confidence_from_snr(communication_link.get("link_snr_db", 0.0)),
        "radiation_sensor_confidence": confidence_from_health((radiation_record or {}).get("electronics_health_percent", 100.0)),
        "passive_rf_sensor_confidence": float(clamp_value(rf_confidence, 0.0, 0.99)),
        "camera_sensor_confidence": float((camera_sensor or {}).get("sensor_confidence", 0.55)),
    }
    confidence["overall_sensor_confidence"] = float(sum(confidence.values()) / len(confidence))
    confidence["confidence_model_note"] = "Heuristic sensor confidence derived from SNR, sensor noise, geometry, battery state, camera degradation, and radiation/electronics health."
    return confidence


# -----------------------------
# TCAD lookup-table sensor degradation runtime
# -----------------------------
def initialize_tcad_lookup_runtime():
    global tcad_lookup
    if not TCAD_LOOKUP_ENABLED:
        print("TCAD lookup disabled. Using built-in fallback degradation equations.")
        return None
    if not TCAD_RUNTIME_IMPORT_AVAILABLE or load_tcad_lookup is None:
        print(f"TCAD runtime helper not available. Using fallback degradation equations. Import error: {globals().get('TCAD_RUNTIME_IMPORT_ERROR', 'unknown')}")
        tcad_lookup = None
        return None
    print("Loading TCAD sensor-degradation lookup runtime...")
    print(f"  lookup table: {TCAD_LOOKUP_TABLE_PATH}")
    print(f"  sqlite cache: {TCAD_LOOKUP_SQLITE_PATH}")
    tcad_lookup = load_tcad_lookup(
        TCAD_LOOKUP_TABLE_PATH,
        sqlite_path=TCAD_LOOKUP_SQLITE_PATH,
        auto_build_cache=True,
    )
    print(f"TCAD lookup status: {tcad_lookup.last_load_note}")
    return tcad_lookup


def temperature_for_tcad_sensor(sensor_key, thermal_profile):
    if thermal_profile is None:
        return 25.0
    if sensor_key == "thermal_profile":
        return float(thermal_profile.get("bus_temperature_c", 25.0))
    if sensor_key in ["communication_link", "passive_rf"]:
        return float(thermal_profile.get("power_amp_temperature_c", thermal_profile.get("bus_temperature_c", 25.0)))
    if sensor_key in ["command_decoder", "onboard_processor", "camera_sensor"]:
        return float(thermal_profile.get("processor_temperature_c", thermal_profile.get("bus_temperature_c", 25.0)))
    return float(thermal_profile.get("bus_temperature_c", 25.0))


def ensure_sensor_degradation_state(sat, sensor_key):
    sid = sat["name"]
    sensor_id = f"{sid}:{sensor_key}"
    sensor_degradation_state.setdefault(sensor_id, {
        "sensor_id": sensor_id,
        "satellite_id": sid,
        "sensor_key": sensor_key,
        "tcad_sensor_type": SIM_SENSOR_TO_TCAD_SENSOR.get(sensor_key, sensor_key),
        "last_update_physical_time_s": None,
        "cumulative_dose_msv": 0.0,
        "cumulative_particle_flux_pfu_days": 0.0,
        "health_percent": 100.0,
        "noise_accumulated_fraction": 0.0,
        "bias_drift_accumulated_fraction": 0.0,
        "seu_probability_per_day": 0.0,
        "total_expected_seu_count": 0.0,
        "sensor_confidence": 0.99,
    })
    return sensor_degradation_state[sensor_id]


def fallback_tcad_lookup_payload(sensor_key, state, radiation_record, temperature_c):
    dose_rate = float((radiation_record or {}).get("dose_rate_msv_per_day", 0.0))
    particle_flux = float((radiation_record or {}).get("particle_flux_pfu", (radiation_record or {}).get("particle_flux_pfu_gt_10mev", 1.0)))
    sun_exposure = float((radiation_record or {}).get("sun_exposure_factor", 1.0))
    region = (radiation_record or {}).get("radiation_region", "low_earth_orbit")
    trapped_factor = 1.0 if region == "inner_van_allen_region" else 0.70 if region == "outer_van_allen_region" else 0.35
    shield = 0.5 ** (float((radiation_record or {}).get("shielding_mm_aluminum", DEFAULT_SATELLITE_SHIELDING_MM_AL)) / RADIATION_SHIELDING_HALVING_THICKNESS_MM_AL)
    severity = shield * (0.55 + 0.45 * trapped_factor) * (0.25 + 0.75 * sun_exposure)
    sensor_mult = {
        "camera_sensor": 1.35,
        "attitude_state": 1.45,
        "passive_rf": 1.15,
        "communication_link": 1.18,
        "voltage_sensors": 0.85,
        "thermal_profile": 0.75,
        "solar_panel_system": 0.85,
        "command_decoder": 1.10,
        "onboard_processor": 1.25,
    }.get(sensor_key, 1.0)
    temp_stress = max(0.0, temperature_c - 40.0) / 60.0
    health_loss_per_day = clamp_value(sensor_mult * severity * (0.015 * dose_rate + 0.000004 * particle_flux + 0.25 * temp_stress), 0.0, 8.0)
    noise_growth_per_day = clamp_value(sensor_mult * severity * (0.0006 * dose_rate + 0.0000008 * particle_flux + 0.015 * temp_stress), 0.0, 0.5)
    bias_drift_per_day = clamp_value(sensor_mult * severity * (0.00025 * dose_rate + 0.006 * temp_stress), 0.0, 0.25)
    seu_probability_per_day = clamp_value(sensor_mult * shield * (0.00001 * particle_flux + 0.0004 * dose_rate), 0.0, 0.95)
    confidence = clamp_value(1.0 - 0.0012 * state.get("cumulative_dose_msv", 0.0) - health_loss_per_day * 0.04 - noise_growth_per_day * 0.5 - seu_probability_per_day * 0.3, 0.05, 0.99)
    return {
        "enabled": False,
        "source": "simulation_fallback_equation",
        "sensor_type": SIM_SENSOR_TO_TCAD_SENSOR.get(sensor_key, sensor_key),
        "quantized_inputs": {
            "cumulative_dose_msv": state.get("cumulative_dose_msv", 0.0),
            "dose_rate_msv_per_day": dose_rate,
            "particle_flux_pfu": particle_flux,
            "temperature_c": temperature_c,
            "shielding_mm_aluminum": float((radiation_record or {}).get("shielding_mm_aluminum", DEFAULT_SATELLITE_SHIELDING_MM_AL)),
            "sun_exposure_factor": sun_exposure,
            "trapped_belt_factor": trapped_factor,
        },
        "outputs": {"sensor_effects": {
            "sensor_confidence": float(confidence),
            "health_loss_per_day": float(health_loss_per_day),
            "noise_growth_per_day": float(noise_growth_per_day),
            "bias_drift_per_day": float(bias_drift_per_day),
            "seu_probability_per_day": float(seu_probability_per_day),
            "trust_level": "normal" if confidence >= 0.85 else "slightly_downweighted" if confidence >= 0.65 else "heavily_downweighted" if confidence >= 0.40 else "quarantine_sensor_data",
            "safe_to_use_for_autonomous_control": bool(confidence >= 0.65),
        }},
        "note": "TCAD lookup runtime unavailable or lookup table not loaded; using built-in fallback equations.",
    }


def lookup_tcad_for_sensor(sat, sensor_key, radiation_record, thermal_profile, physical_time_s):
    state = ensure_sensor_degradation_state(sat, sensor_key)
    delta_days = 0.0 if state["last_update_physical_time_s"] is None else max(0.0, (physical_time_s - state["last_update_physical_time_s"]) / 86400.0)
    state["last_update_physical_time_s"] = physical_time_s

    dose_rate = float((radiation_record or {}).get("dose_rate_msv_per_day", 0.0))
    particle_flux = float((radiation_record or {}).get("particle_flux_pfu", (radiation_record or {}).get("particle_flux_pfu_gt_10mev", 1.0)))
    shielding = float((radiation_record or {}).get("shielding_mm_aluminum", sat.get("shielding_mm_al", DEFAULT_SATELLITE_SHIELDING_MM_AL)))
    sun_exposure = float((radiation_record or {}).get("sun_exposure_factor", 1.0))
    region = (radiation_record or {}).get("radiation_region", "low_earth_orbit")
    trapped_factor = float(region_to_trapped_belt_factor(region)) if region_to_trapped_belt_factor is not None else (1.0 if region == "inner_van_allen_region" else 0.70 if region == "outer_van_allen_region" else 0.35)
    temperature_c = temperature_for_tcad_sensor(sensor_key, thermal_profile)

    state["cumulative_dose_msv"] += dose_rate * delta_days
    state["cumulative_particle_flux_pfu_days"] += particle_flux * delta_days

    tcad_sensor_type = SIM_SENSOR_TO_TCAD_SENSOR.get(sensor_key, sensor_key)
    if lookup_sensor_degradation is not None:
        lookup_payload = lookup_sensor_degradation(tcad_lookup, tcad_sensor_type, state["cumulative_dose_msv"], dose_rate, particle_flux, temperature_c, shielding, sun_exposure, trapped_factor)
    else:
        lookup_payload = fallback_tcad_lookup_payload(sensor_key, state, radiation_record, temperature_c)

    outputs = lookup_payload.get("outputs", {})
    sensor_effects = outputs.get("sensor_effects", {})
    health_loss_per_day = float(sensor_effects.get("health_loss_per_day", 0.0))
    noise_growth_per_day = float(sensor_effects.get("noise_growth_per_day", 0.0))
    bias_drift_per_day = float(sensor_effects.get("bias_drift_per_day", 0.0))
    seu_probability_per_day = float(sensor_effects.get("seu_probability_per_day", 0.0))
    lookup_confidence = float(sensor_effects.get("sensor_confidence", state.get("sensor_confidence", 0.99)))

    state["health_percent"] = clamp_value(state["health_percent"] - health_loss_per_day * delta_days, 0.0, 100.0)
    state["noise_accumulated_fraction"] = clamp_value(state["noise_accumulated_fraction"] + noise_growth_per_day * delta_days, 0.0, 5.0)
    state["bias_drift_accumulated_fraction"] = clamp_value(state["bias_drift_accumulated_fraction"] + bias_drift_per_day * delta_days, 0.0, 5.0)
    state["seu_probability_per_day"] = clamp_value(seu_probability_per_day, 0.0, 1.0)
    state["total_expected_seu_count"] += seu_probability_per_day * delta_days
    health_confidence = clamp_value(0.05 + 0.94 * state["health_percent"] / 100.0, 0.05, 0.99)
    accumulated_noise_penalty = clamp_value(1.0 - state["noise_accumulated_fraction"], 0.05, 1.0)
    state["sensor_confidence"] = clamp_value(min(lookup_confidence, health_confidence) * accumulated_noise_penalty, 0.01, 0.99)

    return {
        "schema": "satellite_simulation.tcad_sensor_degradation.v1",
        "sensor_key": sensor_key,
        "tcad_sensor_type": tcad_sensor_type,
        "lookup_enabled": bool(lookup_payload.get("enabled", False)),
        "lookup_source": lookup_payload.get("source"),
        "radiation_region": region,
        "sun_exposure_factor": float(sun_exposure),
        "trapped_belt_factor": float(trapped_factor),
        "temperature_c": float(temperature_c),
        "shielding_mm_aluminum": float(shielding),
        "delta_days_integrated": float(delta_days),
        "cumulative_dose_msv": float(state["cumulative_dose_msv"]),
        "cumulative_particle_flux_pfu_days": float(state["cumulative_particle_flux_pfu_days"]),
        "health_percent": float(state["health_percent"]),
        "noise_accumulated_fraction": float(state["noise_accumulated_fraction"]),
        "bias_drift_accumulated_fraction": float(state["bias_drift_accumulated_fraction"]),
        "seu_probability_per_day": float(state["seu_probability_per_day"]),
        "total_expected_seu_count": float(state["total_expected_seu_count"]),
        "sensor_confidence": float(state["sensor_confidence"]),
        "trust_level": sensor_effects.get("trust_level", "unknown"),
        "safe_to_use_for_autonomous_control": bool(sensor_effects.get("safe_to_use_for_autonomous_control", state["sensor_confidence"] >= 0.65)),
        "rates_from_lookup": {
            "health_loss_per_day": float(health_loss_per_day),
            "noise_growth_per_day": float(noise_growth_per_day),
            "bias_drift_per_day": float(bias_drift_per_day),
            "seu_probability_per_day": float(seu_probability_per_day),
        },
        "lookup_quantized_inputs": lookup_payload.get("quantized_inputs", {}),
        "lookup_outputs": outputs,
        "note": "Every sensor is continuously degraded by integrating TCAD lookup-table rates over simulation time.",
    }


def build_all_sensor_degradation_payloads(sat, radiation_record, thermal_profile, physical_time_s):
    return {sensor_key: lookup_tcad_for_sensor(sat, sensor_key, radiation_record, thermal_profile, physical_time_s) for sensor_key in SIM_SENSOR_TO_TCAD_SENSOR}


def apply_tcad_degradation_to_payloads(attitude_payload, solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, camera_sensor, tcad_sensor_degradation):
    def adjust(payload, sensor_key):
        if payload is None:
            return
        degradation = tcad_sensor_degradation.get(sensor_key, {})
        tcad_conf = float(degradation.get("sensor_confidence", payload.get("sensor_confidence", 0.99)))
        existing_conf = float(payload.get("sensor_confidence", 0.99))
        payload["sensor_confidence_without_tcad"] = existing_conf
        payload["sensor_confidence"] = float(clamp_value(min(existing_conf, tcad_conf), 0.0, 0.99))
        payload["tcad_degradation"] = degradation
    adjust(attitude_payload, "attitude_state")
    adjust(solar_panel_system, "solar_panel_system")
    adjust(voltage_sensors, "voltage_sensors")
    adjust(power_system, "voltage_sensors")
    adjust(thermal_profile, "thermal_profile")
    adjust(communication_link, "communication_link")
    adjust(camera_sensor, "camera_sensor")
    if camera_sensor is not None:
        camera_deg = tcad_sensor_degradation.get("camera_sensor", {})
        camera_effects = camera_deg.get("lookup_outputs", {}).get("camera_effects", {})
        if camera_effects:
            camera_sensor["tcad_camera_effects"] = camera_effects
            camera_sensor["dark_current_factor"] = max(float(camera_sensor.get("dark_current_factor", 1.0)), float(camera_effects.get("dark_current_multiplier", 1.0)))
            camera_sensor["hot_pixel_fraction"] = max(float(camera_sensor.get("hot_pixel_fraction", 0.0)), float(camera_effects.get("hot_pixel_fraction", 0.0)))
            camera_sensor["dead_pixel_fraction"] = max(float(camera_sensor.get("dead_pixel_fraction", 0.0)), float(camera_effects.get("dead_pixel_fraction", 0.0)))
            camera_sensor["frame_corruption_probability"] = max(float(camera_sensor.get("frame_corruption_probability", 0.0)), float(camera_effects.get("frame_corruption_probability", 0.0)))
            camera_sensor["camera_confidence_score"] = min(float(camera_sensor.get("camera_confidence_score", 0.99)), float(camera_deg.get("sensor_confidence", 0.99)))
            camera_sensor["sensor_confidence"] = camera_sensor["camera_confidence_score"]


def apply_tcad_confidence_to_sensor_fusion(sensor_fusion_state, tcad_sensor_degradation):
    scores = sensor_fusion_state.get("sensor_confidence_scores", {})
    for sensor_key, score_key in SENSOR_CONFIDENCE_SCORE_KEYS.items():
        if score_key not in scores:
            continue
        degradation = tcad_sensor_degradation.get(sensor_key)
        if degradation is not None:
            scores[score_key] = float(clamp_value(min(float(scores[score_key]), float(degradation.get("sensor_confidence", 0.99))), 0.0, 0.99))
    numeric_values = [v for v in scores.values() if isinstance(v, (int, float))]
    if numeric_values:
        scores["overall_sensor_confidence"] = float(sum(numeric_values) / len(numeric_values))
        sensor_fusion_state["confidence"] = float(scores["overall_sensor_confidence"])
    sensor_fusion_state["tcad_lookup_integrated"] = True
    sensor_fusion_state["tcad_degradation_note"] = "Overall confidence includes persistent per-sensor degradation from the TCAD lookup table."

# -----------------------------
# Camera target geometry and continuous radiation degradation
# -----------------------------
def build_imaging_target_geometry_payload(sat, physical_time_s):
    if active_data_target is None:
        return {"enabled": False, "reason": "no_target_configured"}
    target_pos_m = target_surface_position_m(active_data_target, physical_time_s)
    sat_pos = sat["position_m"]
    sat_to_target = target_pos_m - sat_pos
    slant_range_m = mag(sat_to_target)
    nadir_vec = unit_vector_or_zero(-sat_pos)
    target_vec = unit_vector_or_zero(sat_to_target)
    off_nadir_angle_deg = degrees(math.acos(clamp_value(dot(nadir_vec, target_vec), -1.0, 1.0))) if mag(target_vec) > 0 else 180.0
    target_surface_normal = unit_vector_or_zero(target_pos_m)
    horizon_visible = dot(unit_vector_or_zero(sat_pos - target_pos_m), target_surface_normal) > 0.0
    within_camera_cone = off_nadir_angle_deg <= CAMERA_NADIR_HALF_ANGLE_DEG
    distance_score = clamp_value(1.0 - slant_range_m / CAMERA_MAX_USEFUL_SLANT_RANGE_M, 0.0, 1.0)
    pointing_score = clamp_value(1.0 - off_nadir_angle_deg / max(1.0, CAMERA_NADIR_HALF_ANGLE_DEG), 0.0, 1.0)
    imaging_geometry_score = distance_score * pointing_score * (1.0 if horizon_visible else 0.0)
    return {
        "enabled": True,
        "target_name": active_data_target.get("name"),
        "target_lat_deg": float(active_data_target.get("lat_deg", 0.0)),
        "target_lon_deg": float(active_data_target.get("lon_deg", 0.0)),
        "target_position_m_eci": vector_to_dict(target_pos_m),
        "satellite_to_target_vector_eci": vector_to_dict(unit_vector_or_zero(sat_to_target)),
        "slant_range_to_target_m": float(slant_range_m),
        "distance_to_target_m": float(slant_range_m),
        "off_nadir_angle_deg": float(off_nadir_angle_deg),
        "camera_half_angle_deg": float(CAMERA_NADIR_HALF_ANGLE_DEG),
        "horizon_visible": bool(horizon_visible),
        "within_camera_cone": bool(within_camera_cone),
        "candidate_for_imaging": bool(horizon_visible and within_camera_cone),
        "distance_score": float(distance_score),
        "pointing_geometry_score": float(pointing_score),
        "imaging_geometry_score": float(imaging_geometry_score),
        "external_decision_note": "External program should choose which satellite images this target. The sim only exports target geometry and camera health/confidence."
    }


def ensure_camera_sensor_state(sat):
    sid = sat["name"]
    camera_sensor_state.setdefault(sid, {
        "cumulative_dose_msv": 0.0,
        "cumulative_particle_exposure": 0.0,
        "hot_pixel_fraction": CAMERA_BASE_HOT_PIXEL_FRACTION,
        "dead_pixel_fraction": CAMERA_BASE_DEAD_PIXEL_FRACTION,
        "image_noise_fraction": CAMERA_BASE_IMAGE_NOISE_FRACTION,
        "dark_current_factor": CAMERA_BASE_DARK_CURRENT_FACTOR,
        "frame_corruption_probability": CAMERA_BASE_FRAME_CORRUPTION_PROBABILITY,
        "camera_health_percent": 100.0,
        "last_update_physical_time_s": None,
    })
    return camera_sensor_state[sid]


def camera_lookup_degradation_rates(radiation_record, temperature_c):
    # This acts like the lookup-table interface: environment values in, sensor-specific degradation rates out.
    # You can replace these equations with a CSV/JSON lookup table later without changing the payload structure.
    if radiation_record is None:
        dose_rate = 0.0
        particle_flux = 0.0
        seu_probability = 0.0
        storm_active = False
    else:
        dose_rate = float(radiation_record.get("dose_rate_msv_per_day", 0.0))
        particle_flux = float(radiation_record.get("particle_flux_pfu", radiation_record.get("particle_flux_pfu_gt_10mev", 0.0)))
        seu_probability = float(radiation_record.get("single_event_upset_probability_per_day", 0.0))
        storm_active = bool(radiation_record.get("flags", {}).get("solar_storm_active", False))
    temp_factor = clamp_value(1.0 + max(0.0, temperature_c - 20.0) / 65.0, 1.0, 2.3)
    storm_factor = 1.8 if storm_active else 1.0
    return {
        "dose_rate_msv_per_day": dose_rate,
        "particle_flux_pfu": particle_flux,
        "hot_pixel_growth_per_day": (2.0e-6 * dose_rate + 1.0e-9 * particle_flux) * storm_factor,
        "dead_pixel_growth_per_day": (4.0e-7 * dose_rate + 2.0e-10 * particle_flux) * storm_factor,
        "noise_growth_per_day": (1.5e-4 * dose_rate + 5.0e-8 * particle_flux) * temp_factor * storm_factor,
        "dark_current_growth_per_day": (8.0e-4 * dose_rate * temp_factor) * storm_factor,
        "health_loss_per_day": (0.020 * dose_rate + 4.0 * seu_probability) * storm_factor,
        "frame_corruption_probability_rate_per_day": (0.10 * seu_probability + 1.0e-7 * particle_flux) * storm_factor,
    }


def integrate_camera_degradation(sat, radiation_record, thermal_profile, physical_time_s):
    state = ensure_camera_sensor_state(sat)
    last_time = state.get("last_update_physical_time_s")
    if last_time is None:
        delta_days = 0.0
    else:
        delta_days = max(0.0, (physical_time_s - last_time) / 86400.0)
    state["last_update_physical_time_s"] = physical_time_s

    temperature_c = float(thermal_profile.get("bus_temperature_c", 20.0)) if thermal_profile else 20.0
    rates = camera_lookup_degradation_rates(radiation_record, temperature_c)
    state["cumulative_dose_msv"] += rates["dose_rate_msv_per_day"] * delta_days
    state["cumulative_particle_exposure"] += rates["particle_flux_pfu"] * delta_days
    state["hot_pixel_fraction"] = clamp_value(state["hot_pixel_fraction"] + rates["hot_pixel_growth_per_day"] * delta_days, 0.0, 0.25)
    state["dead_pixel_fraction"] = clamp_value(state["dead_pixel_fraction"] + rates["dead_pixel_growth_per_day"] * delta_days, 0.0, 0.20)
    state["image_noise_fraction"] = clamp_value(state["image_noise_fraction"] + rates["noise_growth_per_day"] * delta_days, 0.0, 0.75)
    state["dark_current_factor"] = clamp_value(state["dark_current_factor"] + rates["dark_current_growth_per_day"] * delta_days, 1.0, 12.0)
    state["frame_corruption_probability"] = clamp_value(CAMERA_BASE_FRAME_CORRUPTION_PROBABILITY + rates["frame_corruption_probability_rate_per_day"], 0.0, 0.95)
    health_loss = rates["health_loss_per_day"] * delta_days
    pixel_damage_loss = (rates["hot_pixel_growth_per_day"] * 120.0 + rates["dead_pixel_growth_per_day"] * 300.0) * delta_days
    state["camera_health_percent"] = clamp_value(state["camera_health_percent"] - health_loss - pixel_damage_loss, 0.0, 100.0)
    return state, rates


def build_camera_sensor_payload(sat, radiation_record, thermal_profile, target_geometry, physical_time_s):
    state, rates = integrate_camera_degradation(sat, radiation_record, thermal_profile, physical_time_s)
    geometry_score = float(target_geometry.get("imaging_geometry_score", 0.0)) if target_geometry else 0.0
    health_score = clamp_value(state["camera_health_percent"] / 100.0, 0.0, 1.0)
    noise_penalty = clamp_value(1.0 - state["image_noise_fraction"], 0.0, 1.0)
    hot_dead_penalty = clamp_value(1.0 - 2.2 * state["hot_pixel_fraction"] - 3.0 * state["dead_pixel_fraction"], 0.0, 1.0)
    corruption_penalty = clamp_value(1.0 - state["frame_corruption_probability"], 0.0, 1.0)
    camera_confidence = clamp_value(0.45 * health_score + 0.20 * noise_penalty + 0.15 * hot_dead_penalty + 0.10 * corruption_penalty + 0.10 * geometry_score, 0.0, 0.99)
    degraded = bool(state["camera_health_percent"] < 90.0 or state["image_noise_fraction"] > 0.06 or state["hot_pixel_fraction"] > 0.001 or state["frame_corruption_probability"] > 0.02)
    return {
        "schema": "satellite_simulation.camera_sensor.v1",
        "enabled": bool(CAMERA_SENSOR_MODEL_ENABLED),
        "sensor_type": "visible_camera",
        "target_geometry": target_geometry,
        "cumulative_dose_msv": float(state["cumulative_dose_msv"]),
        "cumulative_particle_exposure_pfu_day": float(state["cumulative_particle_exposure"]),
        "image_noise_fraction": float(state["image_noise_fraction"]),
        "hot_pixel_fraction": float(state["hot_pixel_fraction"]),
        "dead_pixel_fraction": float(state["dead_pixel_fraction"]),
        "dark_current_factor": float(state["dark_current_factor"]),
        "frame_corruption_probability": float(state["frame_corruption_probability"]),
        "camera_health_percent": float(state["camera_health_percent"]),
        "camera_confidence_score": float(camera_confidence),
        "sensor_confidence": float(camera_confidence),
        "radiation_degraded": degraded,
        "minimum_useful_health_percent": float(CAMERA_MIN_USEFUL_HEALTH_PERCENT),
        "degradation_rates": rates,
        "continuous_degradation_enabled": True,
        "model_note": CAMERA_DIFFRACTION_LIMIT_NOTE,
    }

# -----------------------------
# Solar/power/thermal/attitude sensor-fusion helpers
# -----------------------------
def panel_normal_from_attitude(sat, sun_position_m):
    ensure_satellite_state_defaults(sat)
    att = satellite_attitude_state[sat["name"]]
    # Default panel is roughly normal to orbital plane/velocity frame, then external roll/panel commands rotate it.
    radial = unit_vector_or_zero(sat["position_m"])
    velocity_unit = unit_vector_or_zero(sat["velocity_mps"])
    orbit_normal = cross(radial, velocity_unit)
    if mag(orbit_normal) == 0:
        orbit_normal = vector(0, 0, 1)
    orbit_normal = norm(orbit_normal)
    panel = rotate(orbit_normal, angle=radians(att.get("roll_deg", 0.0)), axis=velocity_unit if mag(velocity_unit) > 0 else vector(0, 1, 0))
    panel = rotate(panel, angle=radians(att.get("pitch_deg", 0.0)), axis=radial if mag(radial) > 0 else vector(1, 0, 0))
    panel = rotate(panel, angle=radians(att.get("panel_rotation_deg", 0.0)), axis=velocity_unit if mag(velocity_unit) > 0 else vector(0, 1, 0))
    return unit_vector_or_zero(panel)


def antenna_normal_from_attitude(sat):
    # Simple Earth-pointing antenna model unless external yaw/roll offsets it.
    ensure_satellite_state_defaults(sat)
    att = satellite_attitude_state[sat["name"]]
    nadir = unit_vector_or_zero(-sat["position_m"])
    velocity_unit = unit_vector_or_zero(sat["velocity_mps"])
    antenna = rotate(nadir, angle=radians(att.get("yaw_deg", 0.0) * 0.05), axis=velocity_unit if mag(velocity_unit) > 0 else vector(0, 0, 1))
    return unit_vector_or_zero(antenna)


def risk_score_from_level(level):
    return {"low": 0.15, "medium": 0.45, "high": 0.75, "critical": 0.95}.get(level, 0.20)


def classify_power_risk(battery_percent, power_margin_w):
    if battery_percent < 15 or power_margin_w < -180:
        return "critical"
    if battery_percent < 30 or power_margin_w < -80:
        return "high"
    if battery_percent < 50 or power_margin_w < 0:
        return "medium"
    return "low"


def classify_thermal_risk(max_temp_c):
    if max_temp_c >= 85:
        return "critical"
    if max_temp_c >= 70:
        return "high"
    if max_temp_c >= 55:
        return "medium"
    return "low"


def classify_communication_risk(packet_loss_probability, link_snr_db):
    if packet_loss_probability >= 0.50 or link_snr_db < 4:
        return "critical"
    if packet_loss_probability >= 0.25 or link_snr_db < 8:
        return "high"
    if packet_loss_probability >= 0.10 or link_snr_db < 12:
        return "medium"
    return "low"


def charging_priority_from_battery(battery_percent):
    if battery_percent < 25:
        return "critical"
    if battery_percent < 45:
        return "high"
    if battery_percent < 70:
        return "medium"
    return "low"


def build_attitude_state_payload(sat, physical_time_s, sun_position_m):
    ensure_satellite_state_defaults(sat)
    att = satellite_attitude_state[sat["name"]]
    panel_normal = panel_normal_from_attitude(sat, sun_position_m)
    antenna_normal = antenna_normal_from_attitude(sat)
    angular_rate = att.get("angular_rate_dps", {"x": 0.0, "y": 0.0, "z": 0.0})
    rate_mag = math.sqrt(angular_rate.get("x", 0.0) ** 2 + angular_rate.get("y", 0.0) ** 2 + angular_rate.get("z", 0.0) ** 2)
    stability = "stable" if rate_mag < 0.2 else "slewing" if rate_mag < 2.0 else "unstable"
    return {
        "schema": "satellite_simulation.attitude_state.v1",
        "roll_deg": float(att.get("roll_deg", 0.0)),
        "pitch_deg": float(att.get("pitch_deg", 0.0)),
        "yaw_deg": float(att.get("yaw_deg", 0.0)),
        "panel_rotation_deg": float(att.get("panel_rotation_deg", 0.0)),
        "angular_rate_dps": {"x": float(angular_rate.get("x", 0.0)), "y": float(angular_rate.get("y", 0.0)), "z": float(angular_rate.get("z", 0.0))},
        "angular_rate_magnitude_dps": float(rate_mag),
        "attitude_stability": stability,
        "panel_normal_eci": vector_to_dict(panel_normal),
        "antenna_normal_eci": vector_to_dict(antenna_normal),
        "attitude_control_source": att.get("attitude_control_source", SAT_ATTITUDE_EXTERNAL_CONTROL_DEFAULT),
        "sensor_confidence": confidence_from_geometry(1.0 - min(1.0, rate_mag / 5.0), floor=0.55),
        "external_control_note": "External controller may command roll/pitch/yaw/panel_rotation. Simulation reports resulting geometry but does not optimize it internally.",
    }


def build_solar_power_thermal_payloads(sat, radiation_record, rf_detections_for_sat, physical_time_s):
    ensure_satellite_state_defaults(sat)
    sun_position_m = current_sun_position_eci_m(physical_time_s)
    sunlight = build_sunlight_state_payload(sat["position_m"], sun_position_m)
    sun_vec = unit_vector_or_zero(sun_position_m - sat["position_m"])
    panel_normal = panel_normal_from_attitude(sat, sun_position_m)
    cos_incidence = max(0.0, dot(panel_normal, sun_vec))
    incidence_deg = degrees(math.acos(clamp_value(dot(panel_normal, sun_vec), -1.0, 1.0))) if mag(panel_normal) > 0 and mag(sun_vec) > 0 else 90.0
    irradiance = solar_irradiance_at_position_w_m2(sat["position_m"], sun_position_m) * sunlight["sun_exposure_factor"]
    degradation = radiation_record.get("solar_panel_degradation_fraction_per_day", 0.0) if radiation_record else 0.0
    degradation_factor = clamp_value(1.0 - degradation * max(0.0, simulation_physical_time / 86400.0), 0.65, 1.0)
    solar_generation_w = irradiance * SAT_SOLAR_PANEL_AREA_M2 * SAT_SOLAR_PANEL_EFFICIENCY * cos_incidence * degradation_factor
    rf_load_w = SAT_RF_PAYLOAD_LOAD_W if len(rf_detections_for_sat) > 0 else 0.0
    radiation_fault_load_w = 25.0 if radiation_record and radiation_record.get("radiation_fault_active", False) else 0.0
    load_w = SAT_BASE_LOAD_W + rf_load_w + radiation_fault_load_w
    power_margin_w = solar_generation_w - load_w

    power_state = satellite_power_state[sat["name"]]
    previous_batt = power_state.get("battery_percent", 80.0)
    delta_hours = max(0.0, dt / 3600.0)
    battery_delta_percent = (power_margin_w * delta_hours / SAT_MAX_BATTERY_WH) * 100.0
    battery_percent = clamp_value(previous_batt + battery_delta_percent, 0.0, 100.0)
    power_state["battery_percent"] = battery_percent

    panel_voltage_v = SAT_PANEL_NOMINAL_VOLTAGE_V * clamp_value(0.25 + 0.75 * cos_incidence, 0.0, 1.0) * sunlight["sun_exposure_factor"]
    panel_voltage_v *= 1.0 + (random() - 0.5) * 2.0 * SAT_VOLTAGE_SENSOR_NOISE_FRACTION
    panel_current_a = solar_generation_w / max(panel_voltage_v, 1.0)
    battery_voltage_v = SAT_BATTERY_NOMINAL_VOLTAGE_V * (0.85 + 0.15 * battery_percent / 100.0)
    battery_voltage_v *= 1.0 + (random() - 0.5) * 2.0 * SAT_VOLTAGE_SENSOR_NOISE_FRACTION
    bus_voltage_v = SAT_NOMINAL_BUS_VOLTAGE_V * (0.96 + 0.04 * battery_percent / 100.0)
    bus_current_draw_a = load_w / max(bus_voltage_v, 1.0)
    net_charge_current_a = power_margin_w / max(battery_voltage_v, 1.0)

    # Recommended panel rotation is just telemetry for external optimizer, not applied here.
    recommended_rotation_deg = clamp_value(incidence_deg, -180.0, 180.0)
    if cos_incidence > 0.92:
        optimization_state = "near_optimal"
    elif cos_incidence > 0.55:
        optimization_state = "suboptimal_angle"
    else:
        optimization_state = "poor_sun_alignment"

    # Thermal model: solar heating + electronics load + radiation/storm contribution + eclipse cooling.
    thermal = satellite_thermal_state[sat["name"]]
    storm_heat_c = 3.0 * (radiation_record.get("solar_storm_exposure_factor", 1.0) - 1.0) if radiation_record else 0.0
    sunlight_heat_c = 12.0 * sunlight["sun_exposure_factor"] * cos_incidence
    electronics_heat_c = 0.035 * load_w
    eclipse_cooling_c = -9.0 if sunlight["in_eclipse"] else 0.0
    target_bus_temp = 5.0 + sunlight_heat_c + electronics_heat_c + storm_heat_c + eclipse_cooling_c
    target_battery_temp = 2.0 + 0.55 * target_bus_temp + 0.020 * abs(net_charge_current_a)
    target_processor_temp = target_bus_temp + 13.0 + 0.030 * load_w
    target_power_amp_temp = target_bus_temp + 18.0 + 0.045 * rf_load_w
    alpha = clamp_value(SAT_THERMAL_TIME_CONSTANT * simulation_speed_multiplier, 0.01, 0.35)
    thermal["bus_temperature_c"] += alpha * (target_bus_temp - thermal["bus_temperature_c"])
    thermal["battery_temperature_c"] += alpha * (target_battery_temp - thermal["battery_temperature_c"])
    thermal["processor_temperature_c"] += alpha * (target_processor_temp - thermal["processor_temperature_c"])
    thermal["power_amp_temperature_c"] += alpha * (target_power_amp_temp - thermal["power_amp_temperature_c"])

    bus_t = thermal["bus_temperature_c"] + (random() - 0.5) * 2.0 * SAT_TEMPERATURE_SENSOR_NOISE_C
    batt_t = thermal["battery_temperature_c"] + (random() - 0.5) * 2.0 * SAT_TEMPERATURE_SENSOR_NOISE_C
    proc_t = thermal["processor_temperature_c"] + (random() - 0.5) * 2.0 * SAT_TEMPERATURE_SENSOR_NOISE_C
    amp_t = thermal["power_amp_temperature_c"] + (random() - 0.5) * 2.0 * SAT_TEMPERATURE_SENSOR_NOISE_C
    max_temp = max(bus_t, batt_t, proc_t, amp_t)
    thermal_risk = classify_thermal_risk(max_temp)
    thermal_fault = "critical_overtemp" if thermal_risk == "critical" else "overtemp" if thermal_risk == "high" else "watch" if thermal_risk == "medium" else "nominal"
    power_risk = classify_power_risk(battery_percent, power_margin_w)

    solar_panel_system = {
        "schema": "satellite_simulation.solar_panel_system.v1",
        "in_sunlight": bool(sunlight["in_sunlight"]),
        "in_eclipse": bool(sunlight["in_eclipse"]),
        "sun_vector_eci": vector_to_dict(sun_vec),
        "panel_normal_eci": vector_to_dict(panel_normal),
        "sun_incidence_angle_deg": float(incidence_deg),
        "panel_efficiency_factor": float(cos_incidence),
        "solar_irradiance_w_m2": float(irradiance),
        "panel_area_m2": float(SAT_SOLAR_PANEL_AREA_M2),
        "panel_efficiency": float(SAT_SOLAR_PANEL_EFFICIENCY),
        "estimated_solar_generation_w": float(solar_generation_w),
        "max_solar_generation_w": float(SAT_MAX_SOLAR_GENERATION_W),
        "recommended_panel_rotation_deg": float(recommended_rotation_deg),
        "panel_rotation_rate_limit_deg_per_s": float(SAT_PANEL_ROTATION_RATE_LIMIT_DEG_PER_S),
        "solar_optimization_state": optimization_state,
        "charging_priority": charging_priority_from_battery(battery_percent),
        "sensor_confidence": confidence_from_geometry(cos_incidence, floor=0.42) if sunlight["in_sunlight"] else 0.48,
        "external_control_note": "External controller should use these values to command roll/panel attitude. The sim does not optimize automatically.",
    }

    voltage_sensors = {
        "schema": "satellite_simulation.voltage_sensors.v1",
        "solar_panel_voltage_v": float(panel_voltage_v),
        "solar_panel_current_a": float(panel_current_a),
        "battery_voltage_v": float(battery_voltage_v),
        "bus_voltage_v": float(bus_voltage_v),
        "bus_current_draw_a": float(bus_current_draw_a),
        "net_charge_current_a": float(net_charge_current_a),
        "sensor_noise_fraction": float(SAT_VOLTAGE_SENSOR_NOISE_FRACTION),
        "sensor_confidence": confidence_from_noise_fraction(SAT_VOLTAGE_SENSOR_NOISE_FRACTION),
    }

    power_system = {
        "schema": "satellite_simulation.power_system.v1",
        "battery_percent": float(battery_percent),
        "battery_capacity_wh": float(SAT_MAX_BATTERY_WH),
        "solar_generation_w": float(solar_generation_w),
        "load_w": float(load_w),
        "base_load_w": float(SAT_BASE_LOAD_W),
        "rf_payload_load_w": float(rf_load_w),
        "radiation_fault_load_w": float(radiation_fault_load_w),
        "power_margin_w": float(power_margin_w),
        "battery_charging_w": float(max(0.0, power_margin_w)),
        "battery_discharging_w": float(max(0.0, -power_margin_w)),
        "power_risk_level": power_risk,
        "sensor_confidence": confidence_from_geometry(battery_percent / 100.0, floor=0.60),
    }

    thermal_profile = {
        "schema": "satellite_simulation.thermal_profile.v1",
        "bus_temperature_c": float(bus_t),
        "battery_temperature_c": float(batt_t),
        "processor_temperature_c": float(proc_t),
        "power_amp_temperature_c": float(amp_t),
        "max_component_temperature_c": float(max_temp),
        "thermal_risk_level": thermal_risk,
        "thermal_fault_state": thermal_fault,
        "in_sunlight": bool(sunlight["in_sunlight"]),
        "in_eclipse": bool(sunlight["in_eclipse"]),
        "thermal_inputs": {
            "sunlight_heat_component_c": float(sunlight_heat_c),
            "electronics_heat_component_c": float(electronics_heat_c),
            "radiation_storm_heat_component_c": float(storm_heat_c),
            "eclipse_cooling_component_c": float(eclipse_cooling_c),
        },
        "temperature_sensor_noise_c": float(SAT_TEMPERATURE_SENSOR_NOISE_C),
        "sensor_confidence": confidence_from_noise_c(SAT_TEMPERATURE_SENSOR_NOISE_C),
    }

    return solar_panel_system, voltage_sensors, power_system, thermal_profile


def build_communication_link_payload(sat, radiation_record, attitude_payload, rf_detections_for_sat):
    antenna_normal = vector(
        attitude_payload["antenna_normal_eci"]["x"],
        attitude_payload["antenna_normal_eci"]["y"],
        attitude_payload["antenna_normal_eci"]["z"],
    )
    ground_dir = unit_vector_or_zero(-sat["position_m"])
    pointing_factor = max(0.0, dot(antenna_normal, ground_dir))
    radiation_blackout = radiation_record.get("communications_blackout_probability", 0.02) if radiation_record else 0.02
    link_snr_db = 24.0 * pointing_factor + 8.0 - 22.0 * radiation_blackout
    rf_processing_load = min(8.0, len(rf_detections_for_sat) * 0.5)
    link_snr_db -= rf_processing_load
    packet_loss = clamp_value(0.02 + radiation_blackout + max(0.0, 10.0 - link_snr_db) * 0.035, 0.0, 0.95)
    latency_ms = 35.0 + 120.0 * packet_loss + random() * 8.0
    risk = classify_communication_risk(packet_loss, link_snr_db)
    return {
        "schema": "satellite_simulation.communication_link.v1",
        "link_available": bool(packet_loss < 0.75 and link_snr_db > 2.0),
        "link_snr_db": float(link_snr_db),
        "packet_loss_probability": float(packet_loss),
        "latency_ms": float(latency_ms),
        "rf_blackout_probability": float(radiation_blackout),
        "antenna_pointing_factor": float(pointing_factor),
        "communication_risk_level": risk,
        "sensor_confidence": confidence_from_snr(link_snr_db),
    }


def build_sensor_fusion_state(sat, radiation_record, solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, rf_detections_for_sat, camera_sensor=None):
    critical_rf = any(d.get("threat_level") == "critical" for d in rf_detections_for_sat)
    high_rf = any(d.get("threat_level") == "high" for d in rf_detections_for_sat)
    if critical_rf:
        collision_risk = 0.95
    elif high_rf:
        collision_risk = 0.75
    elif len(rf_detections_for_sat) > 0:
        collision_risk = 0.35
    else:
        collision_risk = 0.08
    radiation_risk = risk_score_from_level(radiation_record.get("radiation_risk_level", "low")) if radiation_record else 0.15
    thermal_risk = risk_score_from_level(thermal_profile.get("thermal_risk_level", "low"))
    power_risk = risk_score_from_level(power_system.get("power_risk_level", "low"))
    comm_risk = risk_score_from_level(communication_link.get("communication_risk_level", "low"))
    solar_charging_score = clamp_value(solar_panel_system.get("panel_efficiency_factor", 0.0) * (1.0 if solar_panel_system.get("in_sunlight") else 0.0), 0.0, 1.0)
    sensor_confidence_scores = build_sensor_confidence_summary(solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, radiation_record, rf_detections_for_sat, camera_sensor)
    health_components = [
        1.0 - radiation_risk,
        1.0 - thermal_risk,
        1.0 - power_risk,
        1.0 - comm_risk,
        clamp_value(power_system.get("battery_percent", 0.0) / 100.0, 0.0, 1.0),
    ]
    fused_health = sum(health_components) / len(health_components)
    overall_risk = clamp_value(0.32 * collision_risk + 0.20 * radiation_risk + 0.18 * thermal_risk + 0.16 * power_risk + 0.14 * comm_risk, 0.0, 1.0)
    actions = []
    if collision_risk >= 0.70:
        actions.append("evaluate_avoidance_maneuver")
    if solar_panel_system.get("solar_optimization_state") != "near_optimal" and power_system.get("battery_percent", 100.0) < 75:
        actions.append("optimize_solar_panel_angle")
    if thermal_risk >= 0.75:
        actions.append("reduce_payload_load_or_safe_mode")
    if radiation_risk >= 0.75:
        actions.append("radiation_safe_mode_or_asset_switch")
    if comm_risk >= 0.75:
        actions.append("switch_communication_asset")
    if not actions:
        actions.append("continue_nominal_monitoring")
    return {
        "schema": "satellite_simulation.sensor_fusion_state.v1",
        "collision_risk_score": float(collision_risk),
        "radiation_risk_score": float(radiation_risk),
        "thermal_risk_score": float(thermal_risk),
        "power_risk_score": float(power_risk),
        "communication_risk_score": float(comm_risk),
        "solar_charging_score": float(solar_charging_score),
        "sensor_confidence_scores": sensor_confidence_scores,
        "fused_health_score": float(fused_health),
        "overall_operational_risk_score": float(overall_risk),
        "confidence": float(sensor_confidence_scores["overall_sensor_confidence"]),
        "recommended_external_action": actions[0],
        "candidate_external_actions": actions,
        "router_layer_included_in_simulation": False,
    }

# -----------------------------
# Telemetry state serialization
# -----------------------------
def object_state_dict(object_id, object_type, active, position_m, velocity_mps, mass_kg=None, radius_m=None, selected_for_data=False, measurement_timestamp=None, extra=None):
    alt = altitude_m(position_m)
    state = {
        "id": object_id,
        "type": object_type,
        "active": bool(active),
        "selected_for_data": bool(selected_for_data),
        "position_m_eci": vector_to_dict(position_m),
        "velocity_mps_eci": vector_to_dict(velocity_mps),
        "speed_mps": speed_mps(velocity_mps),
        "distance_from_earth_center_m": float(mag(position_m)),
        "altitude_m": float(alt),
        "altitude_km": float(alt / 1000.0),
        "specific_orbital_energy_j_per_kg": orbital_energy_j_per_kg(position_m, velocity_mps),
        "central_body": "Earth",
    }
    if measurement_timestamp is not None:
        state["measurement_timestamp"] = measurement_timestamp
    if mass_kg is not None:
        state["mass_kg"] = float(mass_kg)
    if radius_m is not None:
        state["physical_radius_m"] = float(radius_m)
    if extra is not None:
        state.update(extra)
    return state


def build_external_decision_request(satellite_states, passive_rf_detections, radiation_environment):
    requests = []
    for sat in satellite_states:
        fusion = sat.get("sensor_fusion_state", {})
        if fusion.get("overall_operational_risk_score", 0.0) >= 0.55 or fusion.get("recommended_external_action") != "continue_nominal_monitoring":
            requests.append({
                "target_satellite": sat["id"],
                "recommended_external_action": fusion.get("recommended_external_action"),
                "candidate_external_actions": fusion.get("candidate_external_actions", []),
                "input_data_available": [
                    "environment_vectors", "sunlight_state", "attitude_state", "solar_panel_system",
                    "voltage_sensors", "power_system", "thermal_profile", "communication_link",
                    "radiation", "camera_sensor", "imaging_target_geometry", "passive_rf_detections", "sensor_fusion_state",
                ],
                "reason": "sensor fusion indicates power/thermal/radiation/RF/solar condition requiring external evaluation",
                "router_layer_included_in_simulation": False,
            })
    return {
        "schema": "satellite_simulation.decision_request.v2.earth_sun_sensor_fusion",
        "needs_external_decision": bool(len(requests) > 0 or radiation_environment.get("solar_weather", {}).get("solar_storm_active", False)),
        "router_layer_included_in_simulation": False,
        "simulation_does_not_choose_roll_or_route": True,
        "external_controller_expected": True,
        "request_count": len(requests),
        "requests": requests[:20],
        "global_considerations": [
            "Use sun vectors, panel normals, voltages, battery, camera/target geometry, thermal sensors, and radiation fields to optimize roll/panel pointing externally.",
            "Use RF detections and collision-risk scores before issuing thruster maneuvers.",
            "During solar storms, account for eclipse shielding, radiation fault state, communication risk, and thermal load.",
        ],
    }


def build_telemetry_payload(frame_count_value, visual_time_s, physical_time_s, satellites, asteroids, debris_particles):
    active_debris = [d for d in debris_particles if d["active"]]
    unix_time_s = time.time()
    timestamp_utc_iso = datetime.fromtimestamp(unix_time_s, tz=timezone.utc).isoformat()
    measurement_timestamp = {
        "unix_time_s": float(unix_time_s),
        "utc_iso": timestamp_utc_iso,
        "simulation_visual_time_s": float(visual_time_s),
        "simulation_physical_time_s": float(physical_time_s),
        "frame": int(frame_count_value),
        "sample_hz": float(effective_telemetry_sample_hz()),
        "base_sample_hz": float(BASE_TELEMETRY_SAMPLE_HZ),
        "speed_multiplier": float(simulation_speed_multiplier),
    }
    passive_rf_detections = build_passive_rf_detections(satellites, asteroids, debris_particles, measurement_timestamp)
    radiation_environment, radiation_exposure_by_id = build_radiation_environment_payload(frame_count_value, visual_time_s, physical_time_s, satellites, asteroids, debris_particles)
    detections_by_sensor = {}
    for d in passive_rf_detections:
        detections_by_sensor.setdefault(d["sensor_id"], []).append(d)

    satellite_states = []
    for sat in satellites:
        radiation_record = radiation_exposure_by_id.get(sat["name"], None)
        rf_for_sat = detections_by_sensor.get(sat["name"], [])
        attitude_payload = build_attitude_state_payload(sat, physical_time_s, current_sun_position_eci_m(physical_time_s))
        solar_panel_system, voltage_sensors, power_system, thermal_profile = build_solar_power_thermal_payloads(sat, radiation_record, rf_for_sat, physical_time_s)
        communication_link = build_communication_link_payload(sat, radiation_record, attitude_payload, rf_for_sat)
        imaging_target_geometry = build_imaging_target_geometry_payload(sat, physical_time_s)
        camera_sensor = build_camera_sensor_payload(sat, radiation_record, thermal_profile, imaging_target_geometry, physical_time_s)
        tcad_sensor_degradation = build_all_sensor_degradation_payloads(sat, radiation_record, thermal_profile, physical_time_s)
        apply_tcad_degradation_to_payloads(attitude_payload, solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, camera_sensor, tcad_sensor_degradation)
        sensor_fusion_state = build_sensor_fusion_state(sat, radiation_record, solar_panel_system, voltage_sensors, power_system, thermal_profile, communication_link, rf_for_sat, camera_sensor)
        apply_tcad_confidence_to_sensor_fusion(sensor_fusion_state, tcad_sensor_degradation)
        satellite_states.append(object_state_dict(
            object_id=sat["name"],
            object_type="satellite",
            active=sat["active"],
            position_m=sat["position_m"],
            velocity_mps=sat["velocity_mps"],
            mass_kg=sat.get("mass_kg"),
            radius_m=sat.get("physical_radius_m"),
            selected_for_data=sat.get("selected_for_data", False),
            measurement_timestamp=measurement_timestamp,
            extra={
                "can_maneuver": bool(sat["active"]),
                "destroyed": not bool(sat["active"]),
                "orbit_class": sat.get("orbit_class", "unknown"),
                "orbit_description": sat.get("orbit_description", "unknown"),
                "environment_vectors": build_environment_vectors_payload(sat["name"], "satellite", sat["position_m"], sat["velocity_mps"], physical_time_s),
                "sunlight_state": build_sunlight_state_payload(sat["position_m"], current_sun_position_eci_m(physical_time_s)),
                "attitude_state": attitude_payload,
                "solar_panel_system": solar_panel_system,
                "voltage_sensors": voltage_sensors,
                "power_system": power_system,
                "thermal_profile": thermal_profile,
                "communication_link": communication_link,
                "camera_sensor": camera_sensor,
                "tcad_sensor_degradation": tcad_sensor_degradation,
                "imaging_target_geometry": imaging_target_geometry,
                "distance_to_imaging_target_m": imaging_target_geometry.get("distance_to_target_m"),
                "radiation": radiation_record,
                "sensor_fusion_state": sensor_fusion_state,
                "sensor_confidence_scores": sensor_fusion_state.get("sensor_confidence_scores", {}),
            },
        ))

    asteroid_states = []
    for ast in asteroids:
        asteroid_states.append(object_state_dict(
            object_id=ast["name"],
            object_type="asteroid",
            active=ast["active"],
            position_m=ast["position_m"],
            velocity_mps=ast["velocity_mps"],
            mass_kg=ast.get("mass_kg"),
            radius_m=ast.get("physical_radius_m"),
            measurement_timestamp=measurement_timestamp,
            extra={
                "collision_threat": bool(ast["active"]),
                "orbit_class": ast.get("orbit_class", "unknown"),
                "orbit_description": ast.get("orbit_description", "unknown"),
                "radiation": radiation_exposure_by_id.get(ast["name"], None),
            },
        ))

    debris_states = []
    for idx, debris in enumerate(active_debris):
        did = f"DEBRIS-{idx + 1:03d}"
        debris_states.append(object_state_dict(
            object_id=did,
            object_type="debris",
            active=debris["active"],
            position_m=debris["position_m"],
            velocity_mps=debris["velocity_mps"],
            mass_kg=debris.get("mass_kg"),
            radius_m=debris.get("physical_radius_m"),
            measurement_timestamp=measurement_timestamp,
            extra={
                "age_frames": int(debris.get("age", 0)),
                "life_frames_remaining": int(debris.get("life", 0)),
                "recent_collision_cooldown_frames": int(debris.get("recent_collision_cooldown", 0)),
                "radiation": radiation_exposure_by_id.get(did, None),
            },
        ))

    all_hazards = asteroid_states + debris_states
    decision_request = build_external_decision_request(satellite_states, passive_rf_detections, radiation_environment)

    return {
        "schema": "satellite_simulation.telemetry.v2.earth_sun_sensor_fusion",
        "frame": int(frame_count_value),
        "measurement_timestamp": measurement_timestamp,
        "utc_iso": timestamp_utc_iso,
        "unix_time_s": float(unix_time_s),
        "visual_time_s": float(visual_time_s),
        "physical_time_s": float(physical_time_s),
        "mars_removed": True,
        "units": {"position": "meters, Earth-centered inertial demo frame", "velocity": "meters per second", "mass": "kilograms", "radius": "meters", "time": "seconds", "timestamp": "UTC ISO-8601 and Unix seconds"},
        "constants": {
            "earth_mu_m3_s2": MU_EARTH,
            "earth_radius_m": R_EARTH,
            "earth_j2": J2_EARTH,
            "j2_perturbation_enabled": ENABLE_J2_PERTURBATION,
            "atmospheric_drag_enabled": ENABLE_ATMOSPHERIC_DRAG,
            "drag_coefficient": DRAG_COEFFICIENT,
            "max_drag_altitude_m": MAX_DRAG_ALTITUDE_M,
            "sun_true_radius_m": R_SUN,
            "sun_scene_radius_earth_radii": SUN_VISUAL_RADIUS_TRUE_SCALE,
            "earth_sun_distance_m": AU_M,
            "solar_constant_w_m2_at_1au": SOLAR_CONSTANT_W_M2,
            "earth_rotation_rate_rad_s": EARTH_ROTATION_RATE,
            "earth_orbital_speed_mps": EARTH_ORBITAL_SPEED_MPS,
            "earth_orbital_angular_rate_rad_s": EARTH_ORBITAL_ANGULAR_RATE_RAD_S,
            "moon_mu_m3_s2": MU_MOON,
            "moon_radius_m": R_MOON,
            "moon_semi_major_axis_m": MOON_SEMI_MAJOR_AXIS_M,
            "moon_orbital_period_s": MOON_ORBITAL_PERIOD_S,
            "moon_orbital_angular_rate_rad_s": MOON_ORBITAL_ANGULAR_RATE_RAD_S,
            "solar_third_body_gravity_enabled": ENABLE_SOLAR_THIRD_BODY_GRAVITY,
            "lunar_third_body_gravity_enabled": ENABLE_LUNAR_THIRD_BODY_GRAVITY,
            "time_step_s": dt,
            "base_time_step_s": BASE_DT,
            "speed_multiplier": simulation_speed_multiplier,
            "passive_rf_enabled": PASSIVE_RF_ENABLED,
            "rf_model_type": RF_MODEL_TYPE,
            "rf_maxwell_based": True,
            "rf_model_note": "Maxwell-derived far-field approximation: c, wavelength, free-space path loss, Doppler shift, antenna gain, noise floor, and SNR. This is not a full FDTD/Maxwell grid solver.",
            "rf_frequency_hz": RF_FREQUENCY_HZ,
            "rf_wavelength_m": RF_WAVELENGTH_M,
            "radiation_model_enabled": RADIATION_MODEL_ENABLED,
            "radiation_model_type": RADIATION_MODEL_TYPE,
            "solar_sensor_model_enabled": SOLAR_SENSOR_MODEL_ENABLED,
            "camera_sensor_model_enabled": CAMERA_SENSOR_MODEL_ENABLED,
            "camera_continuous_degradation_enabled": True,
            "tcad_lookup_enabled": bool(TCAD_LOOKUP_ENABLED),
            "tcad_runtime_import_available": bool(TCAD_RUNTIME_IMPORT_AVAILABLE),
            "tcad_lookup_table_path": TCAD_LOOKUP_TABLE_PATH,
            "tcad_lookup_loaded": bool(tcad_lookup is not None and getattr(tcad_lookup, "enabled", False)),
            "tcad_lookup_status": getattr(tcad_lookup, "last_load_note", "not_loaded"),
            "router_layer_included": False,
        },
        "counts": {
            "active_satellites": sum(1 for sat in satellites if sat["active"]),
            "active_asteroids": sum(1 for obj in asteroid_states if obj["active"]),
            "active_debris": len(debris_states),
            "total_hazards": len([obj for obj in all_hazards if obj["active"]]),
            "passive_rf_detections": len(passive_rf_detections),
            "critical_rf_detections": sum(1 for d in passive_rf_detections if d.get("threat_level") == "critical"),
            "high_rf_detections": sum(1 for d in passive_rf_detections if d.get("threat_level") == "high"),
            "radiation_monitored_objects": radiation_environment["counts"]["monitored_objects"],
            "high_radiation_risk_objects": radiation_environment["counts"]["high_radiation_risk_objects"],
            "critical_radiation_risk_objects": radiation_environment["counts"]["critical_radiation_risk_objects"],
            "solar_storm_active": bool(radiation_environment["solar_weather"].get("solar_storm_active", False)),
        },
        "satellites": satellite_states,
        "spacecraft": [],
        "asteroids": asteroid_states,
        "debris": debris_states,
        "hazards": all_hazards,
        "active_imaging_target": {
            "enabled": active_data_target is not None,
            "name": None if active_data_target is None else active_data_target.get("name"),
            "lat_deg": None if active_data_target is None else float(active_data_target.get("lat_deg", 0.0)),
            "lon_deg": None if active_data_target is None else float(active_data_target.get("lon_deg", 0.0)),
            "position_m_eci": None if active_data_target is None else vector_to_dict(target_surface_position_m(active_data_target, physical_time_s)),
            "source": "terminal_input_or_quantum_commands_json",
        },
        "solar_environment": build_solar_environment_payload(physical_time_s),
        "lunar_environment": build_lunar_environment_payload(physical_time_s),
        "environment_model": {
            "enabled": True,
            "description": "Earth/Sun/Moon environment vectors for external attitude, roll, antenna, solar panel optimization, and third-body gravity awareness.",
            "simulation_controls_attitude_internally": False,
            "external_controller_expected": True,
            "mars_removed": True,
        },
        "radiation_environment": radiation_environment,
        "radiation_effects": radiation_environment,
        "radiation_alerts": radiation_environment.get("alerts", []),
        "radiation_events": radiation_environment.get("radiation_events", []),
        "passive_rf": {
            "enabled": PASSIVE_RF_ENABLED,
            "maxwell_based": True,
            "model_type": RF_MODEL_TYPE,
            "description": "Passive RF detections from Earth satellites using a Maxwell-derived far-field electromagnetic wave approximation.",
            "physics": {
                "maxwell_equations_used_as_foundation": True,
                "solves_full_spatial_eb_fields": False,
                "approximation": "far-field link-budget / ray-style RF propagation",
                "propagation_speed_mps": SPEED_OF_LIGHT_MPS,
                "frequency_hz": RF_FREQUENCY_HZ,
                "wavelength_m": RF_WAVELENGTH_M,
                "includes_free_space_path_loss": True,
                "includes_line_of_sight_blocking_by_earth": True,
                "includes_doppler_shift": True,
                "includes_snr_thresholding": True,
                "includes_measurement_noise": True,
            },
            "detections": passive_rf_detections,
        },
        "passive_rf_detections": passive_rf_detections,
        "decision_request": decision_request,
    }


def write_telemetry_snapshot_for_mqtt(payload):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"telemetry_{timestamp}_frame_{payload['frame']:08d}.json"
    final_path = os.path.join(MQTT_TELEMETRY_OUTPUT_DIR, filename)
    temp_path = final_path + ".tmp"
    outbox_payload = dict(payload)
    outbox_payload["telemetry_file"] = {
        "path": final_path,
        "created_unix_time_s": float(time.time()),
        "created_utc_iso": datetime.now(timezone.utc).isoformat(),
        "write_mode": "unique_snapshot_file_for_mqtt_outbox",
        "intended_sample_hz": float(effective_telemetry_sample_hz()),
        "mqtt_bridge_note": "SMS communication script should publish this file and delete it after sending.",
    }
    with open(temp_path, "w") as f:
        json.dump(outbox_payload, f, indent=2)
        f.write("\n")
    os.replace(temp_path, final_path)
    try:
        os.utime(final_path, None)
    except Exception:
        pass
    return final_path


def write_live_telemetry_json(payload):
    folder = os.path.dirname(TELEMETRY_JSON_PATH)
    if folder and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)
    with open(TELEMETRY_JSON_TEMP_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(TELEMETRY_JSON_TEMP_PATH, TELEMETRY_JSON_PATH)


def export_telemetry(frame_count_value, visual_time_s, physical_time_s, satellites, asteroids, debris_particles):
    payload = build_telemetry_payload(frame_count_value, visual_time_s, physical_time_s, satellites, asteroids, debris_particles)
    update_rf_sensor_visual_highlights(payload.get("passive_rf_detections", []))
    if TELEMETRY_OUTPUT_MODE == "mqtt_outbox":
        try:
            output_path = write_telemetry_snapshot_for_mqtt(payload)
            telemetry_label.text = (
                f"Telemetry outbox: {effective_telemetry_sample_hz():.1f} Hz | speed {simulation_speed_multiplier}x | "
                f"{payload['counts']['active_satellites']} sats, {payload['counts']['active_asteroids']} asteroids, "
                f"{payload['counts']['active_debris']} debris | RF {payload['counts'].get('passive_rf_detections', 0)} | frame {payload['frame']}"
            )
            telemetry_label.color = color.white
            # Intentionally quiet: telemetry files are written without spamming the terminal.
        except Exception as e:
            telemetry_label.text = f"Telemetry outbox error: {e}"
            telemetry_label.color = color.red
            print(f"Telemetry outbox error: {e}")
    elif TELEMETRY_OUTPUT_MODE == "json_file":
        write_live_telemetry_json(payload)
    elif TELEMETRY_OUTPUT_MODE == "terminal":
        print(json.dumps(payload, indent=2))

# Create the initial terminal-entered target marker. External commands can replace it later.
update_data_target_marker(active_data_target)

# -----------------------------
# Build systems
# -----------------------------
LEO_ALTITUDES_M = [550000.0, 700000.0, 850000.0, 1000000.0, 1200000.0]
MEO_ALTITUDES_M = [20200000.0, 21500000.0, 23222000.0]
HEO_PERIGEE_ALTITUDE_M = 1000000.0
HEO_APOGEE_ALTITUDE_M = 39700000.0
SATELLITE_COLORS = [color.cyan, color.green, color.orange, color.magenta, color.yellow, vector(0.30, 0.55, 1.0), vector(0.85, 0.55, 0.20), vector(0.70, 1.00, 0.70), vector(1.00, 0.55, 0.55), vector(0.75, 0.75, 1.00)]
TRAIL_COLORS = [vector(0.12, 0.35, 0.50), vector(0.12, 0.40, 0.12), vector(0.45, 0.28, 0.08), vector(0.40, 0.12, 0.40), vector(0.45, 0.45, 0.10), vector(0.12, 0.25, 0.55), vector(0.45, 0.22, 0.08), vector(0.20, 0.45, 0.20), vector(0.45, 0.15, 0.15), vector(0.28, 0.28, 0.50)]


def color_for_satellite(index):
    return SATELLITE_COLORS[index % len(SATELLITE_COLORS)]


def trail_color_for_satellite(index):
    return TRAIL_COLORS[index % len(TRAIL_COLORS)]


def build_requested_constellation():
    built = []
    idx = 1
    for i in range(REQUESTED_LEO_SATELLITES):
        alt = LEO_ALTITUDES_M[i % len(LEO_ALTITUDES_M)]
        inc = [53, 70, 98, 45, 82][i % 5]
        raan = (i * 360.0 / max(1, REQUESTED_LEO_SATELLITES)) % 360
        phase = (i * 137.5) % 360
        draw_circular_orbit(alt, color.gray(0.27), inc, raan)
        built.append(create_satellite(f"SAT-{idx}", alt, inc, raan, phase, color_for_satellite(idx - 1), trail_color_for_satellite(idx - 1), orbit_class="LEO", orbit_description=f"LEO circular orbit, altitude {alt / 1000.0:.0f} km"))
        idx += 1
    for i in range(REQUESTED_MEO_SATELLITES):
        alt = MEO_ALTITUDES_M[i % len(MEO_ALTITUDES_M)]
        inc = [55, 56, 63][i % 3]
        raan = (35 + i * 360.0 / max(1, REQUESTED_MEO_SATELLITES)) % 360
        phase = (90 + i * 131.0) % 360
        draw_circular_orbit(alt, color.gray(0.18), inc, raan)
        built.append(create_satellite(f"SAT-{idx}", alt, inc, raan, phase, color_for_satellite(idx - 1), trail_color_for_satellite(idx - 1), orbit_class="MEO", orbit_description=f"MEO circular orbit, altitude {alt / 1000.0:.0f} km"))
        idx += 1
    for i in range(REQUESTED_HEO_SATELLITES):
        inc = 63.4
        raan = (70 + i * 360.0 / max(1, REQUESTED_HEO_SATELLITES)) % 360
        argp = 270
        ta = (i * 147.0) % 360
        draw_elliptical_orbit(HEO_PERIGEE_ALTITUDE_M, HEO_APOGEE_ALTITUDE_M, color.gray(0.16), inc, raan, argp)
        built.append(create_heo_satellite(f"SAT-{idx}", HEO_PERIGEE_ALTITUDE_M, HEO_APOGEE_ALTITUDE_M, inc, raan, argp, ta, color_for_satellite(idx - 1), trail_color_for_satellite(idx - 1)))
        idx += 1
    return built


def build_requested_asteroids(existing_satellites):
    built = []
    idx = 1
    target_sat = existing_satellites[min(1, len(existing_satellites) - 1)] if len(existing_satellites) > 0 else None
    for i in range(REQUESTED_LEO_ASTEROIDS):
        if i == 0 and target_sat is not None:
            built.append(create_physical_asteroid(target_sat, DESIRED_VISUAL_COLLISION_TIME_S, name=f"AST-{idx}"))
        else:
            alt = LEO_ALTITUDES_M[i % len(LEO_ALTITUDES_M)] + 50000.0
            inc = [51.6, 63, 74, 97][i % 4]
            raan = (20 + i * 360.0 / max(1, REQUESTED_LEO_ASTEROIDS)) % 360
            phase = (45 + i * 123.0) % 360
            draw_circular_orbit(alt, color.gray(0.20), inc, raan)
            built.append(create_circular_asteroid(f"AST-{idx}", alt, inc, raan, phase, "LEO", prograde=False))
        idx += 1
    for i in range(REQUESTED_MEO_ASTEROIDS):
        alt = MEO_ALTITUDES_M[i % len(MEO_ALTITUDES_M)] + 100000.0
        inc = [55, 63, 70][i % 3]
        raan = (55 + i * 360.0 / max(1, REQUESTED_MEO_ASTEROIDS)) % 360
        phase = (120 + i * 129.0) % 360
        draw_circular_orbit(alt, color.gray(0.14), inc, raan)
        built.append(create_circular_asteroid(f"AST-{idx}", alt, inc, raan, phase, "MEO", prograde=False))
        idx += 1
    for i in range(REQUESTED_HEO_ASTEROIDS):
        inc = 63.4
        raan = (95 + i * 360.0 / max(1, REQUESTED_HEO_ASTEROIDS)) % 360
        argp = 270
        ta = (60 + i * 151.0) % 360
        draw_elliptical_orbit(HEO_PERIGEE_ALTITUDE_M, HEO_APOGEE_ALTITUDE_M, color.gray(0.12), inc, raan, argp)
        built.append(create_heo_asteroid(f"AST-{idx}", HEO_PERIGEE_ALTITUDE_M, HEO_APOGEE_ALTITUDE_M, inc, raan, argp, ta, prograde=False))
        idx += 1
    return built


satellites = build_requested_constellation()
asteroids = build_requested_asteroids(satellites)
asteroid = asteroids[0] if len(asteroids) > 0 else None
active_visual_events = []
debris_particles = []

# Collision settings
satellite_collision_distance_m = 180000.0
debris_collision_distance_m = 240000.0
warning_distance_m = 700000.0


def active_satellite_count():
    return sum(1 for s in satellites if s["active"])


print("Telemetry output mode:", TELEMETRY_OUTPUT_MODE)
print(f"Telemetry snapshots will be queued in '{MQTT_TELEMETRY_OUTPUT_DIR}' at {effective_telemetry_sample_hz():.1f} Hz.")
initialize_tcad_lookup_runtime()

# Initial telemetry snapshot
export_telemetry(0, 0.0, 0.0, satellites, asteroids, debris_particles)

# -----------------------------
# Main loop
# -----------------------------
while True:
    rate(rate_value)

    if simulation_ended:
        continue
    if not simulation_running:
        continue

    frame_count += 1
    simulation_physical_time += dt
    visual_time = frame_count / rate_value
    physical_time = simulation_physical_time

    # Update true-distance Sun marker and label based on Earth orbital path.
    sun_marker.pos = meters_to_scene(current_sun_position_eci_m(physical_time))
    sun_label.pos = sun_marker.pos + vector(0, -SUN_VISUAL_RADIUS_TRUE_SCALE * 1.15, 0)
    moon_marker.pos = meters_to_scene(current_moon_position_eci_m(physical_time))
    moon_label.pos = moon_marker.pos + vector(0.16, 0.16, 0)
    refresh_data_target_marker(physical_time)

    timer_label.text = f"Visual Time: {visual_time:.1f} s | Physical Time: {physical_time:.0f} s | Speed: {simulation_speed_multiplier}x"

    if frame_count % COMMAND_CHECK_INTERVAL_FRAMES == 0:
        check_for_command_update(satellites)

    if frame_count % telemetry_export_interval_frames() == 0:
        export_telemetry(frame_count, visual_time, physical_time, satellites, asteroids, debris_particles)

    for sat in satellites:
        update_satellite_physics(sat)
        update_satellite_visuals(sat)

    for ast in asteroids:
        if ast["active"]:
            update_asteroid(ast)

    for debris in debris_particles:
        update_debris_particle(debris)

    handle_debris_debris_collisions()

    if frame_count % 300 == 0:
        debris_particles = [d for d in debris_particles if d["active"]]

    # Asteroid-to-satellite collision checks
    if any(ast["active"] for ast in asteroids):
        warning_label.text = ""
        asteroid_collision_happened = False
        for ast in asteroids:
            if not ast["active"]:
                continue
            for sat in satellites:
                if not sat["active"]:
                    continue
                distance_m = mag(ast["position_m"] - sat["position_m"])
                if distance_m < warning_distance_m:
                    warning_label.text = f"WARNING: {ast['name']} CLOSE APPROACH"
                    warning_label.pos = sat["marker"].pos + vector(0, 0.45, 0)
                if distance_m < satellite_collision_distance_m:
                    collision_pos_m = (ast["position_m"] + sat["position_m"]) / 2
                    destroyed_velocity_mps = sat["velocity_mps"]
                    warning_label.text = f"COLLISION: {ast['name']} HIT {sat['name']}"
                    warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.55, 0)
                    hide_satellite(sat)
                    hide_object(ast)
                    visuals, new_debris = create_breakup_event(collision_pos_m, destroyed_velocity_mps)
                    active_visual_events.append(visuals)
                    debris_particles.extend(new_debris)
                    asteroid_collision_happened = True
                    break
            if asteroid_collision_happened:
                break

    # Debris-to-satellite chain reaction
    debris_collision_happened = False
    for debris in debris_particles:
        if not debris["active"] or debris["age"] < debris["can_collide_after"]:
            continue
        for sat in satellites:
            if not sat["active"]:
                continue
            distance_m = mag(debris["position_m"] - sat["position_m"])
            if distance_m < debris_collision_distance_m:
                collision_pos_m = (debris["position_m"] + sat["position_m"]) / 2
                destroyed_velocity_mps = sat["velocity_mps"]
                warning_label.text = f"DEBRIS HIT {sat['name']}"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.55, 0)
                hide_satellite(sat)
                hide_debris(debris)
                visuals, new_debris = create_breakup_event(collision_pos_m, destroyed_velocity_mps)
                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)
                debris_collision_happened = True
                break
        if debris_collision_happened:
            break

    updated_events = []
    for event in active_visual_events:
        updated = update_visual_event(event)
        if len(updated) > 0:
            updated_events.append(updated)
    active_visual_events = updated_events

    if all(not ast["active"] for ast in asteroids) and len(active_visual_events) == 0:
        if active_satellite_count() > 0 and len(debris_particles) > 0:
            if warning_label.text == "" or warning_label.text.startswith("COLLISION"):
                warning_label.text = "ORBITING DEBRIS FIELD ACTIVE"
                warning_label.pos = vector(0, 2.0, 0)
        elif active_satellite_count() == 0:
            warning_label.text = "ALL SATELLITES DESTROYED"
            warning_label.pos = vector(0, 2.0, 0)

    earth.rotate(angle=EARTH_ROTATION_RATE * dt * 0.08, axis=vector(0, 0, 1))
