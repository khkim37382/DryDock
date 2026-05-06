# ============================================================
# ground_station_processor.py  —  v2
# ============================================================
# Complements ground_station_manager.py on the GSM side.
#
# Reads incoming telemetry JSON files from:
#     received_transmissions/
# (written by ground_station_manager.py, retained for 5 hours)
#
# For each file it:
#   1. Extracts the object_id and object_type.
#   2. Pulls the timing fields the SMS/GSM scripts already
#      stamped onto the payload (this file NEVER recomputes
#      times, only reads them).
#   3. Computes an HONESTLY-NAMED set of derived fields:
#         current_uplink_trip_ns        (this packet's SMS->GSM trip)
#         last_known_downlink_trip_ns   (most recent GSM->SMS trip
#                                         that the SMS observed before
#                                         it sent THIS packet)
#         last_downlink_age_s           (how stale the downlink trip is)
#         estimated_link_latency_ns     (uplink + downlink, NOT a true
#                                         roundtrip — see notes below)
#   4. Flat-packs the rich nested telemetry into a per-type
#      schema and appends to an in-memory buffer.
#   5. Flushes per-object buffers to Parquet at every BATCH_SIZE
#      rows or BATCH_FLUSH_INTERVAL_S seconds.
#   6. Maintains per-type master_index.parquet files plus a
#      top-level summary.
#   7. Cleanup thread deletes Parquet files older than
#      RETENTION_HOURS per object.
#
# WHY "ESTIMATED LINK LATENCY" AND NOT "ROUNDTRIP"
# ─────────────────────────────────────────────────
# A true roundtrip = time for ONE specific message to go up and its
# response to come back down.  In our pipeline the satellite-side
# telemetry is fired CONTINUOUSLY at 10 Hz — most telemetry packets
# are NOT replies to any specific GSM->SMS command.  When the SMS
# attaches "last_gsm_to_sms_trip_ns" it's the most recent trip it
# observed, which could be from a command that was issued seconds
# or minutes ago.  Adding that to the current uplink does not give
# a real roundtrip — it gives an "estimated link latency" that is
# useful as a feature but should never be called a roundtrip.
#
# Storage layout (TYPE-SEPARATED so schemas stay clean):
#   training_data/
#       satellites/
#           master_index.parquet
#           SAT-1/
#               batch_000001.parquet
#               batch_000002.parquet
#           SAT-2/
#               batch_000001.parquet
#       asteroids/
#           master_index.parquet
#           AST-1/
#               batch_000001.parquet
#       debris/
#           master_index.parquet
#           DEBRIS-001/
#               batch_000001.parquet
#       summary.parquet         (one row per type, top-level stats)
#
# Each row in a batch Parquet is one complete ping/snapshot
# with type-appropriate columns flat-packed for AI training.
#
# Dependencies:
#   pip install paho-mqtt watchdog pandas pyarrow
# ============================================================

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ──────────────────────────────────────────────────────────────
# PATHS (edit if your layout differs)
# ──────────────────────────────────────────────────────────────

# Directory the GSM script writes incoming telemetry into.
SAVE_DIR = "received_transmissions"

# Root of the AI training dataset.
TRAINING_DIR = "training_data"

# ──────────────────────────────────────────────────────────────
# TUNING PARAMETERS
# ──────────────────────────────────────────────────────────────

BATCH_SIZE              = 1000       # rows per Parquet batch
BATCH_FLUSH_INTERVAL_S  = 60.0       # max seconds before forced flush
RETENTION_HOURS         = 5
CLEANUP_INTERVAL_S      = 120
PARQUET_COMPRESSION     = "snappy"

# Object types we recognise — each lives in its own subdirectory.
KNOWN_TYPES = ("satellite", "asteroid", "debris")


def type_to_dirname(obj_type: str) -> str:
    """Map 'satellite' → 'satellites' for plural directory names."""
    if obj_type == "satellite": return "satellites"
    if obj_type == "asteroid":  return "asteroids"
    if obj_type == "debris":    return "debris"
    return "unknown"


# ──────────────────────────────────────────────────────────────
# GLOBAL STATE  (protected by per-object locks)
# ──────────────────────────────────────────────────────────────

# (object_id, object_type) keyed buffers and metadata
buffers: dict[tuple, list]                 = {}     # (oid, type) -> [row, ...]
last_flush_time: dict[tuple, float]        = {}
batch_seq: dict[tuple, int]                = {}
ping_seq: dict[tuple, int]                 = {}
object_stats: dict[tuple, dict]            = {}
object_locks: dict[tuple, threading.Lock]  = {}

index_lock = threading.Lock()  # guards master-index writes & object_stats

os.makedirs(TRAINING_DIR, exist_ok=True)
os.makedirs(SAVE_DIR,     exist_ok=True)
for t in KNOWN_TYPES:
    os.makedirs(os.path.join(TRAINING_DIR, type_to_dirname(t)), exist_ok=True)


# ──────────────────────────────────────────────────────────────
# OBJECT LOCK
# ──────────────────────────────────────────────────────────────
def get_object_lock(key: tuple) -> threading.Lock:
    if key not in object_locks:
        with index_lock:
            if key not in object_locks:
                object_locks[key] = threading.Lock()
    return object_locks[key]


# ──────────────────────────────────────────────────────────────
# SAFE TYPE COERCERS
# ──────────────────────────────────────────────────────────────
def _f(v, default=None):
    """Safe float."""
    try:
        if v is None: return default
        return float(v)
    except (TypeError, ValueError):
        return default

def _i(v, default=None):
    """Safe int."""
    try:
        if v is None: return default
        return int(v)
    except (TypeError, ValueError):
        return default

def _s(v, default=None):
    """Safe str."""
    if v is None: return default
    return str(v)

def _b(v, default=False):
    """Safe bool."""
    if v is None: return default
    if isinstance(v, bool): return v
    if isinstance(v, (int, float)): return bool(v)
    if isinstance(v, str): return v.lower() in ("true", "1", "yes")
    return default


# ──────────────────────────────────────────────────────────────
# COMMON / SHARED COLUMNS
# ──────────────────────────────────────────────────────────────
# These columns appear on every type. They cover identity, timing,
# position/velocity, and dispatcher metadata — the universal
# features any AI model would need.
# ──────────────────────────────────────────────────────────────

def _common_fields(raw: dict,
                   *,
                   object_id: str,
                   object_type: str,
                   sequence_id: int,
                   received_utc: str,
                   current_uplink_trip_ns,
                   last_downlink_trip_ns,
                   last_downlink_age_s,
                   estimated_link_latency_ns) -> dict:

    pos = raw.get("position_m_eci", {}) or {}
    vel = raw.get("velocity_mps_eci", {}) or {}
    cp  = raw.get("dispatcher_comm_path", {}) or {}

    return {
        # ── identity ─────────────────────────────────────────
        "sequence_id":   sequence_id,
        "object_id":     object_id,
        "object_type":   object_type,
        "received_utc":  received_utc,

        # ── HONEST link timing ───────────────────────────────
        "current_uplink_trip_ns":      _i(current_uplink_trip_ns),
        "current_uplink_trip_ms":      _f(current_uplink_trip_ns / 1e6
                                           if current_uplink_trip_ns is not None
                                           else None),
        "last_known_downlink_trip_ns": _i(last_downlink_trip_ns),
        "last_known_downlink_trip_ms": _f(last_downlink_trip_ns / 1e6
                                           if last_downlink_trip_ns is not None
                                           else None),
        "last_downlink_age_s":         _f(last_downlink_age_s),
        "estimated_link_latency_ns":   _i(estimated_link_latency_ns),
        "estimated_link_latency_ms":   _f(estimated_link_latency_ns / 1e6
                                           if estimated_link_latency_ns is not None
                                           else None),

        # ── raw timestamps (so AI has both clocks) ───────────
        "sms_send_time_ns":    _i(raw.get("sms_send_time_ns")),
        "gsm_receive_time_ns": _i(raw.get("gsm_receive_time_ns")),

        # ── simulation clocks ────────────────────────────────
        "sim_frame":            _i(raw.get("sim_frame")),
        "sim_physical_time_s":  _f(raw.get("sim_physical_time_s")),
        "sim_visual_time_s":    _f(raw.get("sim_visual_time_s")),
        "sim_unix_time_s":      _f(raw.get("sim_unix_time_s")),
        "sim_utc_iso":          _s(raw.get("sim_utc_iso")),

        # ── dispatcher comm path ─────────────────────────────
        "comm_link_type":              _s(cp.get("link_type")),
        "comm_ground_station":         _s(cp.get("ground_station")),
        "comm_tdrs_relay":             _s(cp.get("tdrs_relay")),
        "comm_propagation_delay_ms":   _f(cp.get("propagation_delay_ms")),
        "comm_distance_m":             _f(cp.get("distance_m")),

        # ── position & velocity ──────────────────────────────
        "pos_x_m":  _f(pos.get("x")),
        "pos_y_m":  _f(pos.get("y")),
        "pos_z_m":  _f(pos.get("z")),
        "vel_x_mps": _f(vel.get("x")),
        "vel_y_mps": _f(vel.get("y")),
        "vel_z_mps": _f(vel.get("z")),
        "speed_mps":             _f(raw.get("speed_mps")),

        # ── orbital state ────────────────────────────────────
        "altitude_m":            _f(raw.get("altitude_m")),
        "altitude_km":           _f(raw.get("altitude_km")),
        "orbit_class":           _s(raw.get("orbit_class")),
        "orbit_description":     _s(raw.get("orbit_description")),
        "specific_orbital_energy_j_per_kg":
            _f(raw.get("specific_orbital_energy_j_per_kg")),
        "distance_from_earth_center_m":
            _f(raw.get("distance_from_earth_center_m")),

        # ── object physical ──────────────────────────────────
        "active":             _b(raw.get("active"), True),
        "mass_kg":             _f(raw.get("mass_kg")),
        "physical_radius_m":   _f(raw.get("physical_radius_m")),
        "selected_for_data":   _b(raw.get("selected_for_data"), False),
        "central_body":        _s(raw.get("central_body"), "Earth"),

        # ── blackout recovery flag (set by dispatcher) ───────
        "was_blackout_recovery": _b(raw.get("was_blackout_recovery"), False),
        "blackout_duration_s":   _f(raw.get("blackout_duration_s")),
    }


# ──────────────────────────────────────────────────────────────
# PER-TYPE FLATTENERS
# ──────────────────────────────────────────────────────────────
#
# These pull the relevant nested sub-dicts out of the sim's payload.
# IMPORTANT: every one of these field paths has been verified against
# the actual sim functions (build_solar_power_thermal_payloads,
# build_communication_link_payload, build_sensor_fusion_state, etc).
# ──────────────────────────────────────────────────────────────

def _flatten_satellite(raw: dict, common: dict) -> dict:
    row = dict(common)

    sun = raw.get("sunlight_state",      {}) or {}
    sp  = raw.get("solar_panel_system",  {}) or {}
    vs  = raw.get("voltage_sensors",     {}) or {}
    pw  = raw.get("power_system",        {}) or {}
    th  = raw.get("thermal_profile",     {}) or {}
    cl  = raw.get("communication_link",  {}) or {}
    rad = raw.get("radiation",           {}) or {}
    sf  = raw.get("sensor_fusion_state", {}) or {}
    att = raw.get("attitude_state",      {}) or {}
    ev  = raw.get("environment_vectors", {}) or {}

    # ── sunlight ─────────────────────────────────────────────
    row["in_sunlight"]         = _b(sun.get("in_sunlight"), True)
    row["in_eclipse"]          = _b(sun.get("in_eclipse"),  False)
    row["sun_exposure_factor"] = _f(sun.get("sun_exposure_factor"), 1.0)
    row["eclipse_body"]        = _s(sun.get("eclipse_body"))

    # ── solar panel system ───────────────────────────────────
    row["panel_efficiency_factor"] = _f(sp.get("panel_efficiency_factor"))
    row["panel_incidence_deg"]     = _f(sp.get("sun_incidence_angle_deg"))
    row["solar_irradiance_w_m2"]   = _f(sp.get("solar_irradiance_w_m2"))
    row["solar_panel_area_m2"]     = _f(sp.get("panel_area_m2"))
    row["panel_efficiency"]        = _f(sp.get("panel_efficiency"))
    row["est_solar_generation_w"]  = _f(sp.get("estimated_solar_generation_w"))
    row["max_solar_generation_w"]  = _f(sp.get("max_solar_generation_w"))
    row["recommended_panel_rotation_deg"] = _f(sp.get("recommended_panel_rotation_deg"))
    row["solar_optimization_state"]= _s(sp.get("solar_optimization_state"))
    row["charging_priority"]       = _s(sp.get("charging_priority"))

    # ── voltage sensors ──────────────────────────────────────
    row["solar_panel_voltage_v"] = _f(vs.get("solar_panel_voltage_v"))
    row["solar_panel_current_a"] = _f(vs.get("solar_panel_current_a"))
    row["battery_voltage_v"]     = _f(vs.get("battery_voltage_v"))
    row["bus_voltage_v"]         = _f(vs.get("bus_voltage_v"))
    row["bus_current_draw_a"]    = _f(vs.get("bus_current_draw_a"))
    row["net_charge_current_a"]  = _f(vs.get("net_charge_current_a"))

    # ── power system ─────────────────────────────────────────
    row["battery_percent"]      = _f(pw.get("battery_percent"))
    row["battery_capacity_wh"]  = _f(pw.get("battery_capacity_wh"))
    row["solar_generation_w"]   = _f(pw.get("solar_generation_w"))
    row["load_w"]               = _f(pw.get("load_w"))
    row["base_load_w"]          = _f(pw.get("base_load_w"))
    row["rf_payload_load_w"]    = _f(pw.get("rf_payload_load_w"))
    row["radiation_fault_load_w"] = _f(pw.get("radiation_fault_load_w"))
    row["power_margin_w"]       = _f(pw.get("power_margin_w"))
    row["battery_charging_w"]   = _f(pw.get("battery_charging_w"))
    row["battery_discharging_w"] = _f(pw.get("battery_discharging_w"))
    row["power_risk_level"]     = _s(pw.get("power_risk_level"))

    # ── thermal profile ──────────────────────────────────────
    row["temp_bus_c"]              = _f(th.get("bus_temperature_c"))
    row["temp_battery_c"]          = _f(th.get("battery_temperature_c"))
    row["temp_processor_c"]        = _f(th.get("processor_temperature_c"))
    row["temp_power_amp_c"]        = _f(th.get("power_amp_temperature_c"))
    row["temp_max_component_c"]    = _f(th.get("max_component_temperature_c"))
    row["thermal_risk_level"]      = _s(th.get("thermal_risk_level"))
    row["thermal_fault_state"]     = _s(th.get("thermal_fault_state"))

    # ── communication link ───────────────────────────────────
    row["link_available"]              = _b(cl.get("link_available"))
    row["link_snr_db"]                 = _f(cl.get("link_snr_db"))
    row["packet_loss_probability"]     = _f(cl.get("packet_loss_probability"))
    row["link_latency_ms_simulated"]   = _f(cl.get("latency_ms"))
    row["rf_blackout_probability"]     = _f(cl.get("rf_blackout_probability"))
    row["antenna_pointing_factor"]     = _f(cl.get("antenna_pointing_factor"))
    row["communication_risk_level"]    = _s(cl.get("communication_risk_level"))

    # ── radiation ────────────────────────────────────────────
    row["radiation_region"]            = _s(rad.get("radiation_region"))
    row["radiation_dose_rate_msv_day"] = _f(rad.get("dose_rate_msv_per_day"))
    row["radiation_baseline_msv_day"]  = _f(rad.get("baseline_dose_rate_msv_per_day"))
    row["radiation_storm_factor"]      = _f(rad.get("solar_storm_exposure_factor"))
    row["radiation_shielding_factor"]  = _f(rad.get("shielding_factor"))
    row["radiation_seu_rate_per_day"]  = _f(rad.get("estimated_single_event_upset_rate_per_day"))
    row["radiation_panel_degradation_per_day"] = _f(rad.get("solar_panel_degradation_fraction_per_day"))
    row["radiation_comms_blackout_prob"] = _f(rad.get("communications_blackout_probability"))
    row["radiation_risk_level"]        = _s(rad.get("radiation_risk_level"))
    row["particle_flux_pfu"]           = _f(rad.get("particle_flux_pfu"))

    flags = rad.get("flags", {}) or {}
    row["solar_storm_active"]          = _b(flags.get("solar_storm_active"))
    row["sunlit_side"]                 = _b(flags.get("sunlit_side"))
    row["earth_shadow_shielded"]       = _b(flags.get("earth_shadow_shielded"))
    row["high_dose_rate"]              = _b(flags.get("high_dose_rate"))
    row["critical_dose_rate"]          = _b(flags.get("critical_dose_rate"))
    row["high_seu_risk"]               = _b(flags.get("high_single_event_upset_risk"))
    row["critical_seu_risk"]           = _b(flags.get("critical_single_event_upset_risk"))

    # ── sensor fusion ────────────────────────────────────────
    row["fusion_collision_risk_score"]    = _f(sf.get("collision_risk_score"))
    row["fusion_radiation_risk_score"]    = _f(sf.get("radiation_risk_score"))
    row["fusion_thermal_risk_score"]      = _f(sf.get("thermal_risk_score"))
    row["fusion_power_risk_score"]        = _f(sf.get("power_risk_score"))
    row["fusion_communication_risk_score"]= _f(sf.get("communication_risk_score"))
    row["fusion_solar_charging_score"]    = _f(sf.get("solar_charging_score"))
    row["fusion_health_score"]            = _f(sf.get("fused_health_score"))
    row["fusion_overall_risk_score"]      = _f(sf.get("overall_operational_risk_score"))
    row["fusion_confidence"]              = _f(sf.get("confidence"))
    row["fusion_recommended_action"]      = _s(sf.get("recommended_external_action"))

    # ── attitude ─────────────────────────────────────────────
    row["attitude_roll_deg"]      = _f(att.get("roll_deg"))
    row["attitude_pitch_deg"]     = _f(att.get("pitch_deg"))
    row["attitude_yaw_deg"]       = _f(att.get("yaw_deg"))
    row["panel_rotation_deg"]     = _f(att.get("panel_rotation_deg"))
    row["angular_rate_mag_dps"]   = _f(att.get("angular_rate_magnitude_dps"))
    row["attitude_stability"]     = _s(att.get("attitude_stability"))

    # ── environment vectors (sun direction) ──────────────────
    sv = ev.get("sun_vector_from_object_eci", {}) or {}
    row["sun_vec_from_obj_x"] = _f(sv.get("x"))
    row["sun_vec_from_obj_y"] = _f(sv.get("y"))
    row["sun_vec_from_obj_z"] = _f(sv.get("z"))
    row["distance_to_sun_m"]  = _f(ev.get("distance_to_sun_m"))

    # ── flags ────────────────────────────────────────────────
    row["can_maneuver"] = _b(raw.get("can_maneuver"), True)
    row["destroyed"]    = _b(raw.get("destroyed"),    False)

    return row


def _flatten_asteroid(raw: dict, common: dict) -> dict:
    row = dict(common)

    rad = raw.get("radiation", {}) or {}
    row["collision_threat"]            = _b(raw.get("collision_threat"), False)
    row["radiation_region"]            = _s(rad.get("radiation_region"))
    row["radiation_dose_rate_msv_day"] = _f(rad.get("dose_rate_msv_per_day"))
    row["radiation_risk_level"]        = _s(rad.get("radiation_risk_level"))
    row["particle_flux_pfu"]           = _f(rad.get("particle_flux_pfu"))

    return row


def _flatten_debris(raw: dict, common: dict) -> dict:
    row = dict(common)

    rad = raw.get("radiation", {}) or {}
    row["age_frames"]                  = _i(raw.get("age_frames"))
    row["life_frames_remaining"]       = _i(raw.get("life_frames_remaining"))
    row["recent_collision_cooldown_frames"] = _i(raw.get("recent_collision_cooldown_frames"))
    row["radiation_dose_rate_msv_day"] = _f(rad.get("dose_rate_msv_per_day"))
    row["radiation_risk_level"]        = _s(rad.get("radiation_risk_level"))

    return row


# Type → flattener registry
_FLATTENERS = {
    "satellite": _flatten_satellite,
    "asteroid":  _flatten_asteroid,
    "debris":    _flatten_debris,
}


# ──────────────────────────────────────────────────────────────
# PARQUET FLUSH
# ──────────────────────────────────────────────────────────────
def flush_buffer(key: tuple):
    """
    Flush in-memory rows for (object_id, object_type) to a Parquet
    batch file.  Caller must already hold the per-object lock.
    """
    object_id, object_type = key
    buf = buffers.get(key, [])
    if not buf:
        return

    obj_dir = os.path.join(TRAINING_DIR, type_to_dirname(object_type), object_id)
    os.makedirs(obj_dir, exist_ok=True)

    seq      = batch_seq.get(key, 1)
    filename = f"batch_{seq:06d}.parquet"
    filepath = os.path.join(obj_dir, filename)
    tmppath  = filepath + ".tmp"

    try:
        df    = pd.DataFrame(buf)
        table = pa.Table.from_pandas(df, preserve_index=False)
        # Atomic write: tmp then rename
        pq.write_table(table, tmppath, compression=PARQUET_COMPRESSION)
        os.replace(tmppath, filepath)
        size_kb = os.path.getsize(filepath) // 1024
        print(f"[flush] {object_type}/{object_id} → {filename}  "
              f"({len(buf)} rows, {size_kb} KB)")
    except Exception as e:
        print(f"[flush] ERROR writing {filepath}: {e}")
        traceback.print_exc()
        return

    # Clear and advance
    flushed_rows         = buf
    buffers[key]         = []
    batch_seq[key]       = seq + 1
    last_flush_time[key] = time.time()

    _update_object_stats(key, flushed_rows, filename)


def _update_object_stats(key: tuple, flushed_rows: list, batch_filename: str):
    """Update the in-memory stats dict used to rebuild master indexes."""
    object_id, object_type = key
    with index_lock:
        stats = object_stats.setdefault(key, {
            "object_id":          object_id,
            "object_type":        object_type,
            "orbit_class":        None,
            "total_records":      0,
            "total_batches":      0,
            "oldest_record_utc":  None,
            "newest_record_utc":  None,
            "batch_files":        [],

            # link timing stats — honestly named
            "avg_uplink_trip_ms":          None,
            "min_uplink_trip_ms":          None,
            "max_uplink_trip_ms":          None,
            "uplink_trip_variance_ms":     None,
            "avg_estimated_link_latency_ms": None,

            # link path distribution
            "direct_ground_pct":  None,
            "via_tdrs_pct":       None,
            "blackout_recovery_pct": None,
            "most_used_ground_station": None,
            "most_used_relay":    None,

            # health (satellite-only — None for asteroids/debris)
            "avg_battery_pct":    None,
            "avg_radiation_msv_day": None,
            "avg_link_quality":   None,
            "avg_fusion_health":  None,
            "avg_fusion_overall_risk": None,
            "collision_events":   0,

            # schema descriptor
            "available_columns":  [],
            "column_completeness": {},
        })

        if flushed_rows:
            stats["orbit_class"] = stats["orbit_class"] or flushed_rows[0].get("orbit_class")

        stats["total_records"] += len(flushed_rows)
        stats["total_batches"] += 1

        rel_path = os.path.join(type_to_dirname(object_type), object_id, batch_filename)
        if rel_path not in stats["batch_files"]:
            stats["batch_files"].append(rel_path)

        # Time bounds
        utc_vals = [r["received_utc"] for r in flushed_rows if r.get("received_utc")]
        if utc_vals:
            utc_sorted = sorted(utc_vals)
            stats["oldest_record_utc"] = (
                utc_sorted[0] if stats["oldest_record_utc"] is None
                else min(stats["oldest_record_utc"], utc_sorted[0]))
            stats["newest_record_utc"] = (
                utc_sorted[-1] if stats["newest_record_utc"] is None
                else max(stats["newest_record_utc"], utc_sorted[-1]))

        # Uplink trip stats
        ut = [r["current_uplink_trip_ms"] for r in flushed_rows
              if r.get("current_uplink_trip_ms") is not None]
        if ut:
            avg = sum(ut) / len(ut)
            mn, mx = min(ut), max(ut)
            var = sum((v - avg) ** 2 for v in ut) / len(ut)
            stats["avg_uplink_trip_ms"]      = round(avg, 4)
            stats["min_uplink_trip_ms"]      = round(mn, 4)
            stats["max_uplink_trip_ms"]      = round(mx, 4)
            stats["uplink_trip_variance_ms"] = round(var, 4)

        ell = [r["estimated_link_latency_ms"] for r in flushed_rows
               if r.get("estimated_link_latency_ms") is not None]
        if ell:
            stats["avg_estimated_link_latency_ms"] = round(sum(ell)/len(ell), 4)

        # Link path distribution
        link_types = [r.get("comm_link_type") for r in flushed_rows]
        n = len(link_types)
        if n > 0:
            direct = sum(1 for l in link_types if l == "direct")
            tdrs   = sum(1 for l in link_types if l == "tdrs_relay")
            stats["direct_ground_pct"] = round(100.0 * direct / n, 2)
            stats["via_tdrs_pct"]      = round(100.0 * tdrs   / n, 2)

        recovery = sum(1 for r in flushed_rows if r.get("was_blackout_recovery"))
        if n > 0:
            stats["blackout_recovery_pct"] = round(100.0 * recovery / n, 2)

        gs_counts: dict[str, int] = {}
        for r in flushed_rows:
            gs = r.get("comm_ground_station")
            if gs:
                gs_counts[gs] = gs_counts.get(gs, 0) + 1
        if gs_counts:
            stats["most_used_ground_station"] = max(gs_counts, key=gs_counts.get)

        rl_counts: dict[str, int] = {}
        for r in flushed_rows:
            rl = r.get("comm_tdrs_relay")
            if rl:
                rl_counts[rl] = rl_counts.get(rl, 0) + 1
        if rl_counts:
            stats["most_used_relay"] = max(rl_counts, key=rl_counts.get)

        # Health (satellite only)
        if object_type == "satellite":
            batt = [r["battery_percent"] for r in flushed_rows
                    if r.get("battery_percent") is not None]
            if batt:
                stats["avg_battery_pct"] = round(sum(batt)/len(batt), 2)

            rad = [r["radiation_dose_rate_msv_day"] for r in flushed_rows
                   if r.get("radiation_dose_rate_msv_day") is not None]
            if rad:
                stats["avg_radiation_msv_day"] = round(sum(rad)/len(rad), 4)

            lq = [r["link_snr_db"] for r in flushed_rows
                  if r.get("link_snr_db") is not None]
            if lq:
                stats["avg_link_quality"] = round(sum(lq)/len(lq), 4)

            fh = [r["fusion_health_score"] for r in flushed_rows
                  if r.get("fusion_health_score") is not None]
            if fh:
                stats["avg_fusion_health"] = round(sum(fh)/len(fh), 4)

            fr = [r["fusion_overall_risk_score"] for r in flushed_rows
                  if r.get("fusion_overall_risk_score") is not None]
            if fr:
                stats["avg_fusion_overall_risk"] = round(sum(fr)/len(fr), 4)

        # Asteroid collision events
        if object_type == "asteroid":
            stats["collision_events"] += sum(
                1 for r in flushed_rows if r.get("collision_threat"))

        # Schema descriptor
        if flushed_rows:
            cols = list(flushed_rows[0].keys())
            stats["available_columns"] = cols
            n_rows = len(flushed_rows)
            completeness = {}
            for c in cols:
                non_null = sum(1 for r in flushed_rows if r.get(c) is not None)
                completeness[c] = round(non_null / n_rows, 4)
            stats["column_completeness"] = completeness


# ──────────────────────────────────────────────────────────────
# MASTER INDEX REBUILD
# ──────────────────────────────────────────────────────────────
def rebuild_master_index():
    """
    Rebuild a per-type master_index.parquet plus a top-level summary.parquet.
    Called lazily after every flush.
    """
    with index_lock:
        if not object_stats:
            return

        # Group by type
        per_type: dict[str, list[dict]] = {t: [] for t in KNOWN_TYPES}
        for stats in object_stats.values():
            t = stats.get("object_type", "unknown")
            if t not in per_type:
                per_type[t] = []
            row = dict(stats)
            row["batch_files"]         = json.dumps(row.get("batch_files", []))
            row["available_columns"]   = json.dumps(row.get("available_columns", []))
            row["column_completeness"] = json.dumps(row.get("column_completeness", {}))
            row["last_updated_utc"]    = datetime.now(timezone.utc).isoformat()
            per_type[t].append(row)

        # Per-type master index
        for t, rows in per_type.items():
            if not rows: continue
            try:
                df = pd.DataFrame(rows)
                table = pa.Table.from_pandas(df, preserve_index=False)
                idx_path = os.path.join(TRAINING_DIR, type_to_dirname(t),
                                         "master_index.parquet")
                tmp = idx_path + ".tmp"
                pq.write_table(table, tmp, compression=PARQUET_COMPRESSION)
                os.replace(tmp, idx_path)
            except Exception as e:
                print(f"[index] error writing {t} master index: {e}")
                traceback.print_exc()

        # Top-level summary
        summary_rows = []
        for t, rows in per_type.items():
            if not rows: continue
            total_records = sum(r.get("total_records", 0) or 0 for r in rows)
            total_batches = sum(r.get("total_batches", 0) or 0 for r in rows)
            avg_uplinks = [r.get("avg_uplink_trip_ms") for r in rows
                           if r.get("avg_uplink_trip_ms") is not None]
            summary_rows.append({
                "object_type":         t,
                "type_dir":            type_to_dirname(t),
                "objects_count":       len(rows),
                "total_records":       total_records,
                "total_batches":       total_batches,
                "avg_uplink_trip_ms":  round(sum(avg_uplinks)/len(avg_uplinks), 4)
                                        if avg_uplinks else None,
                "last_updated_utc":    datetime.now(timezone.utc).isoformat(),
            })
        if summary_rows:
            try:
                sdf = pd.DataFrame(summary_rows)
                stab = pa.Table.from_pandas(sdf, preserve_index=False)
                spath = os.path.join(TRAINING_DIR, "summary.parquet")
                tmp = spath + ".tmp"
                pq.write_table(stab, tmp, compression=PARQUET_COMPRESSION)
                os.replace(tmp, spath)
            except Exception as e:
                print(f"[index] error writing summary: {e}")


# ──────────────────────────────────────────────────────────────
# PING PROCESSOR
# ──────────────────────────────────────────────────────────────
def process_telemetry_file(filepath: str):
    """Read one telemetry JSON, flatten, append, flush if needed."""
    try:
        with open(filepath, "r") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"[processor] cannot parse {filepath}: {e}")
        return

    # ── identity ─────────────────────────────────────────────
    object_id = raw.get("id") or raw.get("object_id") or raw.get("name")
    if not object_id:
        print(f"[processor] no id in {filepath}, skipping")
        return

    object_type = raw.get("type", raw.get("object_type", "satellite"))
    if object_type not in _FLATTENERS:
        print(f"[processor] unknown type '{object_type}' for {object_id}, "
              f"defaulting to satellite")
        object_type = "satellite"

    key = (object_id, object_type)

    # ── timing fields (read-only, never recompute) ──────────
    sms_send_ns          = _i(raw.get("sms_send_time_ns"))
    gsm_recv_ns          = _i(raw.get("gsm_receive_time_ns"))
    sms_to_gsm_ns        = _i(raw.get("sms_to_gsm_trip_ns"))
    last_dn_trip_ns      = _i(raw.get("last_gsm_to_sms_trip_ns"))

    # current_uplink = sms_to_gsm if available, else gsm_recv - sms_send
    if sms_to_gsm_ns is not None:
        current_uplink_trip_ns = sms_to_gsm_ns
    elif sms_send_ns is not None and gsm_recv_ns is not None:
        current_uplink_trip_ns = gsm_recv_ns - sms_send_ns
    else:
        current_uplink_trip_ns = None

    # estimated link latency = uplink + last known downlink
    est_latency_ns = None
    if current_uplink_trip_ns is not None and last_dn_trip_ns is not None:
        est_latency_ns = current_uplink_trip_ns + last_dn_trip_ns

    # last_downlink_age (s): how stale the cached downlink trip is
    # We can't know precisely without an extra timestamp from SMS,
    # so we approximate it as the gap between the most recent
    # GSM->SMS measurement and now.  If SMS doesn't include such a
    # timestamp, we leave it None.
    last_downlink_observed_ns = _i(raw.get("last_gsm_to_sms_observed_at_ns"))
    if last_downlink_observed_ns is not None and gsm_recv_ns is not None:
        last_downlink_age_s = (gsm_recv_ns - last_downlink_observed_ns) / 1e9
    else:
        last_downlink_age_s = None

    received_utc = datetime.now(timezone.utc).isoformat()

    lk = get_object_lock(key)
    with lk:
        if key not in ping_seq:
            ping_seq[key]     = 0
            buffers[key]      = []
            last_flush_time[key] = time.time()
            batch_seq[key]    = 1

        ping_seq[key] += 1
        seq_id = ping_seq[key]

        common = _common_fields(
            raw,
            object_id                = object_id,
            object_type              = object_type,
            sequence_id              = seq_id,
            received_utc             = received_utc,
            current_uplink_trip_ns   = current_uplink_trip_ns,
            last_downlink_trip_ns    = last_dn_trip_ns,
            last_downlink_age_s      = last_downlink_age_s,
            estimated_link_latency_ns= est_latency_ns,
        )

        flatten_fn = _FLATTENERS.get(object_type, _flatten_satellite)
        row = flatten_fn(raw, common)

        buffers[key].append(row)

        # Flush decision
        elapsed = time.time() - last_flush_time.get(key, 0)
        if len(buffers[key]) >= BATCH_SIZE or elapsed >= BATCH_FLUSH_INTERVAL_S:
            flush_buffer(key)
            threading.Thread(target=rebuild_master_index, daemon=True).start()


# ──────────────────────────────────────────────────────────────
# CLEANUP
# ──────────────────────────────────────────────────────────────
def cleanup_old_parquet():
    """Periodically delete Parquet batches older than RETENTION_HOURS."""
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        cutoff = time.time() - RETENTION_HOURS * 3600
        deleted = 0
        try:
            for type_name in KNOWN_TYPES:
                type_dir = Path(TRAINING_DIR) / type_to_dirname(type_name)
                if not type_dir.exists(): continue
                for obj_dir in type_dir.iterdir():
                    if not obj_dir.is_dir(): continue
                    for pf in obj_dir.glob("*.parquet"):
                        try:
                            if pf.stat().st_mtime < cutoff:
                                rel = os.path.join(
                                    type_to_dirname(type_name),
                                    obj_dir.name, pf.name)
                                pf.unlink()
                                deleted += 1
                                key = (obj_dir.name, type_name)
                                with index_lock:
                                    if key in object_stats:
                                        bfl = object_stats[key].get("batch_files", [])
                                        if rel in bfl:
                                            bfl.remove(rel)
                        except FileNotFoundError:
                            pass
        except Exception as e:
            print(f"[cleanup] error: {e}")
        if deleted > 0:
            print(f"[cleanup] deleted {deleted} stale parquet batch(es) "
                  f"(>{RETENTION_HOURS}h old)")
            rebuild_master_index()


def interval_flush_thread():
    """Force-flush slow streams so data hits disk at least every
    BATCH_FLUSH_INTERVAL_S seconds."""
    while True:
        time.sleep(BATCH_FLUSH_INTERVAL_S / 2)
        now = time.time()
        for key in list(buffers.keys()):
            lk = get_object_lock(key)
            with lk:
                if buffers.get(key) and now - last_flush_time.get(key, 0) \
                        >= BATCH_FLUSH_INTERVAL_S:
                    flush_buffer(key)
                    threading.Thread(target=rebuild_master_index,
                                     daemon=True).start()


# ──────────────────────────────────────────────────────────────
# WATCHDOG
# ──────────────────────────────────────────────────────────────
class TelemetryDirHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory: return
        path = event.src_path
        if not path.endswith(".json") or path.endswith(".tmp"): return
        time.sleep(0.05)
        try:    process_telemetry_file(path)
        except Exception as e:
            print(f"[handler] error on {path}: {e}")
            traceback.print_exc()

    def on_moved(self, event):
        if event.is_directory: return
        dest = event.dest_path
        if not dest.endswith(".json"): return
        time.sleep(0.05)
        try:    process_telemetry_file(dest)
        except Exception as e:
            print(f"[handler] error on {dest}: {e}")
            traceback.print_exc()


def process_existing_files():
    """Process any JSONs already present at startup (chronological)."""
    existing = sorted(Path(SAVE_DIR).glob("*.json"))
    if existing:
        print(f"[startup] processing {len(existing)} existing files...")
        for fp in existing:
            try:    process_telemetry_file(str(fp))
            except Exception as e:
                print(f"[startup] error on {fp}: {e}")


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print(" ground_station_processor.py v2  —  AI Training Builder")
    print("=" * 60)
    print(f" Watching:        {SAVE_DIR}/")
    print(f" Training data:   {TRAINING_DIR}/")
    print(f" Per-type dirs:   {[type_to_dirname(t) for t in KNOWN_TYPES]}")
    print(f" Batch size:      {BATCH_SIZE} rows")
    print(f" Flush interval:  {BATCH_FLUSH_INTERVAL_S} s")
    print(f" Retention:       {RETENTION_HOURS} hours")
    print(f" Compression:     {PARQUET_COMPRESSION}")
    print("=" * 60)

    process_existing_files()

    threading.Thread(target=cleanup_old_parquet,   daemon=True).start()
    threading.Thread(target=interval_flush_thread, daemon=True).start()

    handler  = TelemetryDirHandler()
    observer = Observer()
    observer.schedule(handler, SAVE_DIR, recursive=False)
    observer.start()
    print(f"[processor] watchdog active on {SAVE_DIR}/")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[processor] shutting down ...")
        observer.stop()
        print("[processor] flushing buffers ...")
        for key in list(buffers.keys()):
            lk = get_object_lock(key)
            with lk:
                if buffers.get(key):
                    flush_buffer(key)
        rebuild_master_index()
        observer.join()
        print("[processor] clean exit.")


if __name__ == "__main__":
    main()
