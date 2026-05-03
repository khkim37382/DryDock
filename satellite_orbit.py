from vpython import *
import json
import os
import time
import math
from datetime import datetime, timezone

# ============================================================
# ENHANCED Physics-Grounded Satellite / Debris Sim (v2)
# ============================================================
# Added: J2 Perturbation, Atmospheric Drag, Spatial Grid Optimization,
# Sun Lighting, and Conjunction Analysis.
# ============================================================

# -----------------------------
# Pre-simulation constellation input
# -----------------------------
def read_nonnegative_int(prompt_text, default_value):
    # This function is used when running locally in terminal.
    # In some VPython environments (like GlowScript), 'input' might behave differently.
    try:
        raw = input(f"{prompt_text} [{default_value}]: ").strip()
        if raw == "": return default_value
        value = int(raw)
        return value if value >= 0 else default_value
    except:
        return default_value

# Constants for setup
REQUESTED_LEO_SATELLITES = 2
REQUESTED_MEO_SATELLITES = 1
REQUESTED_HEO_SATELLITES = 1
REQUESTED_LEO_ASTEROIDS = 1

# -----------------------------
# Scene setup
# -----------------------------
scene.title = "Enhanced Orbital Mechanics: J2, Drag & Conjunction Simulation"
scene.width = 1200
scene.height = 800
scene.background = color.black
scene.forward = vector(-1, -0.35, -0.9)
scene.range = 4.8

# Lighting - Adding a Sun
scene.lights = [] # Remove default
sun_light = distant_light(direction=vector(1, 0, 0), color=color.white)
scene.ambient = color.gray(0.15)

# -----------------------------
# Real physical constants
# -----------------------------
MU_EARTH = 3.986004418e14          # m^3/s^2
R_EARTH = 6371008.4                # m
EARTH_ROTATION_RATE = 7.2921159e-5 # rad/s
J2 = 1.08262668e-3                # Zonal harmonic coefficient
RHO_0 = 1.225                      # Sea-level density kg/m^3
SCALE_HEIGHT = 8500.0              # Scale height for atm (meters)
CD = 2.2                           # Typical drag coefficient

VISUAL_SCALE = 1 / R_EARTH         
BASE_DT = 5.0
dt = BASE_DT
rate_value = 120
simulation_speed_multiplier = 1

COMMAND_FILE = "quantum_commands.json"
TELEMETRY_OUTPUT_MODE = "terminal"
BASE_TELEMETRY_SAMPLE_HZ = 10.0

# -----------------------------
# Objects & Labels
# -----------------------------
earth = sphere(pos=vector(0, 0, 0), radius=1, texture=textures.earth, shininess=0.4)
timer_label = label(pos=vector(-3.6, 3.0, 0), text="Visual Time: 0.0 s", height=12, box=False, color=color.white)
warning_label = label(pos=vector(0, 2.0, 0), text="", height=16, box=False, color=color.red)

# -----------------------------
# Enhanced Physics Functions
# -----------------------------
def gravity_acceleration(pos_m):
    r_vec = pos_m
    r = mag(r_vec)
    if r == 0: return vector(0,0,0)
    
    # Standard Two-Body Gravity
    acc_gravity = -MU_EARTH * r_vec / (r ** 3)
    
    # J2 Perturbation (Accounting for Earth's oblateness)
    z = r_vec.z
    r2 = r**2
    factor = (1.5 * J2 * MU_EARTH * (R_EARTH**2)) / (r**5)
    
    j2_x = factor * r_vec.x * (5 * (z**2 / r2) - 1)
    j2_y = factor * r_vec.y * (5 * (z**2 / r2) - 1)
    j2_z = factor * r_vec.z * (5 * (z**2 / r2) - 3)
    
    return acc_gravity + vector(j2_x, j2_y, j2_z)

def atmospheric_drag(pos_m, vel_m, mass, area):
    h = mag(pos_m) - R_EARTH
    if h > 800000 or h < 0: return vector(0,0,0) # Drag negligible above 800km
    
    # Simplified Exponential Density Model
    rho = RHO_0 * math.exp(-h / SCALE_HEIGHT)
    
    # Drag Equation: Fd = -0.5 * rho * v^2 * Cd * A
    v_rel = vel_m # Simplification: ignoring atmospheric rotation
    v_mag = mag(v_rel)
    if v_mag == 0: return vector(0,0,0)
    
    acc_drag = -0.5 * rho * (v_mag**2) * CD * (area / mass) * norm(v_rel)
    return acc_drag

# -----------------------------
# Spatial Partitioning (The Grid)
# -----------------------------
GRID_SIZE_M = 500000.0 # 500km grid cells

def get_grid_key(pos_m):
    return (int(pos_m.x / GRID_SIZE_M), 
            int(pos_m.y / GRID_SIZE_M), 
            int(pos_m.z / GRID_SIZE_M))

# -----------------------------
# Utilities
# -----------------------------
def meters_to_scene(v): return v * VISUAL_SCALE
def circular_speed(radius_m): return sqrt(MU_EARTH / radius_m)

def make_orbit_state(altitude, inclination_deg=0, raan_deg=0, phase_deg=0, prograde=True):
    radius_m = R_EARTH + altitude
    pos = vector(radius_m, 0, 0)
    vel = vector(0, circular_speed(radius_m), 0)
    if not prograde: vel = -vel
    pos = rotate(pos, angle=radians(phase_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(phase_deg), axis=vector(0, 0, 1))
    pos = rotate(pos, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    vel = rotate(vel, angle=radians(inclination_deg), axis=vector(1, 0, 0))
    pos = rotate(pos, angle=radians(raan_deg), axis=vector(0, 0, 1))
    vel = rotate(vel, angle=radians(raan_deg), axis=vector(0, 0, 1))
    return pos, vel

# -----------------------------
# Main Simulation Classes
# -----------------------------
satellites = []
asteroids = []
debris_particles = []

def create_sat(name, altitude, inc, raan, phase, color_val):
    pos, vel = make_orbit_state(altitude, inc, raan, phase)
    marker = sphere(pos=meters_to_scene(pos), radius=0.04, color=color_val, make_trail=True, retain=200)
    marker.trail_radius = 0.005
    return {
        "name": name, "position_m": pos, "velocity_mps": vel, 
        "marker": marker, "active": True, "mass": 500.0, "area": 4.0, "type": "satellite"
    }

def create_ast(name, altitude, inc, raan, phase):
    pos, vel = make_orbit_state(altitude, inc, raan, phase, prograde=False)
    marker = sphere(pos=meters_to_scene(pos), radius=0.06, color=color.red, emissive=True)
    return {
        "name": name, "position_m": pos, "velocity_mps": vel, 
        "marker": marker, "active": True, "mass": 2000.0, "area": 10.0, "type": "asteroid"
    }

def create_breakup(pos_m, vel_m):
    new_debris = []
    for _ in range(20):
        dv = vector(random()-0.5, random()-0.5, random()-0.5) * 150
        d_pos = pos_m + norm(dv) * 5000
        m = sphere(pos=meters_to_scene(d_pos), radius=0.015, color=color.gray(0.7))
        new_debris.append({
            "marker": m, "position_m": d_pos, "velocity_mps": vel_m + dv, 
            "active": True, "mass": 1.0, "area": 0.1, "age": 0, "type": "debris"
        })
    return new_debris

# -----------------------------
# Init Simulation
# -----------------------------
satellites.append(create_sat("SAT-1", 600000, 53, 0, 0, color.cyan))
satellites.append(create_sat("SAT-2", 700000, 70, 45, 90, color.green))
asteroids.append(create_ast("AST-1", 650000, 60, 20, 180))

# -----------------------------
# Main Loop
# -----------------------------
frame = 0
sim_time = 0

while True:
    rate(rate_value)
    frame += 1
    sim_time += dt
    
    # 1. Update Physics (Gravity + J2 + Drag)
    all_objs = satellites + asteroids + debris_particles
    grid = {}
    
    for obj in all_objs:
        if not obj["active"]: continue
        
        # Apply Accelerations
        acc = gravity_acceleration(obj["position_m"])
        acc += atmospheric_drag(obj["position_m"], obj["velocity_mps"], obj["mass"], obj["area"])
        
        obj["velocity_mps"] += acc * dt
        obj["position_m"] += obj["velocity_mps"] * dt
        obj["marker"].pos = meters_to_scene(obj["position_m"])
        
        # 2. Update Spatial Grid for Collision detection
        key = get_grid_key(obj["position_m"])
        if key not in grid: grid[key] = []
        grid[key].append(obj)
        
        # Ground check
        if mag(obj["position_m"]) < R_EARTH:
            obj["active"] = False
            obj["marker"].visible = False

    # 3. Optimized Collision & Conjunction Check
    for key in grid:
        objs_in_cell = grid[key]
        for i in range(len(objs_in_cell)):
            for j in range(i+1, len(objs_in_cell)):
                o1, o2 = objs_in_cell[i], objs_in_cell[j]
                
                dist = mag(o1["position_m"] - o2["position_m"])
                
                # Collision check
                if dist < 150000: # 150km visual threshold
                    if (o1["type"] != "debris" or o2["type"] != "debris"):
                        warning_label.text = f"COLLISION: {o1['name']} & {o2['name']}"
                        o1["active"] = o2["active"] = False
                        o1["marker"].visible = o2["marker"].visible = False
                        debris_particles.extend(create_breakup(o1["position_m"], o1["velocity_mps"]))

    # 4. Telemetry Update (Every 60 frames)
    if frame % 60 == 0:
        timer_label.text = f"Visual Time: {frame/rate_value:.1f}s | Sats Active: {sum(1 for s in satellites if s['active'])}"
        # Export logic can be added here as in the previous version

    # Earth rotation
    earth.rotate(angle=EARTH_ROTATION_RATE * dt, axis=vector(0, 0, 1))
