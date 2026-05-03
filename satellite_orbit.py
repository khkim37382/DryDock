from vpython import *
import json
import os
import time
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

if REQUESTED_LEO_SATELLITES + REQUESTED_MEO_SATELLITES + REQUESTED_HEO_SATELLITES == 0:
    print("You entered 0 total satellites, so the sim will create one default LEO satellite so the demo still runs.")
    REQUESTED_LEO_SATELLITES = 1

print(
    f"\nCreating constellation: "
    f"{REQUESTED_LEO_SATELLITES} LEO, "
    f"{REQUESTED_MEO_SATELLITES} MEO, "
    f"{REQUESTED_HEO_SATELLITES} HEO satellites.\n"
)

# -----------------------------
# Scene setup
# -----------------------------
scene.title = "Physics-Based Satellite Collision / Debris Simulation"
scene.width = 1200
scene.height = 800
scene.background = color.black
scene.forward = vector(-1, -0.35, -0.9)
scene.center = vector(0, 0, 0)
scene.range = 4.8

scene.userspin = True
scene.userzoom = True
scene.userpan = True

# -----------------------------
# Real physical constants
# -----------------------------
MU_EARTH = 3.986004418e14          # m^3/s^2
R_EARTH = 6371008.4                # m
EARTH_ROTATION_RATE = 7.2921159e-5 # rad/s

VISUAL_SCALE = 1 / R_EARTH         # 1 scene unit = 1 Earth radius

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

# Terminal telemetry test output for the quantum routing / decision system.
# For now, this prints JSON directly in Terminal instead of writing a file.
TELEMETRY_OUTPUT_MODE = "terminal"
BASE_TELEMETRY_SAMPLE_HZ = 10.0


def effective_telemetry_sample_hz():
    return BASE_TELEMETRY_SAMPLE_HZ * simulation_speed_multiplier


def telemetry_export_interval_frames():
    return max(1, int(round(rate_value / effective_telemetry_sample_hz())))


print("Telemetry output mode:", TELEMETRY_OUTPUT_MODE)
print(f"Telemetry will print full JSON snapshots in this terminal at {effective_telemetry_sample_hz():.1f} Hz.")

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
    text="Physics: real Earth mu, real Earth radius, two-body gravity",
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
    text="Telemetry output: terminal test mode active, 10 Hz | speed 1x",
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
        f"telemetry {effective_telemetry_sample_hz():.1f} Hz | "
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
    active_asteroid_count = 1 if asteroid is not None and asteroid["active"] else 0
    active_hazard_count = active_asteroid_count + active_debris_count

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
            "active_debris": active_debris_count,
            "total_active_hazards": active_hazard_count,
            "active_visual_impact_events": count_active_visual_events()
        },
        "system_summary": {
            "base_telemetry_sample_hz": BASE_TELEMETRY_SAMPLE_HZ,
            "effective_telemetry_sample_hz": effective_telemetry_sample_hz(),
            "speed_multiplier": simulation_speed_multiplier,
            "physics_timestep_s": dt,
            "base_physics_timestep_s": BASE_DT,
            "earth_mu_m3_s2": MU_EARTH,
            "earth_radius_m": R_EARTH
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
scene.append_to_caption("\n")

# -----------------------------
# Utility
# -----------------------------
def meters_to_scene(v):
    return v * VISUAL_SCALE


def scene_to_meters(v):
    return v / VISUAL_SCALE


def circular_speed(radius_m):
    return sqrt(MU_EARTH / radius_m)


def gravity_acceleration(position_m):
    r = mag(position_m)
    if r == 0:
        return vector(0, 0, 0)
    return -MU_EARTH * position_m / (r ** 3)


def altitude_m(position_m):
    return mag(position_m) - R_EARTH


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


def object_state_dict(object_id, object_type, active, position_m, velocity_mps, mass_kg=None, radius_m=None, selected_for_data=False, measurement_timestamp=None, extra=None):
    altitude = altitude_m(position_m)
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
        "specific_orbital_energy_j_per_kg": orbital_energy_j_per_kg(position_m, velocity_mps)
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
def create_satellite_from_state(name, position_m, velocity_mps, sat_color, trail_color, orbit_class="custom", orbit_description="custom orbit"):
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
        "orbit_description": orbit_description
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


def update_satellite_physics(sat):
    if not sat["active"]:
        return

    sat["velocity_mps"] += gravity_acceleration(sat["position_m"]) * dt
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
def create_physical_asteroid(target_sat, target_collision_visual_time):
    # Put asteroid in the same orbital plane as SAT-2 but retrograde.
    # This gives a physically valid orbit and keeps it visible.
    sat_r = mag(target_sat["position_m"])
    sat_speed = mag(target_sat["velocity_mps"])
    omega_sat = sat_speed / sat_r

    asteroid_altitude = altitude_m(target_sat["position_m"]) + 50000.0
    asteroid_radius = R_EARTH + asteroid_altitude
    asteroid_speed = circular_speed(asteroid_radius)
    omega_ast = asteroid_speed / asteroid_radius

    target_physical_time = target_collision_visual_time * rate_value * dt
    separation_angle = (omega_sat + omega_ast) * target_physical_time
    separation_deg = degrees(separation_angle % (2 * pi))

    inclination_deg = 70
    raan_deg = 45
    sat_phase_deg = 35
    asteroid_phase_deg = sat_phase_deg + separation_deg

    position_m, velocity_mps = make_orbit_state(
        altitude=asteroid_altitude,
        inclination_deg=inclination_deg,
        raan_deg=raan_deg,
        phase_deg=asteroid_phase_deg,
        prograde=False
    )

    marker = sphere(
        pos=meters_to_scene(position_m),
        radius=0.075,
        color=color.red,
        emissive=True,
        make_trail=True,
        trail_color=color.red,
        retain=600
    )
    marker.trail_radius = 0.006

    asteroid_label = label(
        pos=marker.pos + vector(0.14, 0.14, 0),
        text="ASTEROID",
        height=11,
        box=False,
        color=color.red
    )

    return {
        "name": "ASTEROID",
        "position_m": position_m,
        "velocity_mps": velocity_mps,
        "marker": marker,
        "label": asteroid_label,
        "active": True,
        "mass_kg": 1000.0,
        "physical_radius_m": 2.0
    }


def update_asteroid(asteroid):
    if not asteroid["active"]:
        return

    asteroid["velocity_mps"] += gravity_acceleration(asteroid["position_m"]) * dt
    asteroid["position_m"] += asteroid["velocity_mps"] * dt

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

    debris["velocity_mps"] += gravity_acceleration(debris["position_m"]) * dt
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
def build_telemetry_payload(frame_count, visual_time, physical_time, satellites, asteroid, debris_particles):
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
                "orbit_description": sat.get("orbit_description", "unknown")
            }
        ))

    asteroid_states = []
    if asteroid is not None:
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
            extra={"collision_threat": bool(asteroid["active"])}
        ))

    debris_states = []
    for index, debris in enumerate(active_debris):
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
            extra={
                "age_frames": int(debris.get("age", 0)),
                "life_frames_remaining": int(debris.get("life", 0)),
                "recent_collision_cooldown_frames": int(debris.get("recent_collision_cooldown", 0))
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


def export_telemetry(frame_count, visual_time, physical_time, satellites, asteroid, debris_particles):
    payload = build_telemetry_payload(
        frame_count,
        visual_time,
        physical_time,
        satellites,
        asteroid,
        debris_particles
    )

    if TELEMETRY_OUTPUT_MODE == "terminal":
        terminal_payload = make_terminal_payload(payload)
        print("\n========== SPACE STATE TELEMETRY ==========")
        print(json.dumps(terminal_payload, indent=2))
        print("===========================================\n")

        telemetry_label.text = (
            f"Telemetry output: terminal {effective_telemetry_sample_hz():.1f} Hz | speed {simulation_speed_multiplier}x | "
            f"{payload['counts']['active_satellites']} sats, "
            f"{payload['counts']['active_debris']} debris | "
            f"{payload['utc_iso']}"
        )


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


satellites = build_requested_constellation()

# Keep the asteroid demo aimed at a real satellite if at least one exists.
# Prefer the second satellite so the first one often survives for comparison.
asteroid_target_satellite = satellites[min(1, len(satellites) - 1)] if len(satellites) > 0 else None
asteroid = create_physical_asteroid(asteroid_target_satellite, desired_visual_collision_time) if asteroid_target_satellite is not None else None

# -----------------------------
# Collision settings
# -----------------------------
# These are enlarged for visibility.
satellite_collision_distance_m = 180000.0
debris_collision_distance_m = 240000.0
warning_distance_m = 700000.0

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
export_telemetry(0, 0.0, 0.0, satellites, asteroid, debris_particles)

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

    if frame_count % COMMAND_CHECK_INTERVAL_FRAMES == 0:
        check_for_command_update(satellites)

    if frame_count % telemetry_export_interval_frames() == 0:
        export_telemetry(
            frame_count,
            visual_time,
            physical_time,
            satellites,
            asteroid,
            debris_particles
        )

    # Update satellites
    for sat in satellites:
        update_satellite_physics(sat)
        update_satellite_visuals(sat)

    # Update asteroid
    if asteroid is not None and asteroid["active"]:
        update_asteroid(asteroid)

    # Update debris
    for debris in debris_particles:
        update_debris_particle(debris)

    handle_debris_debris_collisions()

    if frame_count % 300 == 0:
        debris_particles = [d for d in debris_particles if d["active"]]

    # Asteroid collision checks
    if asteroid is not None and asteroid["active"]:
        warning_label.text = ""

        for sat in satellites:
            if not sat["active"]:
                continue

            distance_m = mag(asteroid["position_m"] - sat["position_m"])

            if distance_m < warning_distance_m:
                warning_label.text = "WARNING: CLOSE APPROACH DETECTED"
                warning_label.pos = sat["marker"].pos + vector(0, 0.45, 0)

            if distance_m < satellite_collision_distance_m:
                collision_pos_m = (asteroid["position_m"] + sat["position_m"]) / 2
                destroyed_velocity_mps = sat["velocity_mps"]

                warning_label.text = f"COLLISION: {sat['name']} DESTROYED"
                warning_label.pos = meters_to_scene(collision_pos_m) + vector(0, 0.55, 0)

                hide_satellite(sat)
                hide_object(asteroid)

                visuals, new_debris = create_breakup_event(
                    collision_pos_m,
                    destroyed_velocity_mps
                )

                active_visual_events.append(visuals)
                debris_particles.extend(new_debris)
                break

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
    if (asteroid is None or not asteroid["active"]) and len(active_visual_events) == 0:
        if active_satellite_count() > 0 and len(debris_particles) > 0:
            if warning_label.text == "" or warning_label.text.startswith("COLLISION"):
                warning_label.text = "ORBITING DEBRIS FIELD ACTIVE"
                warning_label.pos = vector(0, 2.0, 0)
        elif active_satellite_count() == 0:
            warning_label.text = "ALL SATELLITES DESTROYED"
            warning_label.pos = vector(0, 2.0, 0)

    # Earth rotation for appearance
    earth.rotate(angle=EARTH_ROTATION_RATE * dt * 0.08, axis=vector(0, 0, 1))