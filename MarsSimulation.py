from vpython import *
import json
import os
import time
import math
from datetime import datetime, timezone

# ============================================================
# Physics-Grounded Satellite / Debris / Live Command Demo
# ============================================================
# Realistic parts:
# - Real Earth radius
# - Real Earth gravitational parameter
# - Two-body orbital gravity
# - Circular orbit initialization with realistic orbital speeds
# - Breakup modeled as fragment cloud with small delta-v in vacuum
#
# Visually exaggerated parts:
# - Satellite size
# - Debris size
# - Collision radius
# - Brief flash at impact for visibility
# - Debris-to-debris collision radius is exaggerated so interactions are visible
#
# Live input:
# - Edit quantum_commands.json while the sim is running
# - No restart needed
# ============================================================


# -----------------------------
# Pre-simulation constellation input
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


print("\nSatellite constellation setup")
print("Enter how many satellites you want in each orbit class.")
print("Press Enter to use the default values.\n")

REQUESTED_LEO_SATELLITES = read_nonnegative_int("Number of LEO satellites", 2)
REQUESTED_MEO_SATELLITES = read_nonnegative_int("Number of MEO satellites", 1)
REQUESTED_HEO_SATELLITES = read_nonnegative_int("Number of HEO satellites", 1)

print("\nAsteroid hazard setup")
print("Enter how many asteroids you want in each orbit class.")
print("Default is 1 LEO asteroid so the collision demo still happens.\n")

REQUESTED_LEO_ASTEROIDS = read_nonnegative_int("Number of LEO asteroids", 1)
REQUESTED_MEO_ASTEROIDS = read_nonnegative_int("Number of MEO asteroids", 0)
REQUESTED_HEO_ASTEROIDS = read_nonnegative_int("Number of HEO asteroids", 0)

if REQUESTED_LEO_SATELLITES + REQUESTED_MEO_SATELLITES + REQUESTED_HEO_SATELLITES == 0:
    print("You entered 0 total satellites, so the sim will create one default LEO satellite so the demo still runs.")
    REQUESTED_LEO_SATELLITES = 1

print(
    f"\nCreating constellation: "
    f"{REQUESTED_LEO_SATELLITES} LEO, "
    f"{REQUESTED_MEO_SATELLITES} MEO, "
    f"{REQUESTED_HEO_SATELLITES} HEO satellites.\n"
)

print(
    f"Creating asteroid hazards: "
    f"{REQUESTED_LEO_ASTEROIDS} LEO, "
    f"{REQUESTED_MEO_ASTEROIDS} MEO, "
    f"{REQUESTED_HEO_ASTEROIDS} HEO asteroids.\n"
)

print("Mars satellite constellation setup")
print("Enter how many satellites you want around Mars in each orbit class.")
print("Mars uses accurate Mars radius and Mars gravity, with Mars-centered satellite orbits.\n")

REQUESTED_MARS_LOW_SATELLITES = read_nonnegative_int("Number of Mars low-orbit satellites", 2)
REQUESTED_MARS_MID_SATELLITES = read_nonnegative_int("Number of Mars medium-orbit satellites", 1)
REQUESTED_MARS_HIGH_SATELLITES = read_nonnegative_int("Number of Mars high-orbit satellites", 1)

print(
    f"\nCreating Mars constellation: "
    f"{REQUESTED_MARS_LOW_SATELLITES} low Mars orbit, "
    f"{REQUESTED_MARS_MID_SATELLITES} medium Mars orbit, "
    f"{REQUESTED_MARS_HIGH_SATELLITES} high Mars orbit satellites.\n"
)

print("Mars asteroid hazard setup")
print("Enter how many asteroid hazards you want around Mars in each orbit class.")
print("Default is 1 low Mars asteroid so the Mars collision/hazard demo can happen too.\n")

REQUESTED_MARS_LOW_ASTEROIDS = read_nonnegative_int("Number of Mars low-orbit asteroids", 1)
REQUESTED_MARS_MID_ASTEROIDS = read_nonnegative_int("Number of Mars medium-orbit asteroids", 0)
REQUESTED_MARS_HIGH_ASTEROIDS = read_nonnegative_int("Number of Mars high-orbit asteroids", 0)

print(
    f"\nCreating Mars asteroid hazards: "
    f"{REQUESTED_MARS_LOW_ASTEROIDS} low Mars orbit, "
    f"{REQUESTED_MARS_MID_ASTEROIDS} medium Mars orbit, "
    f"{REQUESTED_MARS_HIGH_ASTEROIDS} high Mars orbit asteroids.\n"
)

# -----------------------------
# Scene setup
# -----------------------------
scene.title = "Earth-Mars Satellite / Debris / Transfer Mission Simulation"
scene.width = 1200
scene.height = 800
scene.background = color.black
scene.forward = vector(-1, -0.35, -0.9)
scene.center = vector(0, 0, 0)
scene.range = 32000

scene.userspin = True
scene.userzoom = False  # custom scroll zoom below: zooms toward the cursor instead of only scene.center
scene.userpan = True

# -----------------------------
# Real physical constants
# -----------------------------
MU_EARTH = 3.986004418e14          # m^3/s^2
R_EARTH = 6371008.4                # m
EARTH_ROTATION_RATE = 7.2921159e-5 # rad/s

# Mars physical constants. Values are NASA/NSSDC-style standard planetary constants.
MU_MARS = 4.282837e13              # m^3/s^2
R_MARS = 3389500.0                 # m
MARS_ROTATION_RATE = 7.0882181e-5  # rad/s

# Higher-fidelity orbital physics options.
# J2 models planetary oblateness, which slowly precesses real orbits.
# Drag is only applied inside the upper atmosphere cutoff so high orbits and
# the Earth-to-Mars transfer graphic are not unrealistically slowed.
ENABLE_J2_PERTURBATION = True
ENABLE_ATMOSPHERIC_DRAG = True
J2_EARTH = 1.08262668e-3
J2_MARS = 1.96045e-3
DRAG_COEFFICIENT = 2.2
EARTH_SURFACE_DENSITY_KG_M3 = 1.225
MARS_SURFACE_DENSITY_KG_M3 = 0.020
EARTH_SCALE_HEIGHT_M = 8500.0
MARS_SCALE_HEIGHT_M = 11100.0
MAX_DRAG_ALTITUDE_M = 800000.0

# Heliocentric Earth-to-Mars transfer constants.
# This creates a physically scaled Earth-Mars geometry using circular heliocentric
# orbits and the classical Hohmann transfer phase angle.
MU_SUN = 1.32712440018e20          # m^3/s^2
AU_M = 149597870700.0              # m
EARTH_HELIOCENTRIC_RADIUS_M = AU_M
MARS_HELIOCENTRIC_RADIUS_M = 1.523679 * AU_M

TRANSFER_SEMI_MAJOR_AXIS_M = 0.5 * (EARTH_HELIOCENTRIC_RADIUS_M + MARS_HELIOCENTRIC_RADIUS_M)
TRANSFER_TIME_S = pi * sqrt(TRANSFER_SEMI_MAJOR_AXIS_M ** 3 / MU_SUN)
TRANSFER_TIME_DAYS = TRANSFER_TIME_S / 86400.0
MARS_MEAN_MOTION_RAD_S = sqrt(MU_SUN / MARS_HELIOCENTRIC_RADIUS_M ** 3)
EARTH_MARS_HOHMANN_PHASE_ANGLE_RAD = pi - MARS_MEAN_MOTION_RAD_S * TRANSFER_TIME_S
EARTH_MARS_HOHMANN_PHASE_ANGLE_DEG = degrees(EARTH_MARS_HOHMANN_PHASE_ANGLE_RAD)

SUN_POSITION_M = vector(-EARTH_HELIOCENTRIC_RADIUS_M, 0, 0)
EARTH_HELIOCENTRIC_POSITION_M = vector(EARTH_HELIOCENTRIC_RADIUS_M, 0, 0)
MARS_HELIOCENTRIC_POSITION_M = vector(
    MARS_HELIOCENTRIC_RADIUS_M * cos(EARTH_MARS_HOHMANN_PHASE_ANGLE_RAD),
    MARS_HELIOCENTRIC_RADIUS_M * sin(EARTH_MARS_HOHMANN_PHASE_ANGLE_RAD),
    0
)
MARS_POSITION_M = MARS_HELIOCENTRIC_POSITION_M - EARTH_HELIOCENTRIC_POSITION_M
EARTH_MARS_DISTANCE_M = mag(MARS_POSITION_M)
EARTH_MARS_DISTANCE_AU = EARTH_MARS_DISTANCE_M / AU_M

# The spacecraft path is drawn as a mission graphic between true-scale Earth and Mars
# positions. The NASA-style Hohmann transfer timing and delta-v stats are real,
# while the months-long mission is time-compressed so it can be watched live.
MARS_TRANSFER_VISUAL_DURATION_S = 75.0

VISUAL_SCALE = 1 / R_EARTH         # 1 scene unit = 1 Earth radius


def meters_to_scene(v):
    return v * VISUAL_SCALE


def scene_to_meters(v):
    return v / VISUAL_SCALE

# Physics timestep
BASE_DT = 5.0
dt = BASE_DT
rate_value = 120

# Fast-forward speed. 1x is normal, 2x advances physics twice as fast, etc.
SIMULATION_SPEED_OPTIONS = [1, 2, 4, 8]
simulation_speed_index = 0
simulation_speed_multiplier = SIMULATION_SPEED_OPTIONS[simulation_speed_index]

# Live command file
COMMAND_FILE = "quantum_commands.json"
COMMAND_CHECK_INTERVAL_FRAMES = 30

# Live telemetry output for the quantum routing / Mosquitto bridge system.
# The simulation continuously overwrites this JSON file with the newest full
# state snapshot. Your separate Eclipse Mosquitto publisher script can read
# this file and publish it to another computer.
TELEMETRY_OUTPUT_MODE = "json_file"
BASE_TELEMETRY_SAMPLE_HZ = 10.0
TELEMETRY_JSON_FILENAME = "satellite_sim_live_telemetry.json"
TELEMETRY_JSON_PATH = os.path.join(os.path.expanduser("~/Desktop"), TELEMETRY_JSON_FILENAME)
TELEMETRY_JSON_TEMP_PATH = TELEMETRY_JSON_PATH + ".tmp"
last_telemetry_file_write_time = 0.0


def effective_telemetry_sample_hz():
    return BASE_TELEMETRY_SAMPLE_HZ * simulation_speed_multiplier


def telemetry_export_interval_frames():
    return max(1, int(round(rate_value / effective_telemetry_sample_hz())))


def ensure_telemetry_output_folder():
    folder = os.path.dirname(TELEMETRY_JSON_PATH)
    if folder != "" and not os.path.exists(folder):
        os.makedirs(folder, exist_ok=True)


def write_live_telemetry_json(payload):
    # Atomic write pattern:
    # 1. write the complete JSON to a temporary file
    # 2. replace the live file in one operation
    # This keeps the Mosquitto publisher from reading half-written JSON.
    global last_telemetry_file_write_time

    ensure_telemetry_output_folder()

    file_payload = dict(payload)
    file_payload["telemetry_file"] = {
        "path": TELEMETRY_JSON_PATH,
        "updated_unix_time_s": float(time.time()),
        "updated_utc_iso": datetime.now(timezone.utc).isoformat(),
        "write_mode": "atomic_temp_file_replace",
        "intended_sample_hz": float(effective_telemetry_sample_hz()),
        "mosquitto_note": "Read this JSON file from a separate publisher script and publish it over MQTT."
    }

    with open(TELEMETRY_JSON_TEMP_PATH, "w") as f:
        json.dump(file_payload, f, indent=2)
        f.write("\n")

    os.replace(TELEMETRY_JSON_TEMP_PATH, TELEMETRY_JSON_PATH)
    last_telemetry_file_write_time = time.time()


print("Telemetry output mode:", TELEMETRY_OUTPUT_MODE)
print(f"Telemetry JSON file: {TELEMETRY_JSON_PATH}")
print(f"Telemetry will update that JSON file at {effective_telemetry_sample_hz():.1f} Hz.")

# Demo timing target for the asteroid
desired_visual_collision_time = 18.0

# -----------------------------
# UI labels
# -----------------------------
earth = sphere(
    pos=vector(0, 0, 0),
    radius=1,
    texture=textures.earth,
    shininess=0.4
)

earth_label = label(
    pos=vector(0, -1.30, 0),
    text="Earth",
    height=16,
    box=False,
    color=color.white
)

# True-size Mars placed at the physically scaled Earth-Mars Hohmann departure distance.
mars = sphere(
    pos=meters_to_scene(MARS_POSITION_M),
    radius=R_MARS / R_EARTH,
    color=vector(0.82, 0.28, 0.13),
    shininess=0.25
)

mars_label = label(
    pos=mars.pos + vector(0, -0.85, 0),
    text=(
        f"Mars | distance {EARTH_MARS_DISTANCE_AU:.3f} AU "
        f"({EARTH_MARS_DISTANCE_M / 1e9:.1f} million km)"
    ),
    height=16,
    box=False,
    color=color.red
)

sun_marker = sphere(
    pos=meters_to_scene(SUN_POSITION_M),
    radius=0.25,
    color=color.yellow,
    emissive=True
)

sun_label = label(
    pos=sun_marker.pos + vector(0, -0.7, 0),
    text="Sun direction marker (not true-size, for transfer reference)",
    height=12,
    box=False,
    color=color.yellow
)

transfer_label = label(
    pos=vector(-3.6, 2.18, 0),
    text=(
        f"Earth-Mars Hohmann transfer: {TRANSFER_TIME_DAYS:.1f} days | "
        f"phase angle {EARTH_MARS_HOHMANN_PHASE_ANGLE_DEG:.1f} deg"
    ),
    height=10,
    box=False,
    color=color.orange
)

warning_label = label(
    pos=vector(0, 2.0, 0),
    text="",
    height=16,
    box=False,
    color=color.red
)

timer_label = label(
    pos=vector(-3.6, 3.0, 0),
    text="Visual Time: 0.0 s | Physical Time: 0 s",
    height=12,
    box=False,
    color=color.white
)

physics_label = label(
    pos=vector(-3.6, 2.78, 0),
    text="Physics: J2 gravity + upper-atmosphere drag enabled",
    height=10,
    box=False,
    color=color.cyan
)

command_label = label(
    pos=vector(-3.6, 2.58, 0),
    text="Command input: waiting for quantum_commands.json",
    height=10,
    box=False,
    color=color.green
)

telemetry_label = label(
    pos=vector(-3.6, 2.40, 0),
    text=f"Telemetry JSON: {TELEMETRY_JSON_PATH} | {effective_telemetry_sample_hz():.1f} Hz",
    height=10,
    box=False,
    color=color.white
)

selected_label = label(
    pos=vector(0, 2.35, 0),
    text="Selected data satellite: none",
    height=12,
    box=False,
    color=color.yellow
)


summary_label = label(
    pos=vector(0, -2.35, 0),
    text="",
    height=12,
    box=True,
    border=8,
    opacity=0.18,
    color=color.white
)


# -----------------------------
# Cursor-centered zoom controls
# -----------------------------
# VPython's default scroll zoom usually zooms toward scene.center. That is painful
# in this Earth-Mars true-scale scene because Earth, Mars, satellites, and the
# transfer craft are separated by huge distances. This custom wheel handler moves
# the camera center toward the point under the cursor while changing scene.range.
# Result: scrolling over an object zooms into that object.
CUSTOM_CURSOR_ZOOM_ENABLED = True
ZOOM_IN_FACTOR = 0.82
ZOOM_OUT_FACTOR = 1.22
MIN_SCENE_RANGE = 0.20
MAX_SCENE_RANGE = 80000.0


def clamp_value(value, low, high):
    return max(low, min(high, value))


def get_cursor_focus_point():
    # Best case: the mouse is over a VPython object. Zoom into that object.
    try:
        picked = scene.mouse.pick
        if picked is not None and hasattr(picked, "pos"):
            return picked.pos
    except Exception:
        pass

    # Otherwise project the cursor onto the current camera plane through the
    # scene center. This still makes empty-space zoom feel cursor-directed.
    try:
        projected = scene.mouse.project(normal=scene.forward, point=scene.center)
        if projected is not None:
            return projected
    except Exception:
        pass

    return scene.center


def wheel_event_means_zoom_in(event):
    # Different VPython/browser builds expose wheel direction with different
    # names/signs, so this checks the common ones safely.
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

    # Fallback: if VPython gives no wheel direction, default to zooming in.
    return True


def zoom_to_cursor(event=None):
    if not CUSTOM_CURSOR_ZOOM_ENABLED:
        return

    focus_point = get_cursor_focus_point()
    zoom_factor = ZOOM_IN_FACTOR if wheel_event_means_zoom_in(event) else ZOOM_OUT_FACTOR

    old_center = scene.center
    new_range = clamp_value(scene.range * zoom_factor, MIN_SCENE_RANGE, MAX_SCENE_RANGE)

    # If range got clamped, recompute the effective factor so center movement
    # exactly matches the actual zoom amount.
    if scene.range != 0:
        effective_factor = new_range / scene.range
    else:
        effective_factor = zoom_factor

    scene.center = focus_point + (old_center - focus_point) * effective_factor
    scene.range = new_range


custom_zoom_bound = False
for wheel_event_name in ["wheel", "scroll"]:
    try:
        scene.bind(wheel_event_name, zoom_to_cursor)
        custom_zoom_bound = True
    except Exception:
        pass

if not custom_zoom_bound:
    print("Custom mouse-wheel zoom binding was not available in this VPython build.")


# Camera follow mode. Most view buttons are one-time camera jumps, but
# Spacecraft View is a live chase/follow camera so the ship does not leave
# the screen while it travels from Earth to Mars.
camera_follow_mode = "none"


def set_camera_follow_mode(mode):
    global camera_follow_mode
    camera_follow_mode = mode


def current_spacecraft_scene_position():
    # This function is defined before the spacecraft object is created, but it is
    # only called after setup is complete. The globals() checks keep camera logic
    # safe even if the spacecraft has already been destroyed.
    if "mars_transfer_spacecraft" in globals() and globals().get("mars_transfer_spacecraft_active", True):
        return mars_transfer_spacecraft.pos

    if "last_transfer_spacecraft_state" in globals():
        return meters_to_scene(last_transfer_spacecraft_state.get("position_m", vector(0, 0, 0)))

    return (earth.pos + mars.pos) / 2


def update_camera_follow():
    if camera_follow_mode == "spacecraft":
        scene.center = current_spacecraft_scene_position()


def focus_earth_view(button_event=None):
    set_camera_follow_mode("none")
    scene.center = earth.pos
    scene.range = 5.0


def focus_mars_view(button_event=None):
    set_camera_follow_mode("none")
    scene.center = mars.pos
    scene.range = 5.0


def focus_transfer_view(button_event=None):
    set_camera_follow_mode("none")
    scene.center = (earth.pos + mars.pos) / 2
    scene.range = max(10.0, mag(mars.pos - earth.pos) * 0.62)


def focus_spacecraft_view(button_event=None):
    set_camera_follow_mode("spacecraft")
    scene.center = current_spacecraft_scene_position()
    scene.range = 4.0


def focus_default_view(button_event=None):
    # Default true-scale mission view: shows the full Earth-to-Mars geometry.
    focus_transfer_view(button_event)


# -----------------------------
# Simulation control buttons
# -----------------------------
simulation_running = True
simulation_ended = False
final_summary_printed = False
simulation_physical_time = 0.0


def set_control_status(text, label_color=color.white):
    telemetry_label.text = text
    telemetry_label.color = label_color


def speed_status_text():
    return (
        f"speed {simulation_speed_multiplier}x | "
        f"telemetry JSON {effective_telemetry_sample_hz():.1f} Hz | "
        f"dt {dt:.1f} s"
    )


def refresh_running_status():
    if simulation_running and not simulation_ended:
        set_control_status(f"Simulation running | {speed_status_text()}", color.green)


def set_simulation_speed(multiplier):
    global simulation_speed_multiplier, dt
    simulation_speed_multiplier = multiplier
    dt = BASE_DT * simulation_speed_multiplier
    print(
        f"Fast forward set to {simulation_speed_multiplier}x | "
        f"telemetry sampling now {effective_telemetry_sample_hz():.1f} Hz | "
        f"physics dt now {dt:.1f} s"
    )
    refresh_running_status()


def fast_forward_simulation(button_event=None):
    global simulation_speed_index

    if simulation_ended:
        set_control_status("Simulation ended. Restart the Python file to run again.", color.red)
        return

    simulation_speed_index = (simulation_speed_index + 1) % len(SIMULATION_SPEED_OPTIONS)
    set_simulation_speed(SIMULATION_SPEED_OPTIONS[simulation_speed_index])



def start_simulation(button_event=None):
    global simulation_running, simulation_ended

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
    return sum(1 for debris in debris_particles if debris["active"])


def count_active_visual_events():
    return sum(1 for event in active_visual_events if len(event) > 0)


def build_final_summary(frame_count_value, visual_time_value, physical_time_value):
    active_satellite_names = [sat["name"] for sat in satellites if sat["active"]]
    destroyed_satellite_names = [sat["name"] for sat in satellites if not sat["active"]]
    active_debris_count = count_active_debris()
    if "asteroids" in globals():
        active_asteroid_count = sum(1 for ast in asteroids if ast["active"])
    else:
        active_asteroid_count = 1 if asteroid is not None and asteroid["active"] else 0
    active_mars_asteroid_count = sum(1 for ast in asteroids if ast["active"] and ast.get("central_body", "Earth") == "Mars") if "asteroids" in globals() else 0
    active_earth_asteroid_count = sum(1 for ast in asteroids if ast["active"] and ast.get("central_body", "Earth") != "Mars") if "asteroids" in globals() else 0
    active_hazard_count = active_asteroid_count + active_debris_count

    spacecraft_status = {
        "name": "MARS-XFER-1",
        "active": bool(globals().get("mars_transfer_spacecraft_active", True)),
        "destroyed": not bool(globals().get("mars_transfer_spacecraft_active", True)),
        "arrived_at_mars": bool(globals().get("mars_transfer_spacecraft_arrived", False)),
        "destroyed_by": globals().get("mars_transfer_spacecraft_destroyed_by", None),
        "mission_fraction": float(globals().get("last_transfer_spacecraft_state", {}).get("fraction", 0.0)),
        "mission_elapsed_days": float(globals().get("last_transfer_spacecraft_state", {}).get("elapsed_transfer_time_days", 0.0))
    }

    return {
        "schema": "satellite_simulation.final_summary.v1",
        "ended_at_unix_time_s": time.time(),
        "ended_at_utc_iso": datetime.now(timezone.utc).isoformat(),
        "frame": int(frame_count_value),
        "visual_time_s": float(visual_time_value),
        "physical_time_s": float(physical_time_value),
        "satellite_summary": {
            "starting_satellites": len(satellites),
            "satellites_left": len(active_satellite_names),
            "active_satellites": active_satellite_names,
            "destroyed_satellites": destroyed_satellite_names
        },
        "hazard_summary": {
            "active_asteroids": active_asteroid_count,
            "active_earth_asteroids": active_earth_asteroid_count,
            "active_mars_asteroids": active_mars_asteroid_count,
            "active_debris": active_debris_count,
            "total_active_hazards": active_hazard_count,
            "active_visual_impact_events": count_active_visual_events()
        },
        "spacecraft_summary": spacecraft_status,
        "system_summary": {
            "base_telemetry_sample_hz": BASE_TELEMETRY_SAMPLE_HZ,
            "effective_telemetry_sample_hz": effective_telemetry_sample_hz(),
            "speed_multiplier": simulation_speed_multiplier,
            "physics_timestep_s": dt,
            "base_physics_timestep_s": BASE_DT,
            "earth_mu_m3_s2": MU_EARTH,
            "earth_radius_m": R_EARTH,
            "earth_j2": J2_EARTH,
            "mars_mu_m3_s2": MU_MARS,
            "mars_radius_m": R_MARS,
            "mars_j2": J2_MARS,
            "j2_perturbation_enabled": ENABLE_J2_PERTURBATION,
            "atmospheric_drag_enabled": ENABLE_ATMOSPHERIC_DRAG,
            "drag_coefficient": DRAG_COEFFICIENT,
            "max_drag_altitude_m": MAX_DRAG_ALTITUDE_M,
            "earth_mars_distance_m": EARTH_MARS_DISTANCE_M,
            "earth_mars_distance_au": EARTH_MARS_DISTANCE_AU,
            "earth_mars_hohmann_transfer_time_days": TRANSFER_TIME_DAYS
        }
    }


def format_final_summary_for_screen(summary):
    sats_left = summary["satellite_summary"]["active_satellites"]
    sats_destroyed = summary["satellite_summary"]["destroyed_satellites"]

    sats_left_text = ", ".join(sats_left) if len(sats_left) > 0 else "none"
    sats_destroyed_text = ", ".join(sats_destroyed) if len(sats_destroyed) > 0 else "none"

    return (
        "SIMULATION ENDED\n"
        f"Visual time: {summary['visual_time_s']:.1f} s | Physical time: {summary['physical_time_s']:.0f} s\n"
        f"Satellites left: {summary['satellite_summary']['satellites_left']} / {summary['satellite_summary']['starting_satellites']} ({sats_left_text})\n"
        f"Destroyed satellites: {sats_destroyed_text}\n"
        f"Mars transfer spacecraft: {'active' if summary['spacecraft_summary']['active'] else 'destroyed'}"
        f" | arrived: {summary['spacecraft_summary']['arrived_at_mars']}"
        f" | progress: {100 * summary['spacecraft_summary']['mission_fraction']:.1f}%\n"
        f"Active asteroid hazards: {summary['hazard_summary']['active_asteroids']} "
        f"(Earth: {summary['hazard_summary']['active_earth_asteroids']}, Mars: {summary['hazard_summary']['active_mars_asteroids']})\n"
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
    current_physical_time = simulation_physical_time
    summary = build_final_summary(frame_count, current_visual_time, current_physical_time)

    summary_label.text = format_final_summary_for_screen(summary)
    summary_label.color = color.white
    warning_label.text = "SIMULATION ENDED - FINAL SUMMARY GENERATED"
    warning_label.pos = vector(0, 2.0, 0)
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
button(text="Mars View", bind=focus_mars_view)
scene.append_to_caption("  ")
button(text="Transfer View", bind=focus_transfer_view)
scene.append_to_caption("  ")
button(text="Spacecraft View", bind=focus_spacecraft_view)
scene.append_to_caption("  ")
button(text="Default View", bind=focus_default_view)
scene.append_to_caption("\nScroll over an object to zoom toward your cursor. Spacecraft View now follows MARS-XFER-1 live. Earth/Mars/Transfer/Default buttons are preserved. Shift-drag still pans, Ctrl/right-drag still rotates.\n")

# -----------------------------
# Utility
# -----------------------------
def meters_to_scene(v):
    return v * VISUAL_SCALE


def scene_to_meters(v):
    return v / VISUAL_SCALE


def circular_speed(radius_m):
    return sqrt(MU_EARTH / radius_m)


def gravity_acceleration_from_body(relative_position_m, mu):
    r = mag(relative_position_m)
    if r == 0:
        return vector(0, 0, 0)
    return -mu * relative_position_m / (r ** 3)


def get_gravity_acc_j2(rel_pos, mu, radius, j2_coeff):
    """Return point-mass gravity plus the J2 oblateness correction.

    rel_pos is measured from the planet center to the object in meters.
    The planet spin axis is assumed to be the scene z-axis, which matches the
    current Earth/Mars visual setup.
    """
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
        rel_pos.z * (5 * (z ** 2 / r2) - 3)
    )

    return acc_point + factor * j2_acc


def get_drag_acc(rel_pos, rel_vel, mass_kg, area_m2, body="Earth"):
    """Return atmospheric-drag acceleration using a simple exponential model.

    rel_pos is measured from the planet center. rel_vel should be the object's
    velocity relative to the rotating atmosphere. Drag is disabled above
    MAX_DRAG_ALTITUDE_M to avoid unrealistic drag in medium/high/interplanetary
    space.
    """
    if not ENABLE_ATMOSPHERIC_DRAG:
        return vector(0, 0, 0)

    if mass_kg is None or mass_kg <= 0 or area_m2 is None or area_m2 <= 0:
        return vector(0, 0, 0)

    if body == "Mars":
        planet_radius = R_MARS
        rho0 = MARS_SURFACE_DENSITY_KG_M3
        scale_height = MARS_SCALE_HEIGHT_M
    else:
        planet_radius = R_EARTH
        rho0 = EARTH_SURFACE_DENSITY_KG_M3
        scale_height = EARTH_SCALE_HEIGHT_M

    altitude = mag(rel_pos) - planet_radius

    if altitude < 0 or altitude > MAX_DRAG_ALTITUDE_M:
        return vector(0, 0, 0)

    rho = rho0 * math.exp(-altitude / scale_height)
    v_mag = mag(rel_vel)

    if v_mag == 0 or rho <= 0:
        return vector(0, 0, 0)

    drag_mag = (0.5 * rho * (v_mag ** 2) * DRAG_COEFFICIENT * area_m2) / mass_kg
    return -drag_mag * norm(rel_vel)


def central_body_constants(body):
    if body == "Mars":
        return {
            "name": "Mars",
            "mu": MU_MARS,
            "radius": R_MARS,
            "j2": J2_MARS,
            "position": MARS_POSITION_M,
            "rotation_rate": MARS_ROTATION_RATE
        }

    return {
        "name": "Earth",
        "mu": MU_EARTH,
        "radius": R_EARTH,
        "j2": J2_EARTH,
        "position": vector(0, 0, 0),
        "rotation_rate": EARTH_ROTATION_RATE
    }


def cross_section_area_from_radius(radius_m):
    if radius_m is None or radius_m <= 0:
        return None
    return pi * radius_m ** 2


def object_cross_section_area_m2(obj, default_radius_m=1.0):
    if "drag_area_m2" in obj:
        return obj["drag_area_m2"]

    radius_m = obj.get("physical_radius_m", default_radius_m)
    return cross_section_area_from_radius(radius_m)


def physics_acceleration_for_object(position_m, velocity_mps, central_body="Earth", mass_kg=None, area_m2=None):
    body = central_body_constants(central_body)
    rel_pos = position_m - body["position"]

    gravity = get_gravity_acc_j2(
        rel_pos,
        body["mu"],
        body["radius"],
        body["j2"]
    )

    # Approximate atmosphere co-rotation. This gives drag relative to the air,
    # not just relative to the inertial scene.
    atmosphere_velocity_mps = cross(vector(0, 0, body["rotation_rate"]), rel_pos)
    relative_atmosphere_velocity_mps = velocity_mps - atmosphere_velocity_mps

    drag = get_drag_acc(
        rel_pos,
        relative_atmosphere_velocity_mps,
        mass_kg,
        area_m2,
        body=body["name"]
    )

    return gravity + drag


def gravity_acceleration(position_m, velocity_mps=vector(0, 0, 0), mass_kg=None, area_m2=None):
    return physics_acceleration_for_object(position_m, velocity_mps, "Earth", mass_kg, area_m2)


def mars_gravity_acceleration(position_m, velocity_mps=vector(0, 0, 0), mass_kg=None, area_m2=None):
    return physics_acceleration_for_object(position_m, velocity_mps, "Mars", mass_kg, area_m2)


def altitude_m(position_m, central_body_position_m=vector(0, 0, 0), central_body_radius_m=R_EARTH):
    return mag(position_m - central_body_position_m) - central_body_radius_m


def vector_to_dict(v):
    return {
        "x": float(v.x),
        "y": float(v.y),
        "z": float(v.z)
    }


def speed_mps(velocity_mps):
    return float(mag(velocity_mps))


def orbital_energy_j_per_kg(position_m, velocity_mps):
    # Specific orbital energy epsilon = v^2 / 2 - mu / r.
    r = mag(position_m)
    if r == 0:
        return 0.0
    return float(0.5 * mag(velocity_mps) ** 2 - MU_EARTH / r)


def object_state_dict(object_id, object_type, active, position_m, velocity_mps, mass_kg=None, radius_m=None, selected_for_data=False, measurement_timestamp=None, extra=None, central_body_name="Earth", central_body_position_m=vector(0, 0, 0), central_body_radius_m=R_EARTH):
    altitude = altitude_m(position_m, central_body_position_m, central_body_radius_m)
    state = {
        "id": object_id,
        "type": object_type,
        "active": bool(active),
        "selected_for_data": bool(selected_for_data),
        "position_m_eci": vector_to_dict(position_m),
        "velocity_mps_eci": vector_to_dict(velocity_mps),
        "speed_mps": speed_mps(velocity_mps),
        "distance_from_earth_center_m": float(mag(position_m)),
        "altitude_m": float(altitude),
        "altitude_km": float(altitude / 1000.0),
        "specific_orbital_energy_j_per_kg": orbital_energy_j_per_kg(position_m - central_body_position_m, velocity_mps),
        "central_body": central_body_name
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


def random_unit_vector():
    d = vector(random() - 0.5, random() - 0.5, random() - 0.5)
    if mag(d) == 0:
        return vector(1, 0, 0)
    return norm(d)


def perpendicular_velocity_dir(position_m, velocity_mps):
    radial_dir = norm(position_m)
    tangential = velocity_mps - dot(velocity_mps, radial_dir) * radial_dir

    if mag(tangential) == 0:
        temp = cross(vector(0, 0, 1), radial_dir)

        if mag(temp) == 0:
            temp = cross(vector(0, 1, 0), radial_dir)

        return norm(temp)

    return norm(tangential)


def make_orbit_state(altitude, inclination_deg=0, raan_deg=0, phase_deg=0, prograde=True):
    radius_m = R_EARTH + altitude
    pos = vector(radius_m, 0, 0)
    vel = vector(0, circular_speed(radius_m), 0)

    if not prograde:
        vel = -vel

    # Rotate satellite around its orbit
    pos = rotate(pos, angle=radians(phase_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(phase_deg), axis=vector(0, 0, 1))

    # Tilt orbital plane
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))

    # Rotate entire orbit around Earth
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))

    return pos, vel


def draw_circular_orbit(altitude, orbit_color, inclination_deg=0, raan_deg=0):
    radius_m = R_EARTH + altitude
    points = []
    num_points = 720

    for i in range(num_points + 1):
        theta = 2 * pi * i / num_points
        p = vector(radius_m * cos(theta), radius_m * sin(theta), 0)

        p = rotate(p, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        p = rotate(p, angle=radians(raan_deg), axis=vector(0, 0, 1))

        points.append(meters_to_scene(p))

    return curve(
        pos=points,
        color=orbit_color,
        radius=0.006
    )


def make_elliptical_orbit_state(perigee_altitude, apogee_altitude, inclination_deg=63.4, raan_deg=0, argument_of_perigee_deg=270, true_anomaly_deg=0, prograde=True):
    # Keplerian two-body initial state using the vis-viva/perifocal equations.
    # This is useful for HEO, where the orbit is intentionally highly elliptical.
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
    num_points = 720

    for i in range(num_points + 1):
        nu = 2 * pi * i / num_points
        r = p_orbit / (1 + e * cos(nu))
        pos = vector(r * cos(nu), r * sin(nu), 0)

        pos = rotate(pos, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
        pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))

        points.append(meters_to_scene(pos))

    return curve(
        pos=points,
        color=orbit_color,
        radius=0.006
    )

# -----------------------------
# Live command helpers
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
        "data_collection_target": {
            "name": "Nashville target",
            "lat_deg": 36.1627,
            "lon_deg": -86.7816
        },
        "maneuvers": [
            {
                "satellite": "SAT-2",
                "type": "radial_out",
                "delta_v_mps": 0
            }
        ]
    }

    try:
        with open(COMMAND_FILE, "w") as f:
            json.dump(sample, f, indent=2)
    except Exception:
        pass


def lat_lon_to_position(lat_deg, lon_deg):
    lat = radians(lat_deg)
    lon = radians(lon_deg)

    x = cos(lat) * cos(lon)
    y = cos(lat) * sin(lon)
    z = sin(lat)

    return vector(x, y, z)


def update_data_target_marker(target_data):
    global target_marker, target_label

    if target_data is None:
        return

    try:
        name = target_data.get("name", "Data target")
        lat_deg = float(target_data.get("lat_deg", 0))
        lon_deg = float(target_data.get("lon_deg", 0))
    except Exception:
        return

    surface_pos = lat_lon_to_position(lat_deg, lon_deg) * 1.025

    if target_marker is None:
        target_marker = sphere(
            pos=surface_pos,
            radius=0.03,
            color=color.yellow,
            emissive=True
        )

        target_label = label(
            pos=surface_pos + vector(0.05, 0.05, 0),
            text=name,
            height=10,
            box=False,
            color=color.yellow
        )
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

    if mag(plane_dir) == 0:
        plane_dir = vector(0, 0, 1)
    else:
        plane_dir = norm(plane_dir)

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


def apply_command_data(command_data, satellites):
    selected_name = command_data.get("selected_satellite", None)

    for sat in satellites:
        sat["selected_for_data"] = (sat["name"] == selected_name)
        set_satellite_highlight(sat, sat["selected_for_data"])

    if selected_name is None:
        selected_label.text = "Selected data satellite: none"
    else:
        selected_label.text = f"Selected data satellite: {selected_name}"

    update_data_target_marker(command_data.get("data_collection_target", None))

    maneuvers = command_data.get("maneuvers", [])

    for maneuver in maneuvers:
        sat_name = maneuver.get("satellite", "")
        maneuver_type = maneuver.get("type", "")
        delta_v_mps = float(maneuver.get("delta_v_mps", 0))

        if abs(delta_v_mps) <= 0:
            continue

        for sat in satellites:
            if sat["name"] == sat_name:
                apply_delta_v(sat, maneuver_type, delta_v_mps)
                warning_label.text = f"Quantum command: {sat_name} {maneuver_type} delta-v={delta_v_mps:.1f} m/s"
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

        if text.strip() == "":
            last_command_mtime = mtime
            return

        if text == last_command_text:
            last_command_mtime = mtime
            return

        data = json.loads(text)
        apply_command_data(data, satellites)

        last_command_mtime = mtime
        last_command_text = text
        command_label.text = "Command input: live update applied"

    except Exception:
        command_label.text = "Command input: JSON error or unreadable file"

# -----------------------------
# Satellite system
# -----------------------------
def create_satellite_from_state(name, position_m, velocity_mps, sat_color, trail_color, orbit_class="custom", orbit_description="custom orbit", central_body="Earth"):
    marker = sphere(
        pos=meters_to_scene(position_m),
        radius=0.045,
        color=sat_color,
        make_trail=True,
        trail_color=trail_color,
        retain=1800
    )
    marker.trail_radius = 0.006

    body = box(
        pos=marker.pos,
        length=0.13,
        height=0.055,
        width=0.055,
        color=color.white
    )

    panel_left = box(
        pos=marker.pos + vector(0, 0.09, 0),
        length=0.22,
        height=0.018,
        width=0.006,
        color=vector(0.03, 0.10, 0.40)
    )

    panel_right = box(
        pos=marker.pos + vector(0, -0.09, 0),
        length=0.22,
        height=0.018,
        width=0.006,
        color=vector(0.03, 0.10, 0.40)
    )

    sat_label = label(
        pos=marker.pos + vector(0.13, 0.13, 0),
        text=name,
        height=11,
        box=False,
        color=sat_color
    )

    return {
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
        "central_body": central_body
    }


def create_satellite(name, altitude, inclination_deg, raan_deg, phase_deg, sat_color, trail_color, prograde=True, orbit_class="circular", orbit_description=None):
    position_m, velocity_mps = make_orbit_state(
        altitude=altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        phase_deg=phase_deg,
        prograde=prograde
    )

    if orbit_description is None:
        orbit_description = f"circular orbit, altitude {altitude / 1000.0:.0f} km"

    return create_satellite_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        sat_color=sat_color,
        trail_color=trail_color,
        orbit_class=orbit_class,
        orbit_description=orbit_description
    )


def create_heo_satellite(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, sat_color, trail_color):
    position_m, velocity_mps = make_elliptical_orbit_state(
        perigee_altitude=perigee_altitude,
        apogee_altitude=apogee_altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        argument_of_perigee_deg=argument_of_perigee_deg,
        true_anomaly_deg=true_anomaly_deg
    )

    return create_satellite_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        sat_color=sat_color,
        trail_color=trail_color,
        orbit_class="HEO",
        orbit_description=(
            f"highly elliptical orbit, perigee {perigee_altitude / 1000.0:.0f} km, "
            f"apogee {apogee_altitude / 1000.0:.0f} km"
        )
    )



# -----------------------------
# Mars satellite system
# -----------------------------
def make_mars_orbit_state(altitude, inclination_deg=0, raan_deg=0, phase_deg=0, prograde=True):
    radius_m = R_MARS + altitude
    pos = vector(radius_m, 0, 0)
    vel = vector(0, sqrt(MU_MARS / radius_m), 0)

    if not prograde:
        vel = -vel

    pos = rotate(pos, angle=radians(phase_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(phase_deg), axis=vector(0, 0, 1))
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))

    return MARS_POSITION_M + pos, vel


def make_mars_elliptical_orbit_state(perigee_altitude, apogee_altitude, inclination_deg=63.0, raan_deg=0, argument_of_perigee_deg=270, true_anomaly_deg=0, prograde=True):
    rp = R_MARS + perigee_altitude
    ra = R_MARS + apogee_altitude
    a = 0.5 * (rp + ra)
    e = (ra - rp) / (ra + rp)
    p_orbit = a * (1 - e ** 2)

    nu = radians(true_anomaly_deg)
    r = p_orbit / (1 + e * cos(nu))

    pos = vector(r * cos(nu), r * sin(nu), 0)
    velocity_factor = sqrt(MU_MARS / p_orbit)
    vel = vector(-sin(nu), e + cos(nu), 0) * velocity_factor

    if not prograde:
        vel = -vel

    pos = rotate(pos, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))

    return MARS_POSITION_M + pos, vel


def draw_mars_circular_orbit(altitude, orbit_color, inclination_deg=0, raan_deg=0):
    radius_m = R_MARS + altitude
    points = []
    num_points = 720

    for i in range(num_points + 1):
        theta = 2 * pi * i / num_points
        p = vector(radius_m * cos(theta), radius_m * sin(theta), 0)
        p = rotate(p, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        p = rotate(p, angle=radians(raan_deg), axis=vector(0, 0, 1))
        points.append(meters_to_scene(MARS_POSITION_M + p))

    return curve(pos=points, color=orbit_color, radius=0.006)


def draw_mars_elliptical_orbit(perigee_altitude, apogee_altitude, orbit_color, inclination_deg=63.0, raan_deg=0, argument_of_perigee_deg=270):
    rp = R_MARS + perigee_altitude
    ra = R_MARS + apogee_altitude
    a = 0.5 * (rp + ra)
    e = (ra - rp) / (ra + rp)
    p_orbit = a * (1 - e ** 2)
    points = []
    num_points = 720

    for i in range(num_points + 1):
        nu = 2 * pi * i / num_points
        r = p_orbit / (1 + e * cos(nu))
        pos = vector(r * cos(nu), r * sin(nu), 0)
        pos = rotate(pos, angle=radians(argument_of_perigee_deg), axis=vector(0, 0, 1))
        pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
        pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
        points.append(meters_to_scene(MARS_POSITION_M + pos))

    return curve(pos=points, color=orbit_color, radius=0.006)


def create_mars_satellite(name, altitude, inclination_deg, raan_deg, phase_deg, sat_color, trail_color, orbit_class="Mars Low", orbit_description=None):
    position_m, velocity_mps = make_mars_orbit_state(
        altitude=altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        phase_deg=phase_deg,
        prograde=True
    )

    if orbit_description is None:
        orbit_description = f"Mars circular orbit, altitude {altitude / 1000.0:.0f} km"

    return create_satellite_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        sat_color=sat_color,
        trail_color=trail_color,
        orbit_class=orbit_class,
        orbit_description=orbit_description,
        central_body="Mars"
    )


def create_mars_high_satellite(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, sat_color, trail_color):
    position_m, velocity_mps = make_mars_elliptical_orbit_state(
        perigee_altitude=perigee_altitude,
        apogee_altitude=apogee_altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        argument_of_perigee_deg=argument_of_perigee_deg,
        true_anomaly_deg=true_anomaly_deg
    )

    return create_satellite_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        sat_color=sat_color,
        trail_color=trail_color,
        orbit_class="Mars High",
        orbit_description=(
            f"Mars high elliptical orbit, perigee {perigee_altitude / 1000.0:.0f} km, "
            f"apogee {apogee_altitude / 1000.0:.0f} km"
        ),
        central_body="Mars"
    )

def update_satellite_physics(sat):
    if not sat["active"]:
        return

    central_body = sat.get("central_body", "Earth")

    area_m2 = object_cross_section_area_m2(sat, default_radius_m=1.5)

    sat["velocity_mps"] += physics_acceleration_for_object(
        sat["position_m"],
        sat["velocity_mps"],
        central_body,
        sat.get("mass_kg", 500.0),
        area_m2
    ) * dt
    sat["position_m"] += sat["velocity_mps"] * dt

    if central_body == "Mars":
        if mag(sat["position_m"] - MARS_POSITION_M) <= R_MARS:
            hide_satellite(sat)
    else:
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


def hide_satellite(sat):
    sat["active"] = False
    sat["marker"].make_trail = False
    sat["marker"].clear_trail()
    sat["marker"].visible = False
    sat["body"].visible = False
    sat["panel_left"].visible = False
    sat["panel_right"].visible = False
    sat["label"].visible = False

# -----------------------------
# Asteroid
# -----------------------------
def create_asteroid_from_state(name, position_m, velocity_mps, orbit_class, orbit_description, asteroid_color=color.red, central_body="Earth"):
    marker = sphere(
        pos=meters_to_scene(position_m),
        radius=0.075,
        color=asteroid_color,
        emissive=True,
        make_trail=True,
        trail_color=asteroid_color,
        retain=600
    )
    marker.trail_radius = 0.006

    asteroid_label = label(
        pos=marker.pos + vector(0.14, 0.14, 0),
        text=name,
        height=11,
        box=False,
        color=asteroid_color
    )

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
        "central_body": central_body
    }


def create_physical_asteroid(target_sat, target_collision_visual_time, name="AST-1"):
    # Guaranteed collision-course asteroid.
    #
    # Instead of giving the asteroid a random orbit, this places it on the same
    # circular orbital plane/radius as the target satellite, but with the opposite
    # tangential velocity. In ideal two-body gravity, the satellite and asteroid
    # are on the same circle moving toward each other, so their paths must intersect.
    # The starting angular separation is chosen so the impact happens later in the
    # run instead of immediately.
    #
    # This is still a demo threat: real mission planning would use tracking data
    # and orbit determination, but this is physically valid enough for a live
    # collision demonstration.
    target_position_m = target_sat["position_m"]
    target_velocity_mps = target_sat["velocity_mps"]
    target_radius_m = mag(target_position_m)

    target_speed_mps = circular_speed(target_radius_m)
    omega_rad_per_s = target_speed_mps / target_radius_m

    target_physical_time = target_collision_visual_time * rate_value * BASE_DT
    separation_angle = (2.0 * omega_rad_per_s * target_physical_time) % (2.0 * pi)

    orbit_normal = cross(target_position_m, target_velocity_mps)
    if mag(orbit_normal) == 0:
        orbit_normal = vector(0, 0, 1)
    else:
        orbit_normal = norm(orbit_normal)

    # Place the asteroid ahead of the satellite along the satellite's direction
    # of motion, then give it the opposite velocity.
    asteroid_position_m = rotate(
        target_position_m,
        angle=separation_angle,
        axis=orbit_normal
    )

    prograde_dir_at_asteroid = cross(orbit_normal, norm(asteroid_position_m))
    if mag(prograde_dir_at_asteroid) == 0:
        prograde_dir_at_asteroid = perpendicular_velocity_dir(asteroid_position_m, target_velocity_mps)
    else:
        prograde_dir_at_asteroid = norm(prograde_dir_at_asteroid)

    asteroid_velocity_mps = -prograde_dir_at_asteroid * target_speed_mps

    asteroid = create_asteroid_from_state(
        name=name,
        position_m=asteroid_position_m,
        velocity_mps=asteroid_velocity_mps,
        orbit_class=target_sat.get("orbit_class", "LEO"),
        orbit_description=(
            f"guaranteed retrograde collision-course orbit targeting {target_sat['name']}; "
            f"predicted impact in about {target_collision_visual_time:.1f} visual seconds"
        ),
        asteroid_color=color.red
    )

    asteroid["target_satellite"] = target_sat["name"]
    asteroid["predicted_collision_visual_time_s"] = target_collision_visual_time
    asteroid["predicted_collision_physical_time_s"] = target_physical_time
    asteroid["guaranteed_collision_course"] = True

    print(
        f"{name} placed on guaranteed collision course with {target_sat['name']} "
        f"in about {target_collision_visual_time:.1f} visual seconds "
        f"({target_physical_time:.0f} physical seconds)."
    )

    return asteroid


def create_circular_asteroid(name, altitude, inclination_deg, raan_deg, phase_deg, orbit_class, prograde=False):
    position_m, velocity_mps = make_orbit_state(
        altitude=altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        phase_deg=phase_deg,
        prograde=prograde
    )

    return create_asteroid_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        orbit_class=orbit_class,
        orbit_description=f"{orbit_class} circular asteroid orbit, altitude {altitude / 1000.0:.0f} km",
        asteroid_color=color.red
    )


def create_heo_asteroid(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, prograde=False):
    position_m, velocity_mps = make_elliptical_orbit_state(
        perigee_altitude=perigee_altitude,
        apogee_altitude=apogee_altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        argument_of_perigee_deg=argument_of_perigee_deg,
        true_anomaly_deg=true_anomaly_deg,
        prograde=prograde
    )

    return create_asteroid_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        orbit_class="HEO",
        orbit_description=(
            f"HEO elliptical asteroid orbit, perigee {perigee_altitude / 1000.0:.0f} km, "
            f"apogee {apogee_altitude / 1000.0:.0f} km"
        ),
        asteroid_color=color.red
    )


def create_physical_mars_asteroid(target_sat, target_collision_visual_time, name="MARS-AST-1"):
    # Guaranteed Mars-centered collision-course asteroid. This mirrors the Earth
    # demo asteroid, but all geometry and gravity assumptions are Mars-centered.
    target_local_position_m = target_sat["position_m"] - MARS_POSITION_M
    target_velocity_mps = target_sat["velocity_mps"]
    target_radius_m = mag(target_local_position_m)

    target_speed_mps = sqrt(MU_MARS / target_radius_m)
    omega_rad_per_s = target_speed_mps / target_radius_m

    target_physical_time = target_collision_visual_time * rate_value * BASE_DT
    separation_angle = (2.0 * omega_rad_per_s * target_physical_time) % (2.0 * pi)

    orbit_normal = cross(target_local_position_m, target_velocity_mps)
    if mag(orbit_normal) == 0:
        orbit_normal = vector(0, 0, 1)
    else:
        orbit_normal = norm(orbit_normal)

    asteroid_local_position_m = rotate(
        target_local_position_m,
        angle=separation_angle,
        axis=orbit_normal
    )

    prograde_dir_at_asteroid = cross(orbit_normal, norm(asteroid_local_position_m))
    if mag(prograde_dir_at_asteroid) == 0:
        prograde_dir_at_asteroid = perpendicular_velocity_dir(asteroid_local_position_m, target_velocity_mps)
    else:
        prograde_dir_at_asteroid = norm(prograde_dir_at_asteroid)

    asteroid_velocity_mps = -prograde_dir_at_asteroid * target_speed_mps
    asteroid_position_m = MARS_POSITION_M + asteroid_local_position_m

    asteroid = create_asteroid_from_state(
        name=name,
        position_m=asteroid_position_m,
        velocity_mps=asteroid_velocity_mps,
        orbit_class=target_sat.get("orbit_class", "Mars Low"),
        orbit_description=(
            f"guaranteed Mars-centered retrograde collision-course orbit targeting {target_sat['name']}; "
            f"predicted impact in about {target_collision_visual_time:.1f} visual seconds"
        ),
        asteroid_color=vector(1.0, 0.22, 0.06),
        central_body="Mars"
    )

    asteroid["target_satellite"] = target_sat["name"]
    asteroid["predicted_collision_visual_time_s"] = target_collision_visual_time
    asteroid["predicted_collision_physical_time_s"] = target_physical_time
    asteroid["guaranteed_collision_course"] = True

    print(
        f"{name} placed on guaranteed Mars collision course with {target_sat['name']} "
        f"in about {target_collision_visual_time:.1f} visual seconds "
        f"({target_physical_time:.0f} physical seconds)."
    )

    return asteroid


def create_mars_circular_asteroid(name, altitude, inclination_deg, raan_deg, phase_deg, orbit_class, prograde=False):
    position_m, velocity_mps = make_mars_orbit_state(
        altitude=altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        phase_deg=phase_deg,
        prograde=prograde
    )

    return create_asteroid_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        orbit_class=orbit_class,
        orbit_description=f"{orbit_class} Mars-centered asteroid orbit, altitude {altitude / 1000.0:.0f} km",
        asteroid_color=vector(1.0, 0.22, 0.06),
        central_body="Mars"
    )


def create_mars_high_asteroid(name, perigee_altitude, apogee_altitude, inclination_deg, raan_deg, argument_of_perigee_deg, true_anomaly_deg, prograde=False):
    position_m, velocity_mps = make_mars_elliptical_orbit_state(
        perigee_altitude=perigee_altitude,
        apogee_altitude=apogee_altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        argument_of_perigee_deg=argument_of_perigee_deg,
        true_anomaly_deg=true_anomaly_deg,
        prograde=prograde
    )

    return create_asteroid_from_state(
        name=name,
        position_m=position_m,
        velocity_mps=velocity_mps,
        orbit_class="Mars High",
        orbit_description=(
            f"Mars high elliptical asteroid orbit, perigee {perigee_altitude / 1000.0:.0f} km, "
            f"apogee {apogee_altitude / 1000.0:.0f} km"
        ),
        asteroid_color=vector(1.0, 0.22, 0.06),
        central_body="Mars"
    )


def update_asteroid(asteroid):
    if not asteroid["active"]:
        return

    central_body = asteroid.get("central_body", "Earth")

    area_m2 = object_cross_section_area_m2(asteroid, default_radius_m=2.0)

    asteroid["velocity_mps"] += physics_acceleration_for_object(
        asteroid["position_m"],
        asteroid["velocity_mps"],
        central_body,
        asteroid.get("mass_kg", 1000.0),
        area_m2
    ) * dt
    asteroid["position_m"] += asteroid["velocity_mps"] * dt

    if central_body == "Mars":
        if mag(asteroid["position_m"] - MARS_POSITION_M) <= R_MARS:
            hide_object(asteroid)
            return
    else:
        if mag(asteroid["position_m"]) <= R_EARTH:
            hide_object(asteroid)
            return

    p = meters_to_scene(asteroid["position_m"])
    asteroid["marker"].pos = p
    asteroid["label"].pos = p + vector(0.14, 0.14, 0)


def hide_object(obj):
    obj["active"] = False
    obj["marker"].visible = False

    if obj["label"] is not None:
        obj["label"].visible = False

# -----------------------------
# Space breakup / debris model
# -----------------------------
# In space, there is no atmospheric fireball or giant shockwave sphere.
# A realistic visual is:
# - short flash
# - fragment cloud
# - fragments keep original orbital velocity plus breakup delta-v
#
# This version keeps the debris field cleaner:
# - fewer fragments per breakup
# - no debris trails
# - fragments can interact with other fragments
# - debris-debris impacts use a simplified NASA-style catastrophic threshold
#
# NASA Standard Breakup Model style idea:
# - catastrophic breakup occurs when impact kinetic energy per target mass
#   is above about 40 J/g = 40,000 J/kg.
# - below that, fragments mostly deflect / exchange momentum instead of
#   creating a huge new cloud.
#
# This is still a VPython demo, not a full orbital-debris propagation tool.

INITIAL_BREAKUP_FRAGMENTS = 45
SECONDARY_BREAKUP_MIN_FRAGMENTS = 2
SECONDARY_BREAKUP_MAX_FRAGMENTS = 5
MAX_DEBRIS_PARTICLES = 180

# 40 J/g is commonly used in NASA Standard Breakup Model discussions
# as the catastrophic collision energy-to-mass threshold.
CATASTROPHIC_ENERGY_J_PER_KG = 40000.0

# VPython-visible debris interaction distance. Real debris pieces are tiny,
# but using literal centimeter-to-meter sizes would make interactions invisible.
debris_debris_collision_distance_m = 90000.0
MAX_DEBRIS_DEBRIS_COLLISIONS_PER_FRAME = 2


def random_debris_mass_kg():
    # Small-to-medium visible fragments. This is not every paint fleck; it is
    # the subset we choose to draw in the demo.
    return 0.25 + random() * 8.0


def random_debris_radius_scene(mass_kg):
    # Smaller visual radius for a cleaner scene. Radius is display-only.
    return 0.006 + min(0.010, 0.0015 * sqrt(mass_kg))


def central_body_for_position(position_m):
    # Breakups near Mars should create Mars-orbit debris, while Earth breakups
    # stay Earth-orbit debris. This keeps Mars collisions from immediately using
    # Earth gravity just because the whole scene uses an Earth-centered frame.
    distance_to_mars = mag(position_m - MARS_POSITION_M)
    distance_to_earth = mag(position_m)

    if distance_to_mars < 20.0 * R_MARS and distance_to_mars < distance_to_earth:
        return "Mars"

    return "Earth"


def create_breakup_event(position_m, base_velocity_mps):
    visual_parts = []
    debris_parts = []

    scene_pos = meters_to_scene(position_m)

    # Brief impact flash
    flash = sphere(
        pos=scene_pos,
        radius=0.05,
        color=color.white,
        emissive=True,
        opacity=0.95
    )

    visual_parts.append({
        "obj": flash,
        "growth": 0.012,
        "shrink_factor": 0.93,
        "life": 18,
        "max_life": 18,
        "grow_for": 5,
        "start_opacity": 0.95
    })

    # A faint larger flash so the collision is easier to notice
    glow = sphere(
        pos=scene_pos,
        radius=0.08,
        color=color.orange,
        emissive=True,
        opacity=0.35
    )

    visual_parts.append({
        "obj": glow,
        "growth": 0.010,
        "shrink_factor": 0.94,
        "life": 26,
        "max_life": 26,
        "grow_for": 7,
        "start_opacity": 0.35
    })

    num_fragments = INITIAL_BREAKUP_FRAGMENTS
    debris_central_body = central_body_for_position(position_m)

    for i in range(num_fragments):
        direction = random_unit_vector()

        # Wider physical starting spread, still tiny compared with orbital radius.
        start_offset_m = 3000.0 + random() * 18000.0
        start_position_m = position_m + direction * start_offset_m

        # More visible but still believable breakup delta-v.
        # Creates a thicker debris band instead of one perfectly thin orbit.
        delta_v_mps = 20.0 + random() * 130.0

        # Most fragments follow original orbit, some get stronger radial/plane spread.
        fragment_velocity_mps = base_velocity_mps + direction * delta_v_mps

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

        marker = sphere(
            pos=meters_to_scene(start_position_m),
            radius=random_debris_radius_scene(mass_kg),
            color=frag_color,
            emissive=False,
            opacity=0.88,
            make_trail=False
        )

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
            "central_body": debris_central_body,
            "recent_collision_cooldown": 0
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


def update_debris_particle(debris):
    if not debris["active"]:
        return

    central_body = debris.get("central_body", "Earth")

    area_m2 = object_cross_section_area_m2(debris, default_radius_m=0.08)

    debris["velocity_mps"] += physics_acceleration_for_object(
        debris["position_m"],
        debris["velocity_mps"],
        central_body,
        debris.get("mass_kg", 1.0),
        area_m2
    ) * dt

    debris["position_m"] += debris["velocity_mps"] * dt
    debris["marker"].pos = meters_to_scene(debris["position_m"])

    debris["age"] += 1
    debris["life"] -= 1

    if debris.get("recent_collision_cooldown", 0) > 0:
        debris["recent_collision_cooldown"] -= 1

    if debris["life"] < 1500:
        debris["marker"].opacity = max(0, debris["life"] / 1500)

    if debris["life"] <= 0:
        hide_debris(debris)
        return

    if debris.get("central_body", "Earth") == "Mars":
        if mag(debris["position_m"] - MARS_POSITION_M) <= R_MARS:
            hide_debris(debris)
    else:
        if mag(debris["position_m"]) <= R_EARTH:
            hide_debris(debris)


def hide_debris(debris):
    debris["active"] = False
    debris["marker"].visible = False



def create_secondary_debris(position_m, center_velocity_mps, relative_speed_mps, parent_mass_kg):
    new_parts = []

    if len(debris_particles) >= MAX_DEBRIS_PARTICLES:
        return new_parts

    remaining_capacity = MAX_DEBRIS_PARTICLES - len(debris_particles)
    count = int(SECONDARY_BREAKUP_MIN_FRAGMENTS + random() * (SECONDARY_BREAKUP_MAX_FRAGMENTS - SECONDARY_BREAKUP_MIN_FRAGMENTS + 1))
    count = min(count, remaining_capacity)

    debris_central_body = central_body_for_position(position_m)

    for i in range(count):
        direction = random_unit_vector()

        # Secondary debris gets a smaller delta-v than the original impact.
        # Hypervelocity impacts can eject small fragments quickly, but we cap it
        # so the demo remains visually readable and numerically stable.
        eject_speed = min(250.0, 15.0 + random() * max(20.0, 0.025 * relative_speed_mps))
        start_position_m = position_m + direction * (1000.0 + random() * 6000.0)
        fragment_velocity_mps = center_velocity_mps + direction * eject_speed
        mass_kg = max(0.05, parent_mass_kg * (0.08 + random() * 0.18))

        frag_color = color.gray(0.75)
        if i == 0:
            frag_color = color.orange
        elif i == 1:
            frag_color = color.white

        marker = sphere(
            pos=meters_to_scene(start_position_m),
            radius=random_debris_radius_scene(mass_kg),
            color=frag_color,
            emissive=False,
            opacity=0.82,
            make_trail=False
        )

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
            "central_body": debris_central_body,
            "recent_collision_cooldown": 20
        })

    return new_parts


def deflect_debris_pair(d1, d2):
    # Simplified inelastic two-body collision. It conserves momentum along the
    # collision normal, loses some relative speed, and adds a small sideways
    # scatter so the particles visibly move into different nearby orbits.
    normal = d1["position_m"] - d2["position_m"]

    if mag(normal) == 0:
        normal = random_unit_vector()
    else:
        normal = norm(normal)

    m1 = d1.get("mass_kg", 1.0)
    m2 = d2.get("mass_kg", 1.0)
    v1 = d1["velocity_mps"]
    v2 = d2["velocity_mps"]

    relative_velocity = v1 - v2
    closing_speed = dot(relative_velocity, normal)

    if closing_speed >= 0:
        normal = -normal
        closing_speed = dot(relative_velocity, normal)

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

        if not d1["active"]:
            continue

        if d1["age"] < d1["can_collide_after"]:
            continue

        if d1.get("recent_collision_cooldown", 0) > 0:
            continue

        for j in range(i + 1, len(active_debris)):
            d2 = active_debris[j]

            if not d2["active"]:
                continue

            if d2["age"] < d2["can_collide_after"]:
                continue

            if d2.get("recent_collision_cooldown", 0) > 0:
                continue

            distance_m = mag(d1["position_m"] - d2["position_m"])

            if distance_m >= debris_debris_collision_distance_m:
                continue

            collision_pos_m = (d1["position_m"] + d2["position_m"]) / 2
            relative_velocity_mps = d1["velocity_mps"] - d2["velocity_mps"]
            relative_speed_mps = mag(relative_velocity_mps)

            m1 = d1.get("mass_kg", 1.0)
            m2 = d2.get("mass_kg", 1.0)
            smaller_mass = min(m1, m2)
            reduced_mass = (m1 * m2) / (m1 + m2)

            impact_energy_j = 0.5 * reduced_mass * relative_speed_mps ** 2
            energy_per_smaller_mass = impact_energy_j / max(smaller_mass, 0.001)

            # Above the NASA-style 40 J/g threshold, there is a chance of a
            # secondary fragmenting event. It is probabilistic to avoid every
            # visible contact turning into an unrealistic runaway cloud.
            catastrophic = energy_per_smaller_mass > CATASTROPHIC_ENERGY_J_PER_KG

            if catastrophic and random() < 0.35 and len(debris_particles) + len(created_parts) < MAX_DEBRIS_PARTICLES:
                center_velocity = (d1["velocity_mps"] * m1 + d2["velocity_mps"] * m2) / (m1 + m2)
                parent_mass = smaller_mass

                # The smaller particle is consumed; the larger one is kicked.
                if m1 <= m2:
                    hide_debris(d1)
                    d2["velocity_mps"] = center_velocity + random_unit_vector() * min(80.0, relative_speed_mps * 0.01)
                    d2["recent_collision_cooldown"] = 25
                else:
                    hide_debris(d2)
                    d1["velocity_mps"] = center_velocity + random_unit_vector() * min(80.0, relative_speed_mps * 0.01)
                    d1["recent_collision_cooldown"] = 25

                created_parts.extend(create_secondary_debris(
                    collision_pos_m,
                    center_velocity,
                    relative_speed_mps,
                    parent_mass
                ))

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
# Telemetry output for quantum system
# -----------------------------
def build_telemetry_payload(frame_count, visual_time, physical_time, satellites, asteroids, debris_particles):
    active_debris = [d for d in debris_particles if d["active"]]

    unix_time_s = time.time()
    timestamp_utc_iso = datetime.fromtimestamp(unix_time_s, tz=timezone.utc).isoformat()
    measurement_timestamp = {
        "unix_time_s": float(unix_time_s),
        "utc_iso": timestamp_utc_iso,
        "simulation_visual_time_s": float(visual_time),
        "simulation_physical_time_s": float(physical_time),
        "frame": int(frame_count),
        "sample_hz": float(effective_telemetry_sample_hz()),
        "base_sample_hz": float(BASE_TELEMETRY_SAMPLE_HZ),
        "speed_multiplier": float(simulation_speed_multiplier)
    }

    satellite_states = []
    for sat in satellites:
        central_body = sat.get("central_body", "Earth")
        if central_body == "Mars":
            central_position = MARS_POSITION_M
            central_radius = R_MARS
        else:
            central_position = vector(0, 0, 0)
            central_radius = R_EARTH

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
            central_body_name=central_body,
            central_body_position_m=central_position,
            central_body_radius_m=central_radius,
            extra={
                "can_maneuver": bool(sat["active"]),
                "destroyed": not bool(sat["active"]),
                "orbit_class": sat.get("orbit_class", "unknown"),
                "orbit_description": sat.get("orbit_description", "unknown")
            }
        ))

    asteroid_states = []
    for asteroid in asteroids:
        central_body = asteroid.get("central_body", "Earth")
        if central_body == "Mars":
            central_position = MARS_POSITION_M
            central_radius = R_MARS
        else:
            central_position = vector(0, 0, 0)
            central_radius = R_EARTH

        asteroid_states.append(object_state_dict(
            object_id=asteroid["name"],
            object_type="asteroid",
            active=asteroid["active"],
            position_m=asteroid["position_m"],
            velocity_mps=asteroid["velocity_mps"],
            mass_kg=asteroid.get("mass_kg"),
            radius_m=asteroid.get("physical_radius_m"),
            selected_for_data=False,
            measurement_timestamp=measurement_timestamp,
            central_body_name=central_body,
            central_body_position_m=central_position,
            central_body_radius_m=central_radius,
            extra={
                "collision_threat": bool(asteroid["active"]),
                "orbit_class": asteroid.get("orbit_class", "unknown"),
                "orbit_description": asteroid.get("orbit_description", "unknown")
            }
        ))

    debris_states = []
    for index, debris in enumerate(active_debris):
        central_body = debris.get("central_body", "Earth")
        if central_body == "Mars":
            central_position = MARS_POSITION_M
            central_radius = R_MARS
        else:
            central_position = vector(0, 0, 0)
            central_radius = R_EARTH

        debris_states.append(object_state_dict(
            object_id=f"DEBRIS-{index + 1:03d}",
            object_type="debris",
            active=debris["active"],
            position_m=debris["position_m"],
            velocity_mps=debris["velocity_mps"],
            mass_kg=debris.get("mass_kg"),
            radius_m=debris.get("physical_radius_m"),
            selected_for_data=False,
            measurement_timestamp=measurement_timestamp,
            central_body_name=central_body,
            central_body_position_m=central_position,
            central_body_radius_m=central_radius,
            extra={
                "age_frames": int(debris.get("age", 0)),
                "life_frames_remaining": int(debris.get("life", 0)),
                "recent_collision_cooldown_frames": int(debris.get("recent_collision_cooldown", 0))
            }
        ))

    spacecraft_states = []
    if "last_transfer_spacecraft_state" in globals():
        spacecraft_state = last_transfer_spacecraft_state
        spacecraft_states.append(object_state_dict(
            object_id="MARS-XFER-1",
            object_type="earth_to_mars_transfer_spacecraft",
            active=globals().get("mars_transfer_spacecraft_active", True),
            position_m=spacecraft_state.get("position_m", vector(0, 0, 0)),
            velocity_mps=spacecraft_state.get("velocity_mps", vector(0, 0, 0)),
            mass_kg=globals().get("MARS_TRANSFER_SPACECRAFT_MASS_KG", None),
            radius_m=globals().get("MARS_TRANSFER_SPACECRAFT_PHYSICAL_RADIUS_M", None),
            selected_for_data=False,
            measurement_timestamp=measurement_timestamp,
            central_body_name="Interplanetary transfer",
            central_body_position_m=vector(0, 0, 0),
            central_body_radius_m=R_EARTH,
            extra={
                "can_maneuver": bool(globals().get("mars_transfer_spacecraft_active", True)),
                "destroyed": not bool(globals().get("mars_transfer_spacecraft_active", True)),
                "arrived_at_mars": bool(globals().get("mars_transfer_spacecraft_arrived", False)),
                "destroyed_by": globals().get("mars_transfer_spacecraft_destroyed_by", None),
                "mission_fraction": float(spacecraft_state.get("fraction", 0.0)),
                "mission_elapsed_days": float(spacecraft_state.get("elapsed_transfer_time_days", 0.0)),
                "transfer_type": "NASA-style Hohmann transfer graphic with real transfer stats"
            }
        ))

    all_hazards = asteroid_states + debris_states

    return {
        "schema": "satellite_simulation.telemetry.v1",
        "frame": int(frame_count),
        "measurement_timestamp": measurement_timestamp,
        "utc_iso": timestamp_utc_iso,
        "unix_time_s": float(unix_time_s),
        "visual_time_s": float(visual_time),
        "physical_time_s": float(physical_time),
        "units": {
            "position": "meters, Earth-centered inertial demo frame",
            "velocity": "meters per second, Earth-centered inertial demo frame",
            "mass": "kilograms",
            "radius": "meters",
            "time": "seconds",
            "timestamp": "UTC ISO-8601 and Unix seconds"
        },
        "constants": {
            "earth_mu_m3_s2": MU_EARTH,
            "earth_radius_m": R_EARTH,
            "earth_j2": J2_EARTH,
            "mars_mu_m3_s2": MU_MARS,
            "mars_radius_m": R_MARS,
            "mars_j2": J2_MARS,
            "j2_perturbation_enabled": ENABLE_J2_PERTURBATION,
            "atmospheric_drag_enabled": ENABLE_ATMOSPHERIC_DRAG,
            "drag_coefficient": DRAG_COEFFICIENT,
            "max_drag_altitude_m": MAX_DRAG_ALTITUDE_M,
            "earth_mars_distance_m": EARTH_MARS_DISTANCE_M,
            "earth_mars_distance_au": EARTH_MARS_DISTANCE_AU,
            "earth_mars_hohmann_transfer_time_days": TRANSFER_TIME_DAYS,
            "earth_mars_hohmann_phase_angle_deg": EARTH_MARS_HOHMANN_PHASE_ANGLE_DEG,
            "time_step_s": dt,
            "base_time_step_s": BASE_DT,
            "speed_multiplier": simulation_speed_multiplier
        },
        "counts": {
            "active_satellites": sum(1 for sat in satellites if sat["active"]),
            "active_asteroids": sum(1 for obj in asteroid_states if obj["active"]),
            "active_debris": len(debris_states),
            "total_hazards": len([obj for obj in all_hazards if obj["active"]])
        },
        "satellites": satellite_states,
        "spacecraft": spacecraft_states,
        "asteroids": asteroid_states,
        "debris": debris_states,
        "hazards": all_hazards
    }


def make_terminal_payload(payload):
    # Full payload: every currently existing satellite, asteroid, and active debris object.
    # At high sampling rates this can be a lot of text after an explosion, but it is useful for testing.
    terminal_payload = dict(payload)
    terminal_payload["terminal_note"] = (
        "Full telemetry payload: all currently existing objects. "
        f"Base sampling is {BASE_TELEMETRY_SAMPLE_HZ:.1f} Hz; "
        f"current speed is {simulation_speed_multiplier}x, so real-time sampling is {effective_telemetry_sample_hz():.1f} Hz."
    )
    return terminal_payload


def export_telemetry(frame_count, visual_time, physical_time, satellites, asteroids, debris_particles):
    payload = build_telemetry_payload(
        frame_count,
        visual_time,
        physical_time,
        satellites,
        asteroids,
        debris_particles
    )

    if TELEMETRY_OUTPUT_MODE == "json_file":
        try:
            write_live_telemetry_json(payload)
            telemetry_label.text = (
                f"Telemetry JSON: {TELEMETRY_JSON_FILENAME} @ {effective_telemetry_sample_hz():.1f} Hz | "
                f"speed {simulation_speed_multiplier}x | "
                f"{payload['counts']['active_satellites']} sats, "
                f"{payload['counts']['active_asteroids']} asteroids, "
                f"{payload['counts']['active_debris']} debris | "
                f"{payload['utc_iso']}"
            )
            telemetry_label.color = color.white
        except Exception as e:
            telemetry_label.text = f"Telemetry JSON write error: {e}"
            telemetry_label.color = color.red
            print(f"Telemetry JSON write error: {e}")

    elif TELEMETRY_OUTPUT_MODE == "terminal":
        terminal_payload = make_terminal_payload(payload)
        print("\n========== SPACE STATE TELEMETRY ==========")
        print(json.dumps(terminal_payload, indent=2))
        print("===========================================\n")

        telemetry_label.text = (
            f"Telemetry output: terminal {effective_telemetry_sample_hz():.1f} Hz | speed {simulation_speed_multiplier}x | "
            f"{payload['counts']['active_satellites']} sats, "
            f"{payload['counts']['active_asteroids']} asteroids, "
            f"{payload['counts']['active_debris']} debris | "
            f"{payload['utc_iso']}"
        )


# -----------------------------
# Earth-to-Mars transfer spacecraft
# -----------------------------
# NASA-style Hohmann transfer statistics are computed from heliocentric two-body
# physics. The drawn spacecraft path is a mission graphic between the physically
# scaled Earth and Mars positions so it visibly starts at Earth and ends at Mars.
transfer_departure_speed_mps = sqrt(MU_SUN * (2 / EARTH_HELIOCENTRIC_RADIUS_M - 1 / TRANSFER_SEMI_MAJOR_AXIS_M))
earth_orbital_speed_mps = sqrt(MU_SUN / EARTH_HELIOCENTRIC_RADIUS_M)
transfer_arrival_speed_mps = sqrt(MU_SUN * (2 / MARS_HELIOCENTRIC_RADIUS_M - 1 / TRANSFER_SEMI_MAJOR_AXIS_M))
mars_orbital_speed_mps = sqrt(MU_SUN / MARS_HELIOCENTRIC_RADIUS_M)
transfer_departure_delta_v_mps = transfer_departure_speed_mps - earth_orbital_speed_mps
transfer_arrival_delta_v_mps = mars_orbital_speed_mps - transfer_arrival_speed_mps

TRANSFER_START_M = vector(0, 0, 0)
TRANSFER_END_M = MARS_POSITION_M
TRANSFER_CHORD_M = TRANSFER_END_M - TRANSFER_START_M
TRANSFER_CHORD_LENGTH_M = mag(TRANSFER_CHORD_M)
TRANSFER_CHORD_DIR = norm(TRANSFER_CHORD_M)
TRANSFER_ARC_NORMAL = cross(vector(0, 0, 1), TRANSFER_CHORD_DIR)
if mag(TRANSFER_ARC_NORMAL) == 0:
    TRANSFER_ARC_NORMAL = vector(0, 1, 0)
else:
    TRANSFER_ARC_NORMAL = norm(TRANSFER_ARC_NORMAL)
TRANSFER_ARC_HEIGHT_M = 0.16 * TRANSFER_CHORD_LENGTH_M


def earth_to_mars_transfer_graphic_position(fraction):
    t = min(1.0, max(0.0, fraction))
    base = TRANSFER_START_M * (1.0 - t) + TRANSFER_END_M * t
    arc_offset = TRANSFER_ARC_NORMAL * (TRANSFER_ARC_HEIGHT_M * sin(pi * t))
    return base + arc_offset


transfer_points = []
for i in range(361):
    transfer_points.append(meters_to_scene(earth_to_mars_transfer_graphic_position(i / 360.0)))

transfer_curve = curve(pos=transfer_points, color=color.orange, radius=0.010)

# A small marker object owns the trail and camera focus position. The visible
# spacecraft is a multi-part display model updated around this marker. Its
# display size is intentionally larger than the satellites so it is readable,
# while telemetry keeps a realistic physical radius/mass scale.
mars_transfer_spacecraft = sphere(
    pos=meters_to_scene(TRANSFER_START_M),
    radius=0.030,
    color=color.white,
    emissive=True,
    opacity=0.35,
    make_trail=True,
    trail_color=color.white,
    retain=1200
)
mars_transfer_spacecraft.trail_radius = 0.010

MARS_TRANSFER_SPACECRAFT_MASS_KG = 9500.0
MARS_TRANSFER_SPACECRAFT_PHYSICAL_RADIUS_M = 14.0


def transfer_spacecraft_basis(velocity_mps):
    if mag(velocity_mps) > 0:
        forward = norm(velocity_mps)
    else:
        forward = TRANSFER_CHORD_DIR

    wing = cross(vector(0, 0, 1), forward)
    if mag(wing) == 0:
        wing = cross(vector(0, 1, 0), forward)
    wing = norm(wing)

    normal_dir = cross(forward, wing)
    if mag(normal_dir) == 0:
        normal_dir = vector(0, 0, 1)
    else:
        normal_dir = norm(normal_dir)

    return forward, wing, normal_dir


def create_transfer_spacecraft_display_model(initial_pos):
    forward, wing, normal_dir = transfer_spacecraft_basis(TRANSFER_CHORD_DIR)

    model = {
        "bus": cylinder(
            pos=initial_pos - forward * 0.16,
            axis=forward * 0.32,
            radius=0.065,
            color=color.white,
            emissive=False
        ),
        "nose": cone(
            pos=initial_pos + forward * 0.16,
            axis=forward * 0.14,
            radius=0.065,
            color=vector(0.85, 0.85, 0.90),
            emissive=False
        ),
        "engine": cylinder(
            pos=initial_pos - forward * 0.23,
            axis=-forward * 0.09,
            radius=0.045,
            color=color.gray(0.45),
            emissive=False
        ),
        "engine_glow": sphere(
            pos=initial_pos - forward * 0.34,
            radius=0.035,
            color=color.orange,
            emissive=True,
            opacity=0.50
        ),
        "left_panel": box(
            pos=initial_pos + wing * 0.34,
            axis=wing,
            length=0.46,
            height=0.065,
            width=0.010,
            color=vector(0.03, 0.12, 0.55)
        ),
        "right_panel": box(
            pos=initial_pos - wing * 0.34,
            axis=wing,
            length=0.46,
            height=0.065,
            width=0.010,
            color=vector(0.03, 0.12, 0.55)
        ),
        "dish": sphere(
            pos=initial_pos + normal_dir * 0.10 - forward * 0.02,
            radius=0.035,
            color=color.gray(0.75),
            emissive=False
        )
    }

    return model


def update_transfer_spacecraft_display_model(model, center_pos, velocity_mps):
    forward, wing, normal_dir = transfer_spacecraft_basis(velocity_mps)

    model["bus"].pos = center_pos - forward * 0.16
    model["bus"].axis = forward * 0.32

    model["nose"].pos = center_pos + forward * 0.16
    model["nose"].axis = forward * 0.14

    model["engine"].pos = center_pos - forward * 0.23
    model["engine"].axis = -forward * 0.09

    model["engine_glow"].pos = center_pos - forward * 0.34

    model["left_panel"].pos = center_pos + wing * 0.34
    model["left_panel"].axis = wing

    model["right_panel"].pos = center_pos - wing * 0.34
    model["right_panel"].axis = wing

    model["dish"].pos = center_pos + normal_dir * 0.10 - forward * 0.02


def set_transfer_spacecraft_display_visible(model, visible):
    for part in model.values():
        part.visible = visible


mars_transfer_spacecraft_model = create_transfer_spacecraft_display_model(mars_transfer_spacecraft.pos)

mars_transfer_spacecraft_active = True
mars_transfer_spacecraft_arrived = False
mars_transfer_spacecraft_destroyed_by = None
last_transfer_spacecraft_state = {
    "fraction": 0.0,
    "elapsed_transfer_time_s": 0.0,
    "elapsed_transfer_time_days": 0.0,
    "position_m": TRANSFER_START_M,
    "velocity_mps": vector(0, 0, 0)
}

mars_transfer_spacecraft_label = label(
    pos=mars_transfer_spacecraft.pos + vector(0.36, 0.36, 0),
    text="MARS-XFER-1",
    height=11,
    box=False,
    color=color.white
)

transfer_stats_label = label(
    pos=vector(-3.6, 1.98, 0),
    text=(
        f"MARS-XFER-1 | Hohmann TOF {TRANSFER_TIME_DAYS:.1f} days | "
        f"depart dv {transfer_departure_delta_v_mps / 1000.0:.2f} km/s | "
        f"arrive dv {transfer_arrival_delta_v_mps / 1000.0:.2f} km/s"
    ),
    height=10,
    box=False,
    color=color.white
)


def transfer_spacecraft_state(visual_time_s):
    fraction = min(1.0, max(0.0, visual_time_s / MARS_TRANSFER_VISUAL_DURATION_S))
    elapsed_transfer_time_s = fraction * TRANSFER_TIME_S
    position_m = earth_to_mars_transfer_graphic_position(fraction)

    # Approximate physical transfer velocity along the drawn path using the real
    # Hohmann time of flight. This keeps telemetry and breakup debris velocities
    # in a mission-scale range instead of using the time-compressed visual speed.
    small_fraction_step = 1.0 / 2000.0
    fraction_before = max(0.0, fraction - small_fraction_step)
    fraction_after = min(1.0, fraction + small_fraction_step)
    position_before = earth_to_mars_transfer_graphic_position(fraction_before)
    position_after = earth_to_mars_transfer_graphic_position(fraction_after)
    elapsed_before = fraction_before * TRANSFER_TIME_S
    elapsed_after = fraction_after * TRANSFER_TIME_S

    if elapsed_after > elapsed_before:
        velocity_mps = (position_after - position_before) / (elapsed_after - elapsed_before)
    else:
        velocity_mps = TRANSFER_CHORD_DIR * transfer_departure_speed_mps

    return {
        "fraction": fraction,
        "elapsed_transfer_time_s": elapsed_transfer_time_s,
        "elapsed_transfer_time_days": elapsed_transfer_time_s / 86400.0,
        "position_m": position_m,
        "velocity_mps": velocity_mps
    }


def update_transfer_spacecraft(visual_time_s):
    global last_transfer_spacecraft_state, mars_transfer_spacecraft_arrived

    if not mars_transfer_spacecraft_active:
        return

    state = transfer_spacecraft_state(visual_time_s)
    last_transfer_spacecraft_state = state
    p = meters_to_scene(state["position_m"])
    mars_transfer_spacecraft.pos = p
    update_transfer_spacecraft_display_model(mars_transfer_spacecraft_model, p, state["velocity_mps"])
    mars_transfer_spacecraft_label.pos = p + vector(0.36, 0.36, 0)

    transfer_stats_label.text = (
        f"MARS-XFER-1 | transfer {100 * state['fraction']:.1f}% | "
        f"mission elapsed {state['elapsed_transfer_time_days']:.1f}/{TRANSFER_TIME_DAYS:.1f} days | "
        f"distance {EARTH_MARS_DISTANCE_M / 1e9:.1f} million km | "
        f"depart dv {transfer_departure_delta_v_mps / 1000.0:.2f} km/s"
    )

    if state["fraction"] >= 1.0:
        mars_transfer_spacecraft_arrived = True
        mars_transfer_spacecraft.color = color.green
        mars_transfer_spacecraft_model["engine_glow"].color = color.green
        mars_transfer_spacecraft_label.text = "MARS-XFER-1 ARRIVED"


def hide_transfer_spacecraft(reason_text):
    global mars_transfer_spacecraft_active, mars_transfer_spacecraft_destroyed_by

    if not mars_transfer_spacecraft_active:
        return

    mars_transfer_spacecraft_active = False
    mars_transfer_spacecraft_destroyed_by = reason_text
    mars_transfer_spacecraft.make_trail = False
    mars_transfer_spacecraft.clear_trail()
    mars_transfer_spacecraft.visible = False
    set_transfer_spacecraft_display_visible(mars_transfer_spacecraft_model, False)
    mars_transfer_spacecraft_label.visible = False
    transfer_stats_label.text = f"MARS-XFER-1 DESTROYED | cause: {reason_text}"
    transfer_stats_label.color = color.red


def transfer_spacecraft_position_m():
    return last_transfer_spacecraft_state.get("position_m", TRANSFER_START_M)


def transfer_spacecraft_velocity_mps():
    return last_transfer_spacecraft_state.get("velocity_mps", vector(0, 0, 0))

# -----------------------------
# System setup
# -----------------------------
# Orbit class choices:
# - LEO: circular low-Earth orbits around 550 to 1,200 km altitude.
# - MEO: circular medium-Earth orbits around GPS-like altitudes.
# - HEO: highly elliptical Molniya-style orbits using perigee/apogee.
LEO_ALTITUDES_M = [550000.0, 700000.0, 850000.0, 1000000.0, 1200000.0]
MEO_ALTITUDES_M = [20200000.0, 21500000.0, 23222000.0]
HEO_PERIGEE_ALTITUDE_M = 1000000.0
HEO_APOGEE_ALTITUDE_M = 39700000.0

MARS_LOW_ALTITUDES_M = [400000.0, 500000.0, 700000.0, 1000000.0]
MARS_MID_ALTITUDES_M = [6000000.0, 10000000.0, 17000000.0]
MARS_HIGH_PERIGEE_ALTITUDE_M = 400000.0
MARS_HIGH_APOGEE_ALTITUDE_M = 17000000.0

SATELLITE_COLORS = [
    color.cyan,
    color.green,
    color.orange,
    color.magenta,
    color.yellow,
    vector(0.30, 0.55, 1.0),
    vector(0.85, 0.55, 0.20),
    vector(0.70, 1.00, 0.70),
    vector(1.00, 0.55, 0.55),
    vector(0.75, 0.75, 1.00)
]

TRAIL_COLORS = [
    vector(0.12, 0.35, 0.50),
    vector(0.12, 0.40, 0.12),
    vector(0.45, 0.28, 0.08),
    vector(0.40, 0.12, 0.40),
    vector(0.45, 0.45, 0.10),
    vector(0.12, 0.25, 0.55),
    vector(0.45, 0.22, 0.08),
    vector(0.20, 0.45, 0.20),
    vector(0.45, 0.15, 0.15),
    vector(0.28, 0.28, 0.50)
]


def color_for_satellite(index):
    return SATELLITE_COLORS[index % len(SATELLITE_COLORS)]


def trail_color_for_satellite(index):
    return TRAIL_COLORS[index % len(TRAIL_COLORS)]


def build_requested_constellation():
    built_satellites = []
    satellite_index = 1

    for i in range(REQUESTED_LEO_SATELLITES):
        altitude = LEO_ALTITUDES_M[i % len(LEO_ALTITUDES_M)]
        inclination = [53, 70, 98, 45, 82][i % 5]
        raan = (i * 360.0 / max(1, REQUESTED_LEO_SATELLITES)) % 360
        phase = (i * 137.5) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_circular_orbit(altitude, color.gray(0.27), inclination_deg=inclination, raan_deg=raan)
        built_satellites.append(create_satellite(
            f"SAT-{satellite_index}",
            altitude,
            inclination,
            raan,
            phase,
            sat_color,
            trail_color,
            orbit_class="LEO",
            orbit_description=f"LEO circular orbit, altitude {altitude / 1000.0:.0f} km"
        ))
        satellite_index += 1

    for i in range(REQUESTED_MEO_SATELLITES):
        altitude = MEO_ALTITUDES_M[i % len(MEO_ALTITUDES_M)]
        inclination = [55, 56, 63][i % 3]
        raan = (35 + i * 360.0 / max(1, REQUESTED_MEO_SATELLITES)) % 360
        phase = (90 + i * 131.0) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_circular_orbit(altitude, color.gray(0.18), inclination_deg=inclination, raan_deg=raan)
        built_satellites.append(create_satellite(
            f"SAT-{satellite_index}",
            altitude,
            inclination,
            raan,
            phase,
            sat_color,
            trail_color,
            orbit_class="MEO",
            orbit_description=f"MEO circular orbit, altitude {altitude / 1000.0:.0f} km"
        ))
        satellite_index += 1

    for i in range(REQUESTED_HEO_SATELLITES):
        inclination = 63.4
        raan = (70 + i * 360.0 / max(1, REQUESTED_HEO_SATELLITES)) % 360
        argument_of_perigee = 270
        true_anomaly = (i * 147.0) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_elliptical_orbit(
            HEO_PERIGEE_ALTITUDE_M,
            HEO_APOGEE_ALTITUDE_M,
            color.gray(0.16),
            inclination_deg=inclination,
            raan_deg=raan,
            argument_of_perigee_deg=argument_of_perigee
        )
        built_satellites.append(create_heo_satellite(
            f"SAT-{satellite_index}",
            HEO_PERIGEE_ALTITUDE_M,
            HEO_APOGEE_ALTITUDE_M,
            inclination,
            raan,
            argument_of_perigee,
            true_anomaly,
            sat_color,
            trail_color
        ))
        satellite_index += 1

    return built_satellites


def build_requested_asteroids(existing_satellites):
    built_asteroids = []
    asteroid_index = 1

    # Keep the first LEO asteroid aimed at a real satellite if possible so the demo still shows a collision.
    asteroid_target_satellite = existing_satellites[min(1, len(existing_satellites) - 1)] if len(existing_satellites) > 0 else None

    for i in range(REQUESTED_LEO_ASTEROIDS):
        if i == 0 and asteroid_target_satellite is not None:
            built_asteroids.append(create_physical_asteroid(
                asteroid_target_satellite,
                desired_visual_collision_time,
                name=f"AST-{asteroid_index}"
            ))
        else:
            altitude = LEO_ALTITUDES_M[i % len(LEO_ALTITUDES_M)] + 50000.0
            inclination = [51.6, 63, 74, 97][i % 4]
            raan = (20 + i * 360.0 / max(1, REQUESTED_LEO_ASTEROIDS)) % 360
            phase = (45 + i * 123.0) % 360
            draw_circular_orbit(altitude, color.gray(0.20), inclination_deg=inclination, raan_deg=raan)
            built_asteroids.append(create_circular_asteroid(
                f"AST-{asteroid_index}",
                altitude,
                inclination,
                raan,
                phase,
                orbit_class="LEO",
                prograde=False
            ))

        asteroid_index += 1

    for i in range(REQUESTED_MEO_ASTEROIDS):
        altitude = MEO_ALTITUDES_M[i % len(MEO_ALTITUDES_M)] + 100000.0
        inclination = [55, 63, 70][i % 3]
        raan = (55 + i * 360.0 / max(1, REQUESTED_MEO_ASTEROIDS)) % 360
        phase = (120 + i * 129.0) % 360
        draw_circular_orbit(altitude, color.gray(0.14), inclination_deg=inclination, raan_deg=raan)
        built_asteroids.append(create_circular_asteroid(
            f"AST-{asteroid_index}",
            altitude,
            inclination,
            raan,
            phase,
            orbit_class="MEO",
            prograde=False
        ))
        asteroid_index += 1

    for i in range(REQUESTED_HEO_ASTEROIDS):
        inclination = 63.4
        raan = (95 + i * 360.0 / max(1, REQUESTED_HEO_ASTEROIDS)) % 360
        argument_of_perigee = 270
        true_anomaly = (60 + i * 151.0) % 360
        draw_elliptical_orbit(
            HEO_PERIGEE_ALTITUDE_M,
            HEO_APOGEE_ALTITUDE_M,
            color.gray(0.12),
            inclination_deg=inclination,
            raan_deg=raan,
            argument_of_perigee_deg=argument_of_perigee
        )
        built_asteroids.append(create_heo_asteroid(
            f"AST-{asteroid_index}",
            HEO_PERIGEE_ALTITUDE_M,
            HEO_APOGEE_ALTITUDE_M,
            inclination,
            raan,
            argument_of_perigee,
            true_anomaly,
            prograde=False
        ))
        asteroid_index += 1

    return built_asteroids


def build_requested_mars_asteroids(existing_mars_satellites):
    built_asteroids = []
    asteroid_index = 1

    mars_target_satellite = existing_mars_satellites[0] if len(existing_mars_satellites) > 0 else None

    for i in range(REQUESTED_MARS_LOW_ASTEROIDS):
        if i == 0 and mars_target_satellite is not None:
            built_asteroids.append(create_physical_mars_asteroid(
                mars_target_satellite,
                desired_visual_collision_time + 8.0,
                name=f"MARS-AST-{asteroid_index}"
            ))
        else:
            altitude = MARS_LOW_ALTITUDES_M[i % len(MARS_LOW_ALTITUDES_M)] + 35000.0
            inclination = [25, 45, 70, 93][i % 4]
            raan = (25 + i * 360.0 / max(1, REQUESTED_MARS_LOW_ASTEROIDS)) % 360
            phase = (40 + i * 123.0) % 360
            draw_mars_circular_orbit(altitude, color.gray(0.16), inclination_deg=inclination, raan_deg=raan)
            built_asteroids.append(create_mars_circular_asteroid(
                f"MARS-AST-{asteroid_index}",
                altitude,
                inclination,
                raan,
                phase,
                orbit_class="Mars Low",
                prograde=False
            ))

        asteroid_index += 1

    for i in range(REQUESTED_MARS_MID_ASTEROIDS):
        altitude = MARS_MID_ALTITUDES_M[i % len(MARS_MID_ALTITUDES_M)] + 70000.0
        inclination = [35, 55, 65][i % 3]
        raan = (55 + i * 360.0 / max(1, REQUESTED_MARS_MID_ASTEROIDS)) % 360
        phase = (110 + i * 129.0) % 360
        draw_mars_circular_orbit(altitude, color.gray(0.13), inclination_deg=inclination, raan_deg=raan)
        built_asteroids.append(create_mars_circular_asteroid(
            f"MARS-AST-{asteroid_index}",
            altitude,
            inclination,
            raan,
            phase,
            orbit_class="Mars Medium",
            prograde=False
        ))
        asteroid_index += 1

    for i in range(REQUESTED_MARS_HIGH_ASTEROIDS):
        inclination = 63.0
        raan = (95 + i * 360.0 / max(1, REQUESTED_MARS_HIGH_ASTEROIDS)) % 360
        argument_of_perigee = 270
        true_anomaly = (60 + i * 151.0) % 360
        draw_mars_elliptical_orbit(
            MARS_HIGH_PERIGEE_ALTITUDE_M,
            MARS_HIGH_APOGEE_ALTITUDE_M,
            color.gray(0.11),
            inclination_deg=inclination,
            raan_deg=raan,
            argument_of_perigee_deg=argument_of_perigee
        )
        built_asteroids.append(create_mars_high_asteroid(
            f"MARS-AST-{asteroid_index}",
            MARS_HIGH_PERIGEE_ALTITUDE_M,
            MARS_HIGH_APOGEE_ALTITUDE_M,
            inclination,
            raan,
            argument_of_perigee,
            true_anomaly,
            prograde=False
        ))
        asteroid_index += 1

    return built_asteroids


def build_requested_mars_constellation(starting_satellite_index):
    built_satellites = []
    satellite_index = starting_satellite_index

    for i in range(REQUESTED_MARS_LOW_SATELLITES):
        altitude = MARS_LOW_ALTITUDES_M[i % len(MARS_LOW_ALTITUDES_M)]
        inclination = [25, 45, 70, 93][i % 4]
        raan = (i * 360.0 / max(1, REQUESTED_MARS_LOW_SATELLITES)) % 360
        phase = (i * 137.5) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_mars_circular_orbit(altitude, color.gray(0.24), inclination_deg=inclination, raan_deg=raan)
        built_satellites.append(create_mars_satellite(
            f"MARS-SAT-{i + 1}",
            altitude,
            inclination,
            raan,
            phase,
            sat_color,
            trail_color,
            orbit_class="Mars Low",
            orbit_description=f"low Mars circular orbit, altitude {altitude / 1000.0:.0f} km"
        ))
        satellite_index += 1

    for i in range(REQUESTED_MARS_MID_SATELLITES):
        altitude = MARS_MID_ALTITUDES_M[i % len(MARS_MID_ALTITUDES_M)]
        inclination = [35, 55, 65][i % 3]
        raan = (35 + i * 360.0 / max(1, REQUESTED_MARS_MID_SATELLITES)) % 360
        phase = (90 + i * 131.0) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_mars_circular_orbit(altitude, color.gray(0.18), inclination_deg=inclination, raan_deg=raan)
        built_satellites.append(create_mars_satellite(
            f"MARS-SAT-{REQUESTED_MARS_LOW_SATELLITES + i + 1}",
            altitude,
            inclination,
            raan,
            phase,
            sat_color,
            trail_color,
            orbit_class="Mars Medium",
            orbit_description=f"medium Mars circular orbit, altitude {altitude / 1000.0:.0f} km"
        ))
        satellite_index += 1

    for i in range(REQUESTED_MARS_HIGH_SATELLITES):
        inclination = 63.0
        raan = (70 + i * 360.0 / max(1, REQUESTED_MARS_HIGH_SATELLITES)) % 360
        argument_of_perigee = 270
        true_anomaly = (i * 147.0) % 360
        sat_color = color_for_satellite(satellite_index - 1)
        trail_color = trail_color_for_satellite(satellite_index - 1)

        draw_mars_elliptical_orbit(
            MARS_HIGH_PERIGEE_ALTITUDE_M,
            MARS_HIGH_APOGEE_ALTITUDE_M,
            color.gray(0.15),
            inclination_deg=inclination,
            raan_deg=raan,
            argument_of_perigee_deg=argument_of_perigee
        )
        built_satellites.append(create_mars_high_satellite(
            f"MARS-SAT-{REQUESTED_MARS_LOW_SATELLITES + REQUESTED_MARS_MID_SATELLITES + i + 1}",
            MARS_HIGH_PERIGEE_ALTITUDE_M,
            MARS_HIGH_APOGEE_ALTITUDE_M,
            inclination,
            raan,
            argument_of_perigee,
            true_anomaly,
            sat_color,
            trail_color
        ))
        satellite_index += 1

    return built_satellites


earth_satellites = build_requested_constellation()
mars_satellites = build_requested_mars_constellation(len(earth_satellites) + 1)
satellites = earth_satellites + mars_satellites
earth_asteroids = build_requested_asteroids(earth_satellites)
mars_asteroids = build_requested_mars_asteroids(mars_satellites)
asteroids = earth_asteroids + mars_asteroids
# Backward-compatible alias for older summary/status checks.
asteroid = asteroids[0] if len(asteroids) > 0 else None

# -----------------------------
# Collision settings
# -----------------------------
# These are enlarged for visibility.
satellite_collision_distance_m = 180000.0
debris_collision_distance_m = 240000.0
warning_distance_m = 700000.0

# Transfer spacecraft collision distances are intentionally enlarged for visibility,
# matching the rest of this VPython demo. The physical spacecraft radius remains
# in telemetry, while these thresholds make hazards observable in a live run.
spacecraft_collision_distance_m = 220000.0
spacecraft_warning_distance_m = 900000.0

active_visual_events = []
debris_particles = []
frame_count = 0


def active_satellite_count():
    count = 0

    for sat in satellites:
        if sat["active"]:
            count += 1

    return count

# Print the first full telemetry snapshot immediately at startup.
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

    timer_label.text = (
        f"Visual Time: {visual_time:.1f} s | "
        f"Physical Time: {physical_time:.0f} s | "
        f"Speed: {simulation_speed_multiplier}x"
    )

    update_transfer_spacecraft(visual_time)
    update_camera_follow()

    if frame_count % COMMAND_CHECK_INTERVAL_FRAMES == 0:
        check_for_command_update(satellites)

    if frame_count % telemetry_export_interval_frames() == 0:
        export_telemetry(
            frame_count,
            visual_time,
            physical_time,
            satellites,
            asteroids,
            debris_particles
        )

    # Update satellites
    for sat in satellites:
        update_satellite_physics(sat)
        update_satellite_visuals(sat)

    # Update asteroids
    for asteroid in asteroids:
        if asteroid["active"]:
            update_asteroid(asteroid)

    # Update debris
    for debris in debris_particles:
        update_debris_particle(debris)

    handle_debris_debris_collisions()

    if frame_count % 300 == 0:
        debris_particles = [d for d in debris_particles if d["active"]]

    # Asteroid collision checks
    if any(ast["active"] for ast in asteroids):
        warning_label.text = ""

        asteroid_collision_happened = False

        for asteroid in asteroids:
            if not asteroid["active"]:
                continue

            for sat in satellites:
                if not sat["active"]:
                    continue

                distance_m = mag(asteroid["position_m"] - sat["position_m"])

                if distance_m < warning_distance_m:
                    warning_label.text = f"WARNING: {asteroid['name']} CLOSE APPROACH"
                    warning_label.pos = sat["marker"].pos + vector(0, 0.45, 0)

                if distance_m < satellite_collision_distance_m:
                    collision_pos_m = (asteroid["position_m"] + sat["position_m"]) / 2
                    destroyed_velocity_mps = sat["velocity_mps"]

                    warning_label.text = f"COLLISION: {asteroid['name']} HIT {sat['name']}"
                    warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.55, 0)

                    hide_satellite(sat)
                    hide_object(asteroid)

                    visuals, new_debris = create_breakup_event(
                        collision_pos_m,
                        destroyed_velocity_mps
                    )

                    active_visual_events.append(visuals)
                    debris_particles.extend(new_debris)
                    asteroid_collision_happened = True
                    break

            if asteroid_collision_happened:
                break

    # Transfer spacecraft collision checks
    if mars_transfer_spacecraft_active:
        spacecraft_collision_happened = False
        spacecraft_pos_m = transfer_spacecraft_position_m()
        spacecraft_vel_mps = transfer_spacecraft_velocity_mps()

        # Asteroids can destroy the Mars transfer spacecraft.
        for asteroid in asteroids:
            if spacecraft_collision_happened:
                break

            if not asteroid["active"]:
                continue

            distance_m = mag(asteroid["position_m"] - spacecraft_pos_m)

            if distance_m < spacecraft_warning_distance_m:
                warning_label.text = f"WARNING: {asteroid['name']} NEAR MARS-XFER-1"
                warning_label.pos = mars_transfer_spacecraft.pos + vector(0, 0.50, 0)

            if distance_m < spacecraft_collision_distance_m:
                collision_pos_m = (asteroid["position_m"] + spacecraft_pos_m) / 2
                warning_label.text = f"COLLISION: {asteroid['name']} HIT MARS-XFER-1"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.60, 0)

                hide_object(asteroid)
                hide_transfer_spacecraft(f"asteroid impact from {asteroid['name']}")

                visuals, new_debris = create_breakup_event(
                    collision_pos_m,
                    spacecraft_vel_mps
                )
                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)
                spacecraft_collision_happened = True

        # Satellites can also collide with the transfer spacecraft. This matters
        # near Earth departure and near Mars arrival, where local constellations exist.
        for sat in satellites:
            if spacecraft_collision_happened:
                break

            if not sat["active"]:
                continue

            distance_m = mag(sat["position_m"] - spacecraft_pos_m)

            if distance_m < spacecraft_collision_distance_m:
                collision_pos_m = (sat["position_m"] + spacecraft_pos_m) / 2
                warning_label.text = f"COLLISION: MARS-XFER-1 HIT {sat['name']}"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.60, 0)

                hide_satellite(sat)
                hide_transfer_spacecraft(f"spacecraft-satellite impact with {sat['name']}")

                combined_velocity = (spacecraft_vel_mps + sat["velocity_mps"]) / 2
                visuals, new_debris = create_breakup_event(
                    collision_pos_m,
                    combined_velocity
                )
                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)
                spacecraft_collision_happened = True

        # Existing orbital debris can hit the spacecraft and destroy it.
        for debris in debris_particles:
            if spacecraft_collision_happened:
                break

            if not debris["active"]:
                continue

            if debris["age"] < debris["can_collide_after"]:
                continue

            distance_m = mag(debris["position_m"] - spacecraft_pos_m)

            if distance_m < spacecraft_collision_distance_m:
                collision_pos_m = (debris["position_m"] + spacecraft_pos_m) / 2
                warning_label.text = "DEBRIS HIT MARS-XFER-1"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.60, 0)

                hide_debris(debris)
                hide_transfer_spacecraft("orbital debris impact")

                combined_velocity = (spacecraft_vel_mps + debris["velocity_mps"]) / 2
                visuals, new_debris = create_breakup_event(
                    collision_pos_m,
                    combined_velocity
                )
                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)
                spacecraft_collision_happened = True

    # Debris to satellite chain reaction
    debris_collision_happened = False

    for debris in debris_particles:
        if not debris["active"]:
            continue

        if debris["age"] < debris["can_collide_after"]:
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

                visuals, new_debris = create_breakup_event(
                    collision_pos_m,
                    destroyed_velocity_mps
                )

                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)

                debris_collision_happened = True
                break

        if debris_collision_happened:
            break

    # Update impact flash visuals
    updated_events = []

    for event in active_visual_events:
        updated = update_visual_event(event)

        if len(updated) > 0:
            updated_events.append(updated)

    active_visual_events = updated_events

    # Status text
    if all(not ast["active"] for ast in asteroids) and len(active_visual_events) == 0:
        if active_satellite_count() > 0 and len(debris_particles) > 0:
            if warning_label.text == "" or warning_label.text.startswith("COLLISION"):
                warning_label.text = "ORBITING DEBRIS FIELD ACTIVE"
                warning_label.pos = vector(0, 2.0, 0)
        elif active_satellite_count() == 0:
            warning_label.text = "ALL SATELLITES DESTROYED"
            warning_label.pos = vector(0, 2.0, 0)

    # Planet rotation for appearance
    earth.rotate(angle=EARTH_ROTATION_RATE * dt * 0.08, axis=vector(0, 0, 1))
    mars.rotate(angle=MARS_ROTATION_RATE * dt * 0.08, axis=vector(0, 0, 1))
