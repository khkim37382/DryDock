// ============================================================
// ground_station_processor.cpp  —  v1
// ============================================================
// High-throughput JSON-to-Parquet processor for the GSM side.
//
// Replaces ground_station_processor.py with a multi-threaded C++
// implementation using Apache Arrow C++ for Parquet output.
//
// PIPELINE
// ────────
//
//   received_transmissions/{type}/{object_id}/ping_TIMESTAMP.json
//                             │
//                             ▼ (watcher_thread)
//                       work_queue (mutex+cv)
//                             │
//                             ▼ (ingest_threads × N)
//                  per-object buffers (per-buffer mutex)
//                             │
//                             ▼ (flush_thread, single)
//   training_data/{type}/{object_id}/batch_NNNNNN.parquet
//                             │
//                             ▼ (index_thread)
//   training_data/{type}/master_index.parquet
//   training_data/summary.parquet
//
// Threads (all long-lived, no per-message threads):
//   1× watcher_thread      — polls received_transmissions/, enqueues paths
//   N× ingest_threads      — parse JSON, append rows (N = hw_concurrency / 2)
//   1× flush_thread        — writes Parquet batches via Arrow
//   1× index_thread        — rebuilds master_index after flushes
//   1× json_cleanup_thread — deletes JSONs older than 5 minutes
//   1× parquet_cleanup_thread — deletes Parquet older than 5 hours
//   1× stats_thread        — periodic console heartbeat
//
// MUTEX DESIGN
// ────────────
//   std::shared_mutex objects_map_mutex     — read-mostly, exclusive only on
//                                              insert of new (oid,type) buffer
//   std::mutex   buffer.mtx                 — per-object, fine-grained
//   std::mutex   work_queue_mtx             — work queue
//   std::mutex   flush_queue_mtx            — buffers ready to flush
//   std::mutex   stats_mtx                  — global stats struct
//
// BATCHING
// ────────
//   Each buffer flushes when EITHER:
//     * row_count ≥ BATCH_SIZE         (default 1000)
//     * time since last flush ≥ FLUSH_INTERVAL_S (default 60 s)
//
// macOS BUILD (Apple Silicon and Intel)
// ──────────────────────────────────────
//   brew install apache-arrow nlohmann-json
//
//   ./build_processor.sh
//
//   # Or manually:
//   BREW_PREFIX="$(brew --prefix)"
//   clang++ -std=c++20 -O2 -pthread \
//       -I"${BREW_PREFIX}/include" \
//       -L"${BREW_PREFIX}/lib" \
//       -Wl,-rpath,"${BREW_PREFIX}/lib" \
//       ground_station_processor.cpp \
//       -larrow -lparquet \
//       -o ground_station_processor
//
// Run:  ./ground_station_processor
// ============================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <string_view>
#include <vector>
#include <unordered_map>
#include <unordered_set>
#include <set>
#include <queue>
#include <thread>
#include <mutex>
#include <shared_mutex>
#include <condition_variable>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <algorithm>
#include <stdexcept>
#include <csignal>
#include <memory>
#include <variant>
#include <optional>

#include <nlohmann/json.hpp>

#include <arrow/api.h>
#include <arrow/io/api.h>
#include <parquet/arrow/writer.h>
#include <parquet/properties.h>

namespace fs    = std::filesystem;
using json      = nlohmann::json;
using SteadyClk = std::chrono::steady_clock;
using SystemClk = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static const std::string SAVE_DIR     = "received_transmissions";
static const std::string TRAINING_DIR = "training_data";

// Per-buffer batching thresholds.
static const std::size_t BATCH_SIZE          = 1000;
static const double      FLUSH_INTERVAL_S    = 60.0;

// Parquet retention.
static const double      PARQUET_TTL_S       = 5.0 * 3600.0;  // 5 h
// JSON retention (also handled by GSM, but we cleanup what we ingest).
static const double      JSON_TTL_S          = 5.0 * 60.0;    // 5 min

// Cleanup intervals.
static const int CLEANUP_PARQUET_INTERVAL_S  = 120;
static const int CLEANUP_JSON_INTERVAL_S     = 30;

// Watcher poll interval (ms).
static const int WATCHER_POLL_MS             = 25;

// Stats heartbeat interval (s).
static const int STATS_INTERVAL_S            = 10;

// Number of ingest threads (we cap at 16 for sanity).
static const unsigned INGEST_THREAD_COUNT = std::min(
    16u,
    std::max(2u, std::thread::hardware_concurrency() / 2)
);

// Flush queue cap (back-pressure safety).
static const std::size_t FLUSH_QUEUE_MAX = 1024;

// Maximum size of the work queue (incoming file paths).
static const std::size_t WORK_QUEUE_MAX = 200'000;

// Recognised canonical buckets (controls top-level subdir naming).
// All debris-family sub-types funnel into "debris".
static const std::vector<std::string> KNOWN_TYPES = {
    "satellite", "asteroid", "debris", "unknown"
};

inline std::string type_to_dirname(const std::string& t) {
    if (t == "satellite") return "satellites";
    if (t == "asteroid")  return "asteroids";
    if (t == "debris")    return "debris";
    return "unknown";
}

// Map any incoming type label (including dataset-backed sub-types like
// "generated_debris_fragment") to one of the four canonical buckets.
// This mirrors the dispatcher's canonical_bucket() so routing is
// consistent end-to-end.
inline std::string canonical_bucket(const std::string& t) {
    if (t == "satellite") return "satellite";
    if (t == "asteroid")  return "asteroid";
    if (t == "debris" ||
        t == "generated_debris_fragment" ||
        t == "debris_debris_fragmentation" ||
        t == "satellite_or_object_breakup") {
        return "debris";
    }
    return "unknown";
}

// ──────────────────────────────────────────────────────────────
// FLAT ROW STRUCTURE
// ──────────────────────────────────────────────────────────────
//
// We build a "row" as a vector of typed cells and a parallel vector
// of column names. Different object types have different schemas;
// each per-object buffer locks in its own schema based on the FIRST
// row appended. Subsequent rows must match — if they don't, the
// row is logged and dropped (this only happens if the sim changes
// its JSON shape mid-run, which shouldn't occur).
//
// We use Arrow's schema-on-write design: build vectors of values
// per column, then materialise into Arrow arrays at flush time.
// This is the fastest path for batch writes.
// ──────────────────────────────────────────────────────────────

enum class CellType : uint8_t { I64, F64, BOOL, STR, NIL };

struct Cell {
    CellType    t = CellType::NIL;
    int64_t     i = 0;
    double      f = 0.0;
    bool        b = false;
    std::string s;

    static Cell make_i64 (int64_t v) { Cell c; c.t = CellType::I64;  c.i = v; return c; }
    static Cell make_f64 (double  v) { Cell c; c.t = CellType::F64;  c.f = v; return c; }
    static Cell make_bool(bool    v) { Cell c; c.t = CellType::BOOL; c.b = v; return c; }
    static Cell make_str (std::string v) { Cell c; c.t = CellType::STR; c.s = std::move(v); return c; }
    static Cell make_nil() { return Cell{}; }
};

using Row = std::vector<Cell>;

// ──────────────────────────────────────────────────────────────
// HELPERS: extract typed cells from json with safe defaults
// ──────────────────────────────────────────────────────────────

inline Cell jget_i64(const json& j, const char* key) {
    if (!j.contains(key) || j[key].is_null()) return Cell::make_nil();
    try {
        if (j[key].is_number_integer())  return Cell::make_i64(j[key].get<int64_t>());
        if (j[key].is_number_unsigned()) return Cell::make_i64(static_cast<int64_t>(j[key].get<uint64_t>()));
        if (j[key].is_number_float())    return Cell::make_i64(static_cast<int64_t>(j[key].get<double>()));
        if (j[key].is_boolean())         return Cell::make_i64(j[key].get<bool>() ? 1 : 0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jget_f64(const json& j, const char* key) {
    if (!j.contains(key) || j[key].is_null()) return Cell::make_nil();
    try {
        if (j[key].is_number())  return Cell::make_f64(j[key].get<double>());
        if (j[key].is_boolean()) return Cell::make_f64(j[key].get<bool>() ? 1.0 : 0.0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jget_bool(const json& j, const char* key) {
    if (!j.contains(key) || j[key].is_null()) return Cell::make_nil();
    try {
        if (j[key].is_boolean())         return Cell::make_bool(j[key].get<bool>());
        if (j[key].is_number_integer()) return Cell::make_bool(j[key].get<int64_t>() != 0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jget_str(const json& j, const char* key) {
    if (!j.contains(key) || j[key].is_null()) return Cell::make_nil();
    try {
        if (j[key].is_string()) return Cell::make_str(j[key].get<std::string>());
    } catch (...) {}
    return Cell::make_nil();
}

// Drill into a nested key path: j["a"]["b"] becomes get_nested(j, {"a","b"}).
inline const json* nested(const json& j, std::initializer_list<const char*> path) {
    const json* cur = &j;
    for (auto k : path) {
        if (!cur->is_object() || !cur->contains(k)) return nullptr;
        cur = &(*cur)[k];
        if (cur->is_null()) return nullptr;
    }
    return cur;
}

inline Cell ngetf(const json& j, std::initializer_list<const char*> path) {
    const json* p = nested(j, path);
    if (!p) return Cell::make_nil();
    try {
        if (p->is_number())  return Cell::make_f64(p->get<double>());
        if (p->is_boolean()) return Cell::make_f64(p->get<bool>() ? 1.0 : 0.0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell ngeti(const json& j, std::initializer_list<const char*> path) {
    const json* p = nested(j, path);
    if (!p) return Cell::make_nil();
    try {
        if (p->is_number_integer())  return Cell::make_i64(p->get<int64_t>());
        if (p->is_number_unsigned()) return Cell::make_i64(static_cast<int64_t>(p->get<uint64_t>()));
        if (p->is_number_float())    return Cell::make_i64(static_cast<int64_t>(p->get<double>()));
        if (p->is_boolean())         return Cell::make_i64(p->get<bool>() ? 1 : 0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell ngets(const json& j, std::initializer_list<const char*> path) {
    const json* p = nested(j, path);
    if (!p) return Cell::make_nil();
    try {
        if (p->is_string()) return Cell::make_str(p->get<std::string>());
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell ngetb(const json& j, std::initializer_list<const char*> path) {
    const json* p = nested(j, path);
    if (!p) return Cell::make_nil();
    try {
        if (p->is_boolean())         return Cell::make_bool(p->get<bool>());
        if (p->is_number_integer())  return Cell::make_bool(p->get<int64_t>() != 0);
    } catch (...) {}
    return Cell::make_nil();
}

// ──────────────────────────────────────────────────────────────
// PER-TYPE COLUMN BUILDERS
// ──────────────────────────────────────────────────────────────
//
// Each returns (column_names, row_cells) for the given JSON payload.
// Column lists are HARD-CODED to keep schemas stable per type.
// Adding new fields = add to both vectors at the same index.
// ──────────────────────────────────────────────────────────────

struct BuiltRow {
    std::vector<std::string> columns;
    Row                      row;
};

static const std::vector<std::string> COMMON_COLUMNS = {
    // identity & sequencing
    "sequence_id", "object_id", "object_type", "object_bucket",
    "received_utc",

    // honest link timing
    "current_uplink_trip_ns", "current_uplink_trip_ms",
    "sms_send_time_ns", "gsm_receive_time_ns", "sms_to_gsm_trip_ns",

    // simulation clocks
    "sim_frame", "sim_physical_time_s", "sim_visual_time_s",
    "sim_unix_time_s", "sim_utc_iso",

    // dispatcher comm path
    "comm_link_type", "comm_ground_station", "comm_tdrs_relay",
    "comm_propagation_delay_ms", "comm_distance_m",

    // kinematics
    "pos_x_m", "pos_y_m", "pos_z_m",
    "vel_x_mps", "vel_y_mps", "vel_z_mps",
    "speed_mps",

    // orbital state
    "altitude_m", "altitude_km", "orbit_class", "orbit_description",
    "specific_orbital_energy_j_per_kg", "distance_from_earth_center_m",

    // physical
    "active", "mass_kg", "physical_radius_m", "selected_for_data",
    "central_body",

    // blackout recovery
    "was_blackout_recovery", "blackout_duration_s",

    // routing audit
    "mqtt_topic",

    // ── dataset-backed metadata (drydock v1) ────────────────────
    // Catalog identity
    "name", "catalog_name", "json_object_id", "norad_cat_id",
    "mass_source", "mass_confidence_str",

    // Keplerian orbit elements (most informative for AI features)
    "oe_epoch", "oe_mean_motion_rev_per_day", "oe_eccentricity",
    "oe_inclination_deg", "oe_raan_deg", "oe_argument_of_perigee_deg",
    "oe_mean_anomaly_deg", "oe_bstar", "oe_estimated_semimajor_axis_km",

    // Raw CelesTrak passthrough (kept compact)
    "celestrak_object_name", "celestrak_object_id",
    "celestrak_classification_type", "celestrak_norad_cat_id",

    // UCS metadata (operator/owner intelligence)
    "ucs_country_of_operator_owner", "ucs_operator_owner",
    "ucs_users", "ucs_purpose", "ucs_detailed_purpose",
    "ucs_orbit_class", "ucs_orbit_type",
    "ucs_launch_mass_kg", "ucs_dry_mass_kg", "ucs_power_watts",
    "ucs_launch_date", "ucs_expected_lifetime_years",
    "ucs_contractor", "ucs_launch_site", "ucs_launch_vehicle",
    "ucs_cospar_number"
};

// mass_confidence in the schema is "number|string|null".  Render it as a
// string so we don't have to deal with an Arrow union type.  Numbers come
// out like "0.92"; missing values become NIL.
inline Cell jget_mass_confidence_str(const json& raw)
{
    if (!raw.contains("mass_confidence") || raw["mass_confidence"].is_null())
        return Cell::make_nil();
    try {
        const auto& v = raw["mass_confidence"];
        if (v.is_string()) return Cell::make_str(v.get<std::string>());
        if (v.is_number()) {
            std::ostringstream oss; oss << v.get<double>();
            return Cell::make_str(oss.str());
        }
        if (v.is_boolean()) return Cell::make_str(v.get<bool>() ? "true" : "false");
    } catch (...) {}
    return Cell::make_nil();
}

void append_common(const json& raw,
                    int64_t sequence_id,
                    const std::string& object_id,
                    const std::string& object_type,
                    const std::string& object_bucket,
                    const std::string& received_utc,
                    Row& row)
{
    // current uplink trip = sms_to_gsm_trip_ns if present, else gsm_recv - sms_send
    Cell uplink_ns = jget_i64(raw, "sms_to_gsm_trip_ns");
    if (uplink_ns.t == CellType::NIL) {
        Cell s = jget_i64(raw, "sms_send_time_ns");
        Cell g = jget_i64(raw, "gsm_receive_time_ns");
        if (s.t == CellType::I64 && g.t == CellType::I64) {
            uplink_ns = Cell::make_i64(g.i - s.i);
        }
    }
    Cell uplink_ms = (uplink_ns.t == CellType::I64)
        ? Cell::make_f64(static_cast<double>(uplink_ns.i) / 1.0e6)
        : Cell::make_nil();

    // identity & sequencing
    row.push_back(Cell::make_i64(sequence_id));            // sequence_id
    row.push_back(Cell::make_str(object_id));              // object_id
    row.push_back(Cell::make_str(object_type));            // object_type (sub-type)
    row.push_back(Cell::make_str(object_bucket));          // object_bucket (canonical)
    row.push_back(Cell::make_str(received_utc));           // received_utc

    // honest link timing
    row.push_back(uplink_ns);                              // current_uplink_trip_ns
    row.push_back(uplink_ms);                              // current_uplink_trip_ms

    row.push_back(jget_i64(raw, "sms_send_time_ns"));      // sms_send_time_ns
    row.push_back(jget_i64(raw, "gsm_receive_time_ns"));   // gsm_receive_time_ns
    row.push_back(jget_i64(raw, "sms_to_gsm_trip_ns"));    // sms_to_gsm_trip_ns

    // simulation clocks
    row.push_back(jget_i64(raw, "sim_frame"));             // sim_frame
    row.push_back(jget_f64(raw, "sim_physical_time_s"));   // sim_physical_time_s
    row.push_back(jget_f64(raw, "sim_visual_time_s"));     // sim_visual_time_s
    row.push_back(jget_f64(raw, "sim_unix_time_s"));       // sim_unix_time_s
    row.push_back(jget_str(raw, "sim_utc_iso"));           // sim_utc_iso

    // dispatcher comm path
    row.push_back(ngets(raw, {"dispatcher_comm_path","link_type"}));
    row.push_back(ngets(raw, {"dispatcher_comm_path","ground_station"}));
    row.push_back(ngets(raw, {"dispatcher_comm_path","tdrs_relay"}));
    row.push_back(ngetf(raw, {"dispatcher_comm_path","propagation_delay_ms"}));
    row.push_back(ngetf(raw, {"dispatcher_comm_path","distance_m"}));

    // kinematics
    row.push_back(ngetf(raw, {"position_m_eci","x"}));
    row.push_back(ngetf(raw, {"position_m_eci","y"}));
    row.push_back(ngetf(raw, {"position_m_eci","z"}));
    row.push_back(ngetf(raw, {"velocity_mps_eci","x"}));
    row.push_back(ngetf(raw, {"velocity_mps_eci","y"}));
    row.push_back(ngetf(raw, {"velocity_mps_eci","z"}));
    row.push_back(jget_f64(raw, "speed_mps"));

    // orbital state
    row.push_back(jget_f64(raw, "altitude_m"));
    row.push_back(jget_f64(raw, "altitude_km"));
    row.push_back(jget_str(raw, "orbit_class"));
    row.push_back(jget_str(raw, "orbit_description"));
    row.push_back(jget_f64(raw, "specific_orbital_energy_j_per_kg"));
    row.push_back(jget_f64(raw, "distance_from_earth_center_m"));

    // physical
    row.push_back(jget_bool(raw, "active"));
    row.push_back(jget_f64(raw, "mass_kg"));
    row.push_back(jget_f64(raw, "physical_radius_m"));
    row.push_back(jget_bool(raw, "selected_for_data"));
    row.push_back(jget_str(raw, "central_body"));

    // blackout recovery
    row.push_back(jget_bool(raw, "was_blackout_recovery"));
    row.push_back(jget_f64(raw, "blackout_duration_s"));

    // routing audit
    row.push_back(jget_str(raw, "mqtt_topic"));

    // ── dataset-backed metadata (NIL where absent — fragments
    //    and bare debris won't have most of these) ───────────────

    // Catalog identity
    row.push_back(jget_str(raw, "name"));
    row.push_back(jget_str(raw, "catalog_name"));
    row.push_back(jget_str(raw, "json_object_id"));
    row.push_back(jget_i64(raw, "norad_cat_id"));
    row.push_back(jget_str(raw, "mass_source"));
    row.push_back(jget_mass_confidence_str(raw));

    // Keplerian orbit elements
    row.push_back(ngets(raw, {"orbit_elements","epoch"}));
    row.push_back(ngetf(raw, {"orbit_elements","mean_motion_rev_per_day"}));
    row.push_back(ngetf(raw, {"orbit_elements","eccentricity"}));
    row.push_back(ngetf(raw, {"orbit_elements","inclination_deg"}));
    row.push_back(ngetf(raw, {"orbit_elements","raan_deg"}));
    row.push_back(ngetf(raw, {"orbit_elements","argument_of_perigee_deg"}));
    row.push_back(ngetf(raw, {"orbit_elements","mean_anomaly_deg"}));
    row.push_back(ngetf(raw, {"orbit_elements","bstar"}));
    row.push_back(ngetf(raw, {"orbit_elements","estimated_semimajor_axis_km"}));

    // Raw CelesTrak fields (compact subset)
    row.push_back(ngets(raw, {"raw_celestrak_gp","OBJECT_NAME"}));
    row.push_back(ngets(raw, {"raw_celestrak_gp","OBJECT_ID"}));
    row.push_back(ngets(raw, {"raw_celestrak_gp","CLASSIFICATION_TYPE"}));
    row.push_back(ngeti(raw, {"raw_celestrak_gp","NORAD_CAT_ID"}));

    // UCS metadata
    row.push_back(ngets(raw, {"ucs_metadata","country_of_operator_owner"}));
    row.push_back(ngets(raw, {"ucs_metadata","operator_owner"}));
    row.push_back(ngets(raw, {"ucs_metadata","users"}));
    row.push_back(ngets(raw, {"ucs_metadata","purpose"}));
    row.push_back(ngets(raw, {"ucs_metadata","detailed_purpose"}));
    row.push_back(ngets(raw, {"ucs_metadata","orbit_class"}));
    row.push_back(ngets(raw, {"ucs_metadata","orbit_type"}));
    row.push_back(ngetf(raw, {"ucs_metadata","launch_mass_kg"}));
    row.push_back(ngetf(raw, {"ucs_metadata","dry_mass_kg"}));
    row.push_back(ngetf(raw, {"ucs_metadata","power_watts"}));
    row.push_back(ngets(raw, {"ucs_metadata","launch_date"}));
    row.push_back(ngetf(raw, {"ucs_metadata","expected_lifetime_years"}));
    row.push_back(ngets(raw, {"ucs_metadata","contractor"}));
    row.push_back(ngets(raw, {"ucs_metadata","launch_site"}));
    row.push_back(ngets(raw, {"ucs_metadata","launch_vehicle"}));
    row.push_back(ngets(raw, {"ucs_metadata","cospar_number"}));
}

// ── Satellite-specific extension columns ────────────────────────
static const std::vector<std::string> SAT_COLUMNS = {
    "in_sunlight", "in_eclipse", "sun_exposure_factor", "eclipse_body",
    "panel_efficiency_factor", "panel_incidence_deg", "solar_irradiance_w_m2",
    "solar_panel_area_m2", "panel_efficiency", "est_solar_generation_w",
    "max_solar_generation_w", "recommended_panel_rotation_deg",
    "solar_optimization_state", "charging_priority",
    "solar_panel_voltage_v", "solar_panel_current_a", "battery_voltage_v",
    "bus_voltage_v", "bus_current_draw_a", "net_charge_current_a",
    "battery_percent", "battery_capacity_wh", "solar_generation_w",
    "load_w", "base_load_w", "rf_payload_load_w", "radiation_fault_load_w",
    "power_margin_w", "battery_charging_w", "battery_discharging_w",
    "power_risk_level",
    "temp_bus_c", "temp_battery_c", "temp_processor_c", "temp_power_amp_c",
    "temp_max_component_c", "thermal_risk_level", "thermal_fault_state",
    "link_available", "link_snr_db", "packet_loss_probability",
    "link_latency_ms_simulated", "rf_blackout_probability",
    "antenna_pointing_factor", "communication_risk_level",
    "radiation_region", "radiation_dose_rate_msv_day",
    "radiation_baseline_msv_day", "radiation_storm_factor",
    "radiation_shielding_factor", "radiation_seu_rate_per_day",
    "radiation_panel_degradation_per_day", "radiation_comms_blackout_prob",
    "radiation_risk_level", "particle_flux_pfu",
    "solar_storm_active", "sunlit_side", "earth_shadow_shielded",
    "high_dose_rate", "critical_dose_rate", "high_seu_risk", "critical_seu_risk",
    "fusion_collision_risk_score", "fusion_radiation_risk_score",
    "fusion_thermal_risk_score", "fusion_power_risk_score",
    "fusion_communication_risk_score", "fusion_solar_charging_score",
    "fusion_health_score", "fusion_overall_risk_score", "fusion_confidence",
    "fusion_recommended_action",
    "attitude_roll_deg", "attitude_pitch_deg", "attitude_yaw_deg",
    "panel_rotation_deg", "angular_rate_mag_dps", "attitude_stability",
    "sun_vec_from_obj_x", "sun_vec_from_obj_y", "sun_vec_from_obj_z",
    "distance_to_sun_m",
    "can_maneuver", "destroyed"
};

void append_satellite_extension(const json& raw, Row& row)
{
    row.push_back(ngetb(raw, {"sunlight_state","in_sunlight"}));
    row.push_back(ngetb(raw, {"sunlight_state","in_eclipse"}));
    row.push_back(ngetf(raw, {"sunlight_state","sun_exposure_factor"}));
    row.push_back(ngets(raw, {"sunlight_state","eclipse_body"}));

    row.push_back(ngetf(raw, {"solar_panel_system","panel_efficiency_factor"}));
    row.push_back(ngetf(raw, {"solar_panel_system","sun_incidence_angle_deg"}));
    row.push_back(ngetf(raw, {"solar_panel_system","solar_irradiance_w_m2"}));
    row.push_back(ngetf(raw, {"solar_panel_system","panel_area_m2"}));
    row.push_back(ngetf(raw, {"solar_panel_system","panel_efficiency"}));
    row.push_back(ngetf(raw, {"solar_panel_system","estimated_solar_generation_w"}));
    row.push_back(ngetf(raw, {"solar_panel_system","max_solar_generation_w"}));
    row.push_back(ngetf(raw, {"solar_panel_system","recommended_panel_rotation_deg"}));
    row.push_back(ngets(raw, {"solar_panel_system","solar_optimization_state"}));
    row.push_back(ngets(raw, {"solar_panel_system","charging_priority"}));

    row.push_back(ngetf(raw, {"voltage_sensors","solar_panel_voltage_v"}));
    row.push_back(ngetf(raw, {"voltage_sensors","solar_panel_current_a"}));
    row.push_back(ngetf(raw, {"voltage_sensors","battery_voltage_v"}));
    row.push_back(ngetf(raw, {"voltage_sensors","bus_voltage_v"}));
    row.push_back(ngetf(raw, {"voltage_sensors","bus_current_draw_a"}));
    row.push_back(ngetf(raw, {"voltage_sensors","net_charge_current_a"}));

    row.push_back(ngetf(raw, {"power_system","battery_percent"}));
    row.push_back(ngetf(raw, {"power_system","battery_capacity_wh"}));
    row.push_back(ngetf(raw, {"power_system","solar_generation_w"}));
    row.push_back(ngetf(raw, {"power_system","load_w"}));
    row.push_back(ngetf(raw, {"power_system","base_load_w"}));
    row.push_back(ngetf(raw, {"power_system","rf_payload_load_w"}));
    row.push_back(ngetf(raw, {"power_system","radiation_fault_load_w"}));
    row.push_back(ngetf(raw, {"power_system","power_margin_w"}));
    row.push_back(ngetf(raw, {"power_system","battery_charging_w"}));
    row.push_back(ngetf(raw, {"power_system","battery_discharging_w"}));
    row.push_back(ngets(raw, {"power_system","power_risk_level"}));

    row.push_back(ngetf(raw, {"thermal_profile","bus_temperature_c"}));
    row.push_back(ngetf(raw, {"thermal_profile","battery_temperature_c"}));
    row.push_back(ngetf(raw, {"thermal_profile","processor_temperature_c"}));
    row.push_back(ngetf(raw, {"thermal_profile","power_amp_temperature_c"}));
    row.push_back(ngetf(raw, {"thermal_profile","max_component_temperature_c"}));
    row.push_back(ngets(raw, {"thermal_profile","thermal_risk_level"}));
    row.push_back(ngets(raw, {"thermal_profile","thermal_fault_state"}));

    row.push_back(ngetb(raw, {"communication_link","link_available"}));
    row.push_back(ngetf(raw, {"communication_link","link_snr_db"}));
    row.push_back(ngetf(raw, {"communication_link","packet_loss_probability"}));
    row.push_back(ngetf(raw, {"communication_link","latency_ms"}));
    row.push_back(ngetf(raw, {"communication_link","rf_blackout_probability"}));
    row.push_back(ngetf(raw, {"communication_link","antenna_pointing_factor"}));
    row.push_back(ngets(raw, {"communication_link","communication_risk_level"}));

    row.push_back(ngets(raw, {"radiation","radiation_region"}));
    row.push_back(ngetf(raw, {"radiation","dose_rate_msv_per_day"}));
    row.push_back(ngetf(raw, {"radiation","baseline_dose_rate_msv_per_day"}));
    row.push_back(ngetf(raw, {"radiation","solar_storm_exposure_factor"}));
    row.push_back(ngetf(raw, {"radiation","shielding_factor"}));
    row.push_back(ngetf(raw, {"radiation","estimated_single_event_upset_rate_per_day"}));
    row.push_back(ngetf(raw, {"radiation","solar_panel_degradation_fraction_per_day"}));
    row.push_back(ngetf(raw, {"radiation","communications_blackout_probability"}));
    row.push_back(ngets(raw, {"radiation","radiation_risk_level"}));
    row.push_back(ngetf(raw, {"radiation","particle_flux_pfu"}));

    row.push_back(ngetb(raw, {"radiation","flags","solar_storm_active"}));
    row.push_back(ngetb(raw, {"radiation","flags","sunlit_side"}));
    row.push_back(ngetb(raw, {"radiation","flags","earth_shadow_shielded"}));
    row.push_back(ngetb(raw, {"radiation","flags","high_dose_rate"}));
    row.push_back(ngetb(raw, {"radiation","flags","critical_dose_rate"}));
    row.push_back(ngetb(raw, {"radiation","flags","high_single_event_upset_risk"}));
    row.push_back(ngetb(raw, {"radiation","flags","critical_single_event_upset_risk"}));

    row.push_back(ngetf(raw, {"sensor_fusion_state","collision_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","radiation_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","thermal_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","power_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","communication_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","solar_charging_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","fused_health_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","overall_operational_risk_score"}));
    row.push_back(ngetf(raw, {"sensor_fusion_state","confidence"}));
    row.push_back(ngets(raw, {"sensor_fusion_state","recommended_external_action"}));

    row.push_back(ngetf(raw, {"attitude_state","roll_deg"}));
    row.push_back(ngetf(raw, {"attitude_state","pitch_deg"}));
    row.push_back(ngetf(raw, {"attitude_state","yaw_deg"}));
    row.push_back(ngetf(raw, {"attitude_state","panel_rotation_deg"}));
    row.push_back(ngetf(raw, {"attitude_state","angular_rate_magnitude_dps"}));
    row.push_back(ngets(raw, {"attitude_state","attitude_stability"}));

    row.push_back(ngetf(raw, {"environment_vectors","sun_vector_from_object_eci","x"}));
    row.push_back(ngetf(raw, {"environment_vectors","sun_vector_from_object_eci","y"}));
    row.push_back(ngetf(raw, {"environment_vectors","sun_vector_from_object_eci","z"}));
    row.push_back(ngetf(raw, {"environment_vectors","distance_to_sun_m"}));

    row.push_back(jget_bool(raw, "can_maneuver"));
    row.push_back(jget_bool(raw, "destroyed"));
}

// ── Asteroid-specific extension columns ─────────────────────────
static const std::vector<std::string> AST_COLUMNS = {
    "collision_threat",
    "radiation_region", "radiation_dose_rate_msv_day",
    "radiation_risk_level", "particle_flux_pfu"
};

void append_asteroid_extension(const json& raw, Row& row)
{
    row.push_back(jget_bool(raw, "collision_threat"));
    row.push_back(ngets(raw, {"radiation","radiation_region"}));
    row.push_back(ngetf(raw, {"radiation","dose_rate_msv_per_day"}));
    row.push_back(ngets(raw, {"radiation","radiation_risk_level"}));
    row.push_back(ngetf(raw, {"radiation","particle_flux_pfu"}));
}

// ── Debris-specific extension columns ───────────────────────────
static const std::vector<std::string> DEB_COLUMNS = {
    "age_frames", "life_frames_remaining", "recent_collision_cooldown_frames",
    "radiation_dose_rate_msv_day", "radiation_risk_level"
};

void append_debris_extension(const json& raw, Row& row)
{
    row.push_back(jget_i64(raw, "age_frames"));
    row.push_back(jget_i64(raw, "life_frames_remaining"));
    row.push_back(jget_i64(raw, "recent_collision_cooldown_frames"));
    row.push_back(ngetf(raw, {"radiation","dose_rate_msv_per_day"}));
    row.push_back(ngets(raw, {"radiation","radiation_risk_level"}));
}

// ── Build full row given a parsed payload ───────────────────────
BuiltRow build_row(const json& raw,
                    int64_t sequence_id,
                    const std::string& object_id,
                    const std::string& object_type,    // raw sub-type (e.g.
                                                       // "generated_debris_fragment")
                    const std::string& object_bucket,  // canonical bucket
                                                       // ("satellite"/"asteroid"/"debris")
                    const std::string& received_utc)
{
    BuiltRow b;
    b.columns = COMMON_COLUMNS;
    b.row.reserve(b.columns.size() + 96);
    append_common(raw, sequence_id, object_id, object_type,
                   object_bucket, received_utc, b.row);

    // Per-bucket extension uses the CANONICAL bucket so all debris-family
    // sub-types (debris, generated_debris_fragment,
    // debris_debris_fragmentation, satellite_or_object_breakup) share the
    // same compact debris schema.
    if (object_bucket == "satellite") {
        b.columns.insert(b.columns.end(), SAT_COLUMNS.begin(), SAT_COLUMNS.end());
        append_satellite_extension(raw, b.row);
    } else if (object_bucket == "asteroid") {
        b.columns.insert(b.columns.end(), AST_COLUMNS.begin(), AST_COLUMNS.end());
        append_asteroid_extension(raw, b.row);
    } else if (object_bucket == "debris") {
        b.columns.insert(b.columns.end(), DEB_COLUMNS.begin(), DEB_COLUMNS.end());
        append_debris_extension(raw, b.row);
    }
    return b;
}

// ──────────────────────────────────────────────────────────────
// PER-OBJECT BUFFER
// ──────────────────────────────────────────────────────────────

struct ObjectBuffer {
    std::mutex                mtx;
    std::vector<std::string>  columns;        // schema, set on first row
    std::vector<Row>          rows;           // rows queued for flush
    SteadyClk::time_point     last_flush     = SteadyClk::now();
    uint64_t                  batch_seq      = 1;
    uint64_t                  total_rows     = 0;
    std::string               object_type;    // raw sub-type (e.g.
                                              // "generated_debris_fragment")
    std::string               object_bucket;  // canonical: "satellite" /
                                              // "asteroid" / "debris" / "unknown"
    std::string               object_id;
    int64_t                   sequence_counter = 0;

    // Cached column indices (resolved once on first row, then reused).
    // -1 means "not present".
    int idx_uplink_ms       = -1;
    int idx_link_type       = -1;
    int idx_ground_station  = -1;
    int idx_tdrs_relay      = -1;
    int idx_blackout_recov  = -1;

    // Stats kept up-to-date for the master index.
    std::string oldest_record_utc;
    std::string newest_record_utc;
    double avg_uplink_trip_ms_sum = 0.0;
    int64_t avg_uplink_trip_ms_n  = 0;
    double min_uplink_trip_ms     = std::numeric_limits<double>::infinity();
    double max_uplink_trip_ms     = -std::numeric_limits<double>::infinity();
    int64_t direct_count          = 0;
    int64_t tdrs_count            = 0;
    int64_t blackout_recovery_count = 0;
    std::unordered_map<std::string,int64_t> ground_station_counts;
    std::unordered_map<std::string,int64_t> tdrs_relay_counts;
    int64_t total_link_obs        = 0;
};

// Resolve column indices on a freshly-populated ObjectBuffer. Called
// once with the lock held immediately after columns is filled.
inline int find_col(const std::vector<std::string>& cols, const char* name)
{
    for (std::size_t i = 0; i < cols.size(); ++i)
        if (cols[i] == name) return static_cast<int>(i);
    return -1;
}
inline void resolve_buffer_indices(ObjectBuffer& buf)
{
    buf.idx_uplink_ms      = find_col(buf.columns, "current_uplink_trip_ms");
    buf.idx_link_type      = find_col(buf.columns, "comm_link_type");
    buf.idx_ground_station = find_col(buf.columns, "comm_ground_station");
    buf.idx_tdrs_relay     = find_col(buf.columns, "comm_tdrs_relay");
    buf.idx_blackout_recov = find_col(buf.columns, "was_blackout_recovery");
}

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────

struct ObjectKey {
    std::string oid;
    std::string type;
    bool operator==(const ObjectKey& o) const noexcept {
        return oid == o.oid && type == o.type;
    }
};
struct ObjectKeyHash {
    std::size_t operator()(const ObjectKey& k) const noexcept {
        return std::hash<std::string>{}(k.oid) ^ (std::hash<std::string>{}(k.type) << 1);
    }
};

std::unordered_map<ObjectKey, std::unique_ptr<ObjectBuffer>, ObjectKeyHash> g_objects;
std::shared_mutex g_objects_mutex;

// Work queue: file paths waiting to be ingested.
std::queue<std::string> g_work_queue;
std::mutex              g_work_queue_mtx;
std::condition_variable g_work_queue_cv;

// Flush queue: ObjectKey of buffers ready to flush.
std::queue<ObjectKey>   g_flush_queue;
std::mutex              g_flush_queue_mtx;
std::condition_variable g_flush_queue_cv;
std::unordered_set<std::string> g_flush_inflight;   // oid-type keys currently flushing
std::mutex                      g_flush_inflight_mtx;

// Index dirty flag.
std::atomic<bool> g_index_dirty{false};
std::condition_variable g_index_cv;
std::mutex g_index_mtx;

// Shutdown.
std::atomic<bool> g_running{true};

// Stats.
std::atomic<uint64_t> g_stat_files_seen{0};
std::atomic<uint64_t> g_stat_files_parsed{0};
std::atomic<uint64_t> g_stat_files_failed{0};
std::atomic<uint64_t> g_stat_rows_appended{0};
std::atomic<uint64_t> g_stat_batches_written{0};
std::atomic<uint64_t> g_stat_parquet_errors{0};

// ──────────────────────────────────────────────────────────────
// UTILITIES
// ──────────────────────────────────────────────────────────────

std::string utc_now_iso()
{
    using namespace std::chrono;
    auto now = system_clock::now();
    auto t   = system_clock::to_time_t(now);
    auto us  = duration_cast<microseconds>(now.time_since_epoch()) % 1'000'000;
    std::tm gmt;
#if defined(_WIN32)
    gmtime_s(&gmt, &t);
#else
    gmtime_r(&t, &gmt);
#endif
    char buf[64];
    std::snprintf(buf, sizeof(buf),
        "%04d-%02d-%02dT%02d:%02d:%02d.%06lldZ",
        gmt.tm_year + 1900, gmt.tm_mon + 1, gmt.tm_mday,
        gmt.tm_hour, gmt.tm_min, gmt.tm_sec,
        static_cast<long long>(us.count()));
    return buf;
}

double now_seconds()
{
    return std::chrono::duration<double>(
        SystemClk::now().time_since_epoch()).count();
}

double file_mtime_seconds(const fs::path& p)
{
    using namespace std::chrono;
    auto ftime = fs::last_write_time(p);
    auto sctp  = time_point_cast<seconds>(fs::file_clock::to_sys(ftime));
    return static_cast<double>(sctp.time_since_epoch().count());
}

// ──────────────────────────────────────────────────────────────
// ARROW: build a Table from a buffer's row set
// ──────────────────────────────────────────────────────────────
//
// Strategy: scan all rows for each column, determine the "best" type
// (BOOL/I64/F64/STR), build a typed Arrow array. NIL values become
// Arrow nulls.
//
// In practice each column has a stable type because we put each value
// through a typed jget_X helper, so we can inspect the FIRST non-null
// cell to pick the column type. If a column is entirely null we fall
// back to STR (any subsequent partial fill remains valid).
// ──────────────────────────────────────────────────────────────

std::shared_ptr<arrow::Array> build_array_for_column(
    const std::vector<Row>& rows, std::size_t col_idx)
{
    arrow::MemoryPool* pool = arrow::default_memory_pool();

    // Find first non-null type.
    CellType ctype = CellType::NIL;
    for (const auto& r : rows) {
        if (col_idx < r.size() && r[col_idx].t != CellType::NIL) {
            ctype = r[col_idx].t;
            break;
        }
    }
    if (ctype == CellType::NIL) ctype = CellType::STR;   // empty col → string nulls

    if (ctype == CellType::I64) {
        arrow::Int64Builder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::I64) {
                (void)b.Append(r[col_idx].i);
            } else if (col_idx < r.size() && r[col_idx].t == CellType::F64) {
                // mixed int/float — promote to float column
                // Re-do as F64 builder
                arrow::DoubleBuilder b2(pool);
                for (const auto& r2 : rows) {
                    if (col_idx >= r2.size() || r2[col_idx].t == CellType::NIL) {
                        (void)b2.AppendNull();
                    } else if (r2[col_idx].t == CellType::I64) {
                        (void)b2.Append(static_cast<double>(r2[col_idx].i));
                    } else if (r2[col_idx].t == CellType::F64) {
                        (void)b2.Append(r2[col_idx].f);
                    } else {
                        (void)b2.AppendNull();
                    }
                }
                std::shared_ptr<arrow::Array> arr;
                (void)b2.Finish(&arr);
                return arr;
            } else {
                (void)b.AppendNull();
            }
        }
        std::shared_ptr<arrow::Array> arr;
        (void)b.Finish(&arr);
        return arr;
    }
    if (ctype == CellType::F64) {
        arrow::DoubleBuilder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::F64) {
                (void)b.Append(r[col_idx].f);
            } else if (col_idx < r.size() && r[col_idx].t == CellType::I64) {
                (void)b.Append(static_cast<double>(r[col_idx].i));
            } else {
                (void)b.AppendNull();
            }
        }
        std::shared_ptr<arrow::Array> arr;
        (void)b.Finish(&arr);
        return arr;
    }
    if (ctype == CellType::BOOL) {
        arrow::BooleanBuilder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::BOOL) {
                (void)b.Append(r[col_idx].b);
            } else if (col_idx < r.size() && r[col_idx].t == CellType::I64) {
                (void)b.Append(r[col_idx].i != 0);
            } else {
                (void)b.AppendNull();
            }
        }
        std::shared_ptr<arrow::Array> arr;
        (void)b.Finish(&arr);
        return arr;
    }
    // STR
    arrow::StringBuilder b(pool);
    for (const auto& r : rows) {
        if (col_idx < r.size() && r[col_idx].t == CellType::STR) {
            (void)b.Append(r[col_idx].s);
        } else {
            (void)b.AppendNull();
        }
    }
    std::shared_ptr<arrow::Array> arr;
    (void)b.Finish(&arr);
    return arr;
}

bool write_parquet_batch(
    const std::string& filepath,
    const std::vector<std::string>& columns,
    const std::vector<Row>& rows)
{
    if (rows.empty() || columns.empty()) return true;

    std::vector<std::shared_ptr<arrow::Array>>  arrays;
    std::vector<std::shared_ptr<arrow::Field>>  fields;
    arrays.reserve(columns.size());
    fields.reserve(columns.size());

    for (std::size_t i = 0; i < columns.size(); ++i) {
        auto a = build_array_for_column(rows, i);
        if (!a) return false;
        std::shared_ptr<arrow::DataType> dt;
        switch (a->type_id()) {
            case arrow::Type::INT64:   dt = arrow::int64();   break;
            case arrow::Type::DOUBLE:  dt = arrow::float64(); break;
            case arrow::Type::BOOL:    dt = arrow::boolean(); break;
            default:                   dt = arrow::utf8();    break;
        }
        arrays.push_back(a);
        fields.push_back(arrow::field(columns[i], dt));
    }
    auto schema = std::make_shared<arrow::Schema>(fields);
    auto table  = arrow::Table::Make(schema, arrays);

    // Atomic write: tmp → rename
    const std::string tmp = filepath + ".tmp";
    std::shared_ptr<arrow::io::FileOutputStream> outfile;
    {
        auto r = arrow::io::FileOutputStream::Open(tmp);
        if (!r.ok()) {
            std::cerr << "[parquet] open " << tmp << ": "
                      << r.status().ToString() << "\n";
            return false;
        }
        outfile = *r;
    }
    parquet::WriterProperties::Builder props_b;
    props_b.compression(parquet::Compression::SNAPPY);
    auto props = props_b.build();
    auto status = parquet::arrow::WriteTable(
        *table, arrow::default_memory_pool(),
        outfile, /*chunk_size*/ static_cast<int64_t>(rows.size()),
        props);
    if (!status.ok()) {
        std::cerr << "[parquet] write " << tmp << ": "
                  << status.ToString() << "\n";
        return false;
    }
    auto close_status = outfile->Close();
    if (!close_status.ok()) {
        std::cerr << "[parquet] close " << tmp << ": "
                  << close_status.ToString() << "\n";
        return false;
    }
    std::error_code ec;
    fs::rename(tmp, filepath, ec);
    if (ec) {
        std::cerr << "[parquet] rename " << tmp << " → " << filepath
                  << ": " << ec.message() << "\n";
        return false;
    }
    return true;
}

// ──────────────────────────────────────────────────────────────
// FLUSH
// ──────────────────────────────────────────────────────────────

void perform_flush(const ObjectKey& key)
{
    ObjectBuffer* buf = nullptr;
    {
        std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
        auto it = g_objects.find(key);
        if (it == g_objects.end()) return;
        buf = it->second.get();
    }
    if (!buf) return;

    // Extract rows under the per-object lock.
    std::vector<std::string> columns;
    std::vector<Row>          rows;
    uint64_t                  seq;
    std::string               obj_dir;
    {
        std::lock_guard<std::mutex> lk(buf->mtx);
        if (buf->rows.empty()) return;
        columns = buf->columns;
        rows    = std::move(buf->rows);
        buf->rows.clear();
        buf->last_flush = SteadyClk::now();
        seq             = buf->batch_seq++;
        obj_dir = (fs::path(TRAINING_DIR)
                   / type_to_dirname(buf->object_bucket)
                   / buf->object_id).string();
    }

    fs::create_directories(obj_dir);
    char filename[64];
    std::snprintf(filename, sizeof(filename), "batch_%06llu.parquet",
                  static_cast<unsigned long long>(seq));
    const std::string filepath = obj_dir + "/" + filename;

    if (write_parquet_batch(filepath, columns, rows)) {
        ++g_stat_batches_written;
        std::cout << "[flush] " << buf->object_bucket << "/" << buf->object_id
                  << " → " << filename << " (" << rows.size() << " rows)\n";
    } else {
        ++g_stat_parquet_errors;
        // On failure, push rows back at the front (best effort).
        std::lock_guard<std::mutex> lk(buf->mtx);
        // Avoid unbounded retry loops — append behind any new rows.
        buf->rows.insert(buf->rows.end(),
                          std::make_move_iterator(rows.begin()),
                          std::make_move_iterator(rows.end()));
    }
    g_index_dirty.store(true);
    g_index_cv.notify_one();
}

// ──────────────────────────────────────────────────────────────
// INGEST: read JSON, build row, append to per-object buffer
// ──────────────────────────────────────────────────────────────

void ingest_one_file(const std::string& path)
{
    ++g_stat_files_seen;

    std::ifstream f(path);
    if (!f.is_open()) { ++g_stat_files_failed; return; }
    std::ostringstream ss; ss << f.rdbuf();
    f.close();

    json raw;
    try {
        raw = json::parse(ss.str());
    } catch (const json::parse_error& e) {
        ++g_stat_files_failed;
        std::cerr << "[ingest] parse error " << path << ": " << e.what() << "\n";
        return;
    }

    // ── Object identity ────────────────────────────────────────
    std::string object_id;
    if (raw.contains("id") && raw["id"].is_string())
        object_id = raw["id"].get<std::string>();
    else if (raw.contains("object_id") && raw["object_id"].is_string())
        object_id = raw["object_id"].get<std::string>();
    if (object_id.empty()) {
        // Fall back to parent directory name.
        try {
            object_id = fs::path(path).parent_path().filename().string();
        } catch (...) {}
    }
    if (object_id.empty()) { ++g_stat_files_failed; return; }

    // ── Raw sub-type (drives provenance, NOT folder routing) ──
    std::string object_type = "unknown";
    if (raw.contains("type") && raw["type"].is_string())
        object_type = raw["type"].get<std::string>();
    else if (raw.contains("object_type") && raw["object_type"].is_string())
        object_type = raw["object_type"].get<std::string>();

    // ── Canonical bucket (drives folder + per-type schema) ────
    // Order of trust:
    //   1. payload["object_bucket"] stamped by dispatcher/GSM
    //   2. canonical_bucket(object_type) — collapses sub-types
    //   3. grandparent directory: received_transmissions/{bucket}/{oid}/file
    std::string object_bucket;
    if (raw.contains("object_bucket") && raw["object_bucket"].is_string()) {
        object_bucket = raw["object_bucket"].get<std::string>();
    } else if (object_type != "unknown") {
        object_bucket = canonical_bucket(object_type);
    } else {
        try {
            object_bucket = fs::path(path).parent_path().parent_path()
                                .filename().string();
            // De-pluralise legacy folder names if seen on disk.
            if (object_bucket == "satellites") object_bucket = "satellite";
            else if (object_bucket == "asteroids") object_bucket = "asteroid";
        } catch (...) {}
    }
    if (object_bucket.empty()) object_bucket = "unknown";

    // The buffer key is (object_id, object_bucket) — NOT (object_id,
    // object_type). This way debris and a generated fragment of debris
    // co-locate in the same bucket but stay separate per-id.
    ObjectKey key{object_id, object_bucket};

    // Lookup or insert buffer.
    ObjectBuffer* buf = nullptr;
    {
        std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
        auto it = g_objects.find(key);
        if (it != g_objects.end()) buf = it->second.get();
    }
    if (!buf) {
        std::unique_lock<std::shared_mutex> lk(g_objects_mutex);
        auto it = g_objects.find(key);
        if (it == g_objects.end()) {
            auto p = std::make_unique<ObjectBuffer>();
            p->object_id     = object_id;
            p->object_type   = object_type;     // first-seen sub-type
            p->object_bucket = object_bucket;
            p->last_flush    = SteadyClk::now();
            buf = p.get();
            g_objects.emplace(key, std::move(p));
        } else {
            buf = it->second.get();
        }
    }

    const std::string received_utc = utc_now_iso();

    bool needs_flush = false;
    {
        std::lock_guard<std::mutex> lk(buf->mtx);
        const int64_t seq_id = ++buf->sequence_counter;
        BuiltRow br = build_row(raw, seq_id, object_id,
                                 object_type, object_bucket, received_utc);
        if (buf->columns.empty()) {
            buf->columns = std::move(br.columns);
            resolve_buffer_indices(*buf);
        }

        // Update stats using cached indices (robust against schema changes).
        ++buf->total_rows;
        if (buf->oldest_record_utc.empty()) buf->oldest_record_utc = received_utc;
        buf->newest_record_utc = received_utc;

        // current_uplink_trip_ms
        if (buf->idx_uplink_ms >= 0
            && static_cast<std::size_t>(buf->idx_uplink_ms) < br.row.size()
            && br.row[buf->idx_uplink_ms].t == CellType::F64) {
            const double v = br.row[buf->idx_uplink_ms].f;
            buf->avg_uplink_trip_ms_sum += v;
            ++buf->avg_uplink_trip_ms_n;
            if (v < buf->min_uplink_trip_ms) buf->min_uplink_trip_ms = v;
            if (v > buf->max_uplink_trip_ms) buf->max_uplink_trip_ms = v;
        }

        // comm_link_type
        if (buf->idx_link_type >= 0
            && static_cast<std::size_t>(buf->idx_link_type) < br.row.size()
            && br.row[buf->idx_link_type].t == CellType::STR) {
            const std::string& lt = br.row[buf->idx_link_type].s;
            ++buf->total_link_obs;
            if      (lt == "direct")     ++buf->direct_count;
            else if (lt == "tdrs_relay") ++buf->tdrs_count;
        }

        // comm_ground_station
        if (buf->idx_ground_station >= 0
            && static_cast<std::size_t>(buf->idx_ground_station) < br.row.size()
            && br.row[buf->idx_ground_station].t == CellType::STR
            && !br.row[buf->idx_ground_station].s.empty()) {
            ++buf->ground_station_counts[br.row[buf->idx_ground_station].s];
        }

        // comm_tdrs_relay
        if (buf->idx_tdrs_relay >= 0
            && static_cast<std::size_t>(buf->idx_tdrs_relay) < br.row.size()
            && br.row[buf->idx_tdrs_relay].t == CellType::STR
            && !br.row[buf->idx_tdrs_relay].s.empty()) {
            ++buf->tdrs_relay_counts[br.row[buf->idx_tdrs_relay].s];
        }

        // was_blackout_recovery
        if (buf->idx_blackout_recov >= 0
            && static_cast<std::size_t>(buf->idx_blackout_recov) < br.row.size()
            && br.row[buf->idx_blackout_recov].t == CellType::BOOL
            && br.row[buf->idx_blackout_recov].b) {
            ++buf->blackout_recovery_count;
        }
        buf->rows.push_back(std::move(br.row));
        ++g_stat_rows_appended;

        const double elapsed = std::chrono::duration<double>(
            SteadyClk::now() - buf->last_flush).count();
        if (buf->rows.size() >= BATCH_SIZE || elapsed >= FLUSH_INTERVAL_S) {
            needs_flush = true;
        }
    }

    if (needs_flush) {
        const std::string in_flight_key = key.oid + "|" + key.type;
        bool enqueued = false;
        {
            std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
            if (g_flush_inflight.find(in_flight_key) == g_flush_inflight.end()) {
                g_flush_inflight.insert(in_flight_key);
                enqueued = true;
            }
        }
        if (enqueued) {
            std::lock_guard<std::mutex> lk(g_flush_queue_mtx);
            g_flush_queue.push(key);
            g_flush_queue_cv.notify_one();
        }
    }
}

// ──────────────────────────────────────────────────────────────
// THREADS
// ──────────────────────────────────────────────────────────────

void watcher_thread_fn()
{
    std::unordered_set<std::string> seen;
    seen.reserve(100000);

    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(WATCHER_POLL_MS));
        if (!g_running.load()) break;

        // Recursively walk SAVE_DIR — only 2 levels deep, fast.
        std::vector<std::string> new_files;
        try {
            for (const auto& type_entry : fs::directory_iterator(SAVE_DIR)) {
                if (!type_entry.is_directory()) continue;
                for (const auto& obj_entry : fs::directory_iterator(type_entry.path())) {
                    if (!obj_entry.is_directory()) continue;
                    for (const auto& f : fs::directory_iterator(obj_entry.path())) {
                        if (!f.is_regular_file()) continue;
                        const auto& p = f.path();
                        if (p.extension() != ".json") continue;
                        const std::string s = p.string();
                        if (s.find(".tmp") != std::string::npos) continue;
                        if (seen.count(s)) continue;
                        new_files.push_back(s);
                    }
                }
            }
        } catch (const std::exception& e) {
            // SAVE_DIR may not exist yet — that's fine.
        }

        if (new_files.empty()) continue;

        // Sort chronologically (filenames contain ns timestamps).
        std::sort(new_files.begin(), new_files.end());

        // Push into work queue with backpressure.
        {
            std::unique_lock<std::mutex> lk(g_work_queue_mtx);
            for (auto& f : new_files) {
                if (g_work_queue.size() >= WORK_QUEUE_MAX) {
                    // Drop oldest — newer data is more valuable
                    g_work_queue.pop();
                }
                g_work_queue.push(std::move(f));
            }
            g_work_queue_cv.notify_all();
        }
        for (auto& f : new_files) seen.insert(f);

        // Periodically prune the seen set (files that no longer exist).
        if (seen.size() > 200'000) {
            std::unordered_set<std::string> on_disk;
            on_disk.reserve(seen.size());
            try {
                for (const auto& type_entry : fs::directory_iterator(SAVE_DIR)) {
                    if (!type_entry.is_directory()) continue;
                    for (const auto& obj_entry : fs::directory_iterator(type_entry.path())) {
                        if (!obj_entry.is_directory()) continue;
                        for (const auto& f : fs::directory_iterator(obj_entry.path())) {
                            if (!f.is_regular_file()) continue;
                            on_disk.insert(f.path().string());
                        }
                    }
                }
            } catch (...) {}
            seen = std::move(on_disk);
        }
    }
}

void ingest_thread_fn()
{
    while (g_running.load()) {
        std::string path;
        {
            std::unique_lock<std::mutex> lk(g_work_queue_mtx);
            g_work_queue_cv.wait(lk, []{
                return !g_work_queue.empty() || !g_running.load();
            });
            if (!g_running.load()) return;
            path = std::move(g_work_queue.front());
            g_work_queue.pop();
        }
        try { ingest_one_file(path); ++g_stat_files_parsed; }
        catch (const std::exception& e) {
            ++g_stat_files_failed;
            std::cerr << "[ingest] error " << path << ": " << e.what() << "\n";
        }
    }
}

void flush_thread_fn()
{
    while (g_running.load()) {
        ObjectKey key;
        {
            std::unique_lock<std::mutex> lk(g_flush_queue_mtx);
            g_flush_queue_cv.wait(lk, []{
                return !g_flush_queue.empty() || !g_running.load();
            });
            if (!g_running.load()) return;
            key = std::move(g_flush_queue.front());
            g_flush_queue.pop();
        }
        try { perform_flush(key); }
        catch (const std::exception& e) {
            ++g_stat_parquet_errors;
            std::cerr << "[flush] error: " << e.what() << "\n";
        }
        // Clear in-flight marker.
        {
            std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
            g_flush_inflight.erase(key.oid + "|" + key.type);
        }
    }
}

void interval_flush_thread_fn()
{
    // Force-flush any buffer whose rows are aging.
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::seconds(
            static_cast<int>(FLUSH_INTERVAL_S / 2)));
        if (!g_running.load()) break;

        std::vector<ObjectKey> to_flush;
        {
            std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
            for (auto& [k, buf] : g_objects) {
                std::lock_guard<std::mutex> blk(buf->mtx);
                if (!buf->rows.empty()) {
                    const double elapsed = std::chrono::duration<double>(
                        SteadyClk::now() - buf->last_flush).count();
                    if (elapsed >= FLUSH_INTERVAL_S) to_flush.push_back(k);
                }
            }
        }
        for (auto& key : to_flush) {
            const std::string in_flight_key = key.oid + "|" + key.type;
            bool enqueued = false;
            {
                std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
                if (g_flush_inflight.find(in_flight_key) == g_flush_inflight.end()) {
                    g_flush_inflight.insert(in_flight_key);
                    enqueued = true;
                }
            }
            if (enqueued) {
                std::lock_guard<std::mutex> lk(g_flush_queue_mtx);
                g_flush_queue.push(key);
                g_flush_queue_cv.notify_one();
            }
        }
    }
}

// ──────────────────────────────────────────────────────────────
// MASTER INDEX REBUILD
// ──────────────────────────────────────────────────────────────
//
// Per-type master_index.parquet has one row per object with summary stats.
// summary.parquet has one row per type.
//
// We collect snapshots under shared lock, then write Parquet without
// holding the lock.
// ──────────────────────────────────────────────────────────────

struct ObjectStatsSnapshot {
    std::string oid, type, bucket, oldest_utc, newest_utc;
    uint64_t total_rows = 0, total_batches = 0;
    double avg_uplink_ms = std::numeric_limits<double>::quiet_NaN();
    double min_uplink_ms = std::numeric_limits<double>::quiet_NaN();
    double max_uplink_ms = std::numeric_limits<double>::quiet_NaN();
    double direct_pct = 0, tdrs_pct = 0, blackout_recovery_pct = 0;
    std::string most_used_gs, most_used_relay;
};

void rebuild_master_index()
{
    std::vector<ObjectStatsSnapshot> snaps;
    {
        std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
        snaps.reserve(g_objects.size());
        for (auto& [k, buf] : g_objects) {
            std::lock_guard<std::mutex> blk(buf->mtx);
            ObjectStatsSnapshot s;
            s.oid           = buf->object_id;
            s.type          = buf->object_type;     // raw sub-type
            s.bucket        = buf->object_bucket;   // canonical group
            s.oldest_utc    = buf->oldest_record_utc;
            s.newest_utc    = buf->newest_record_utc;
            s.total_rows    = buf->total_rows;
            s.total_batches = buf->batch_seq - 1;
            if (buf->avg_uplink_trip_ms_n > 0) {
                s.avg_uplink_ms = buf->avg_uplink_trip_ms_sum
                                  / buf->avg_uplink_trip_ms_n;
                s.min_uplink_ms = buf->min_uplink_trip_ms;
                s.max_uplink_ms = buf->max_uplink_trip_ms;
            }
            if (buf->total_link_obs > 0) {
                s.direct_pct  = 100.0 * buf->direct_count / buf->total_link_obs;
                s.tdrs_pct    = 100.0 * buf->tdrs_count   / buf->total_link_obs;
                s.blackout_recovery_pct =
                    100.0 * buf->blackout_recovery_count / buf->total_link_obs;
            }
            // Most used ground station
            int64_t best = 0;
            for (auto& [name, n] : buf->ground_station_counts) {
                if (n > best) { best = n; s.most_used_gs = name; }
            }
            best = 0;
            for (auto& [name, n] : buf->tdrs_relay_counts) {
                if (n > best) { best = n; s.most_used_relay = name; }
            }
            snaps.push_back(std::move(s));
        }
    }

    // Group by canonical BUCKET (so master_index.parquet for "debris"
    // contains debris, generated fragments, and other debris-family
    // sub-types together).  The raw sub-type is preserved per row in
    // the object_type column.
    std::unordered_map<std::string, std::vector<const ObjectStatsSnapshot*>> grouped;
    for (auto& s : snaps) grouped[s.bucket].push_back(&s);

    static const std::vector<std::string> IDX_COLS = {
        "object_id", "object_type", "object_bucket",
        "total_records", "total_batches",
        "oldest_record_utc", "newest_record_utc",
        "avg_uplink_trip_ms", "min_uplink_trip_ms", "max_uplink_trip_ms",
        "direct_ground_pct", "via_tdrs_pct", "blackout_recovery_pct",
        "most_used_ground_station", "most_used_relay",
        "last_updated_utc"
    };

    const std::string now_iso = utc_now_iso();

    for (auto& [bucket_name, items] : grouped) {
        if (items.empty()) continue;
        std::vector<Row> rows;
        rows.reserve(items.size());
        for (auto* s : items) {
            Row r;
            r.push_back(Cell::make_str(s->oid));
            r.push_back(Cell::make_str(s->type));      // raw sub-type
            r.push_back(Cell::make_str(s->bucket));    // canonical bucket
            r.push_back(Cell::make_i64(static_cast<int64_t>(s->total_rows)));
            r.push_back(Cell::make_i64(static_cast<int64_t>(s->total_batches)));
            r.push_back(s->oldest_utc.empty() ? Cell::make_nil() : Cell::make_str(s->oldest_utc));
            r.push_back(s->newest_utc.empty() ? Cell::make_nil() : Cell::make_str(s->newest_utc));
            if (std::isnan(s->avg_uplink_ms)) {
                r.push_back(Cell::make_nil());
                r.push_back(Cell::make_nil());
                r.push_back(Cell::make_nil());
            } else {
                r.push_back(Cell::make_f64(s->avg_uplink_ms));
                r.push_back(Cell::make_f64(s->min_uplink_ms));
                r.push_back(Cell::make_f64(s->max_uplink_ms));
            }
            r.push_back(Cell::make_f64(s->direct_pct));
            r.push_back(Cell::make_f64(s->tdrs_pct));
            r.push_back(Cell::make_f64(s->blackout_recovery_pct));
            r.push_back(s->most_used_gs.empty()
                        ? Cell::make_nil() : Cell::make_str(s->most_used_gs));
            r.push_back(s->most_used_relay.empty()
                        ? Cell::make_nil() : Cell::make_str(s->most_used_relay));
            r.push_back(Cell::make_str(now_iso));
            rows.push_back(std::move(r));
        }

        const std::string type_dir =
            (fs::path(TRAINING_DIR) / type_to_dirname(bucket_name)).string();
        fs::create_directories(type_dir);
        const std::string idx_path = type_dir + "/master_index.parquet";
        write_parquet_batch(idx_path, IDX_COLS, rows);
    }

    // Top-level summary.parquet — one row per bucket.
    static const std::vector<std::string> SUM_COLS = {
        "object_bucket", "bucket_dir", "objects_count",
        "total_records", "total_batches", "last_updated_utc"
    };
    std::vector<Row> srows;
    for (auto& [bucket_name, items] : grouped) {
        if (items.empty()) continue;
        uint64_t totalr = 0, totalb = 0;
        for (auto* s : items) { totalr += s->total_rows; totalb += s->total_batches; }
        Row r;
        r.push_back(Cell::make_str(bucket_name));
        r.push_back(Cell::make_str(type_to_dirname(bucket_name)));
        r.push_back(Cell::make_i64(static_cast<int64_t>(items.size())));
        r.push_back(Cell::make_i64(static_cast<int64_t>(totalr)));
        r.push_back(Cell::make_i64(static_cast<int64_t>(totalb)));
        r.push_back(Cell::make_str(now_iso));
        srows.push_back(std::move(r));
    }
    if (!srows.empty()) {
        fs::create_directories(TRAINING_DIR);
        write_parquet_batch(
            (fs::path(TRAINING_DIR) / "summary.parquet").string(),
            SUM_COLS, srows);
    }
}

void index_thread_fn()
{
    while (g_running.load()) {
        std::unique_lock<std::mutex> lk(g_index_mtx);
        g_index_cv.wait_for(lk, std::chrono::seconds(5), []{
            return g_index_dirty.load() || !g_running.load();
        });
        if (!g_running.load()) return;
        if (g_index_dirty.exchange(false)) {
            lk.unlock();
            try { rebuild_master_index(); }
            catch (const std::exception& e) {
                std::cerr << "[index] error: " << e.what() << "\n";
            }
        }
    }
}

// ──────────────────────────────────────────────────────────────
// CLEANUP THREADS
// ──────────────────────────────────────────────────────────────

void json_cleanup_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < CLEANUP_JSON_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const double cutoff = now_seconds() - JSON_TTL_S;
        int deleted = 0;
        try {
            for (const auto& type_entry : fs::directory_iterator(SAVE_DIR)) {
                if (!type_entry.is_directory()) continue;
                for (const auto& obj_entry : fs::directory_iterator(type_entry.path())) {
                    if (!obj_entry.is_directory()) continue;
                    for (const auto& f : fs::directory_iterator(obj_entry.path())) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().extension() != ".json") continue;
                        try {
                            if (file_mtime_seconds(f.path()) < cutoff) {
                                fs::remove(f.path()); ++deleted;
                            }
                        } catch (...) {}
                    }
                }
            }
        } catch (...) {}
        if (deleted > 0)
            std::cout << "[cleanup-json] deleted " << deleted
                      << " stale JSON file(s)\n";
    }
}

void parquet_cleanup_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < CLEANUP_PARQUET_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const double cutoff = now_seconds() - PARQUET_TTL_S;
        int deleted = 0;
        try {
            for (const auto& type_dir : fs::directory_iterator(TRAINING_DIR)) {
                if (!type_dir.is_directory()) continue;
                for (const auto& obj_dir : fs::directory_iterator(type_dir.path())) {
                    if (!obj_dir.is_directory()) continue;
                    for (const auto& f : fs::directory_iterator(obj_dir.path())) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().extension() != ".parquet") continue;
                        // Skip master_index files
                        if (f.path().filename() == "master_index.parquet") continue;
                        try {
                            if (file_mtime_seconds(f.path()) < cutoff) {
                                fs::remove(f.path()); ++deleted;
                            }
                        } catch (...) {}
                    }
                }
            }
        } catch (...) {}
        if (deleted > 0) {
            std::cout << "[cleanup-parquet] deleted " << deleted
                      << " stale Parquet batch(es)\n";
            g_index_dirty.store(true);
            g_index_cv.notify_one();
        }
    }
}

// ──────────────────────────────────────────────────────────────
// STATS THREAD
// ──────────────────────────────────────────────────────────────

void stats_thread_fn()
{
    uint64_t last_parsed = 0;
    while (g_running.load()) {
        for (int i = 0; i < STATS_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const uint64_t parsed = g_stat_files_parsed.load();
        const double rate = static_cast<double>(parsed - last_parsed) / STATS_INTERVAL_S;
        last_parsed = parsed;

        std::size_t wq, fq, n_obj;
        { std::lock_guard<std::mutex> lk(g_work_queue_mtx);  wq = g_work_queue.size(); }
        { std::lock_guard<std::mutex> lk(g_flush_queue_mtx); fq = g_flush_queue.size(); }
        { std::shared_lock<std::shared_mutex> lk(g_objects_mutex); n_obj = g_objects.size(); }

        std::cout << "[stats] seen=" << g_stat_files_seen.load()
                  << " parsed=" << parsed
                  << " (" << rate << "/s)"
                  << " failed=" << g_stat_files_failed.load()
                  << " rows=" << g_stat_rows_appended.load()
                  << " batches=" << g_stat_batches_written.load()
                  << " objects=" << n_obj
                  << " workQ=" << wq
                  << " flushQ=" << fq
                  << " errors=" << g_stat_parquet_errors.load()
                  << "\n";
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────
void install_signal_handlers()
{
    auto handler = [](int){
        g_running.store(false);
        g_work_queue_cv.notify_all();
        g_flush_queue_cv.notify_all();
        g_index_cv.notify_all();
    };
    std::signal(SIGINT,  handler);
    std::signal(SIGTERM, handler);
}

// ──────────────────────────────────────────────────────────────
// MAIN
// ──────────────────────────────────────────────────────────────

int main()
{
    std::cout << "================================================\n"
              << " ground_station_processor.cpp v1\n"
              << "================================================\n";

    fs::create_directories(SAVE_DIR);
    fs::create_directories(TRAINING_DIR);
    install_signal_handlers();

    std::cout << "[main] save_dir=" << SAVE_DIR
              << " training_dir=" << TRAINING_DIR << "\n"
              << "[main] batch_size=" << BATCH_SIZE
              << " flush_interval=" << FLUSH_INTERVAL_S << "s\n"
              << "[main] json_ttl=" << JSON_TTL_S
              << "s parquet_ttl=" << PARQUET_TTL_S << "s\n"
              << "[main] ingest_threads=" << INGEST_THREAD_COUNT << "\n";

    std::thread t_watcher (watcher_thread_fn);
    std::vector<std::thread> ingest_threads;
    for (unsigned i = 0; i < INGEST_THREAD_COUNT; ++i)
        ingest_threads.emplace_back(ingest_thread_fn);
    std::thread t_flush          (flush_thread_fn);
    std::thread t_interval_flush (interval_flush_thread_fn);
    std::thread t_index          (index_thread_fn);
    std::thread t_cleanup_json   (json_cleanup_thread_fn);
    std::thread t_cleanup_parq   (parquet_cleanup_thread_fn);
    std::thread t_stats          (stats_thread_fn);

    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutdown initiated, draining queues ...\n";

    // Notify everyone to wake up.
    g_work_queue_cv.notify_all();
    g_flush_queue_cv.notify_all();
    g_index_cv.notify_all();

    t_watcher.join();
    for (auto& t : ingest_threads) t.join();
    t_flush.join();
    t_interval_flush.join();
    t_index.join();
    t_cleanup_json.join();
    t_cleanup_parq.join();
    t_stats.join();

    // Final flush of all remaining buffered rows.
    std::cout << "[main] final flush of all buffers ...\n";
    {
        std::vector<ObjectKey> all_keys;
        {
            std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
            for (auto& [k, buf] : g_objects) all_keys.push_back(k);
        }
        for (auto& k : all_keys) {
            try { perform_flush(k); }
            catch (const std::exception& e) {
                std::cerr << "[final flush] " << e.what() << "\n";
            }
        }
    }
    try { rebuild_master_index(); }
    catch (const std::exception& e) {
        std::cerr << "[final index] " << e.what() << "\n";
    }

    std::cout << "[main] clean exit. parsed=" << g_stat_files_parsed
              << " rows=" << g_stat_rows_appended
              << " batches=" << g_stat_batches_written
              << " errors=" << g_stat_parquet_errors << "\n";
    return 0;
}
