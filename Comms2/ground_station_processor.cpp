// ============================================================
// ground_station_processor.cpp  —  v2  (session folders, new schema)
// ============================================================
//
// WHAT THIS FILE DOES
// ───────────────────
// Watches the GSM's session json/ folder for incoming per-object
// JSON files, batches them into Parquet training data, and maintains
// table-of-contents index files.
//
// SESSION FOLDER LAYOUT  (processor reads json/, writes parquet/)
// ───────────────────────────────────────────────────────────────
//   received_transmissions/
//     2026-05-05T12-43-05/                ← session (created by GSM manager)
//       json/                             ← 3-min TTL files (GSM manager writes)
//         satellites/NORAD-20580/ping_*.json
//         asteroids/AST-001/ping_*.json
//         debris/NORAD-33757/ping_*.json
//       parquet/                          ← PERMANENT — processor writes here
//         satellites/
//           NORAD-20580/
//             batch_000001.parquet
//           master_index.parquet          ← one row per satellite object
//         asteroids/
//           master_index.parquet
//         debris/
//           master_index.parquet
//         summary.parquet                 ← one row per bucket
//       json_index.parquet               ← one row per JSON file received
//
// HOW THE PROCESSOR FINDS THE SESSION FOLDER
// ────────────────────────────────────────────
// It scans received_transmissions/ for the most recently modified
// subdirectory (the active session) and watches its json/ subtree.
// If a new session folder appears (GSM manager restarted), the
// processor switches automatically.
//
// THREADS
// ───────
//   1× watcher_thread         — polls json/ subdir, enqueues new file paths
//   N× ingest_threads         — parse JSON, append rows to per-object buffers
//   1× flush_thread           — writes Parquet batches via Arrow
//   1× interval_flush_thread  — force-flush slow objects after FLUSH_INTERVAL_S
//   1× index_thread           — rebuilds master_index + summary + json_index
//   1× cleanup_thread         — removes stale .tmp files (safety)
//   1× stats_thread           — periodic console heartbeat
//
// PARQUET SCHEMA  (3 buckets, each has its own column set)
// ──────────────────────────────────────────────────────────
//   Common columns (all buckets):
//     object_id, object_type, object_bucket, session,
//     sequence_id, received_utc,
//     gsm_receive_time_ms, gsm_receive_local_iso_ms,
//     sms_send_time_ms, sms_to_gsm_trip_ms,
//     packet.frame, packet.universe_time_ms,
//     packet.t0_local_clock_iso_ms, packet.t0_local_unix_time_ms,
//     packet.elapsed_ms_since_t0, packet.sample_hz,
//     packet.current_local_clock_iso_ms, packet.current_local_unix_time_ms,
//     dispatcher.link_type, dispatcher.ground_station,
//     dispatcher.tdrs_relay, dispatcher.propagation_delay_ms,
//     dispatcher.distance_m,
//     data.position_m_eci.{x,y,z},
//     data.velocity_mps_eci.{x,y,z},
//     data.orbit_class, data.active, data.mass_kg, data.physical_radius_m
//
//   Satellite-extra columns:
//     data.power_system.{battery_percent, solar_generation_w, load_w, ...}
//     data.thermal_profile.{bus_temperature_c, battery_temperature_c, ...}
//     data.radiation.{dose_rate_msv_per_day, particle_flux_pfu, ...}
//     data.communication_link.{link_available, link_snr_db, ...}
//     data.camera_sensor.{camera_health_percent, sensor_confidence, ...}
//     data.tcad_sensor_degradation.*
//     (plus many more — see SAT_COLUMNS below)
//
//   Debris/asteroid-extra: trimmed schema (no power/camera/attitude)
//
// FRIENDS READING PARQUET
// ────────────────────────
// Each batch file is self-describing (Arrow schema embedded).
// Master index tells you which batch files exist per object.
// Summary tells you object counts per bucket.
// json_index tells you every raw JSON file received (useful for audit).
//
// macOS BUILD
// ───────────
//   brew install apache-arrow nlohmann-json
//   ./build_processor.sh
//   ./ground_station_processor
//   (no --broker flag needed — disk only)
// ============================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
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
#include <ctime>
#include <limits>

#include <nlohmann/json.hpp>
#include <arrow/api.h>
#include <arrow/io/api.h>
#include <parquet/arrow/writer.h>
#include <parquet/properties.h>

namespace fs    = std::filesystem;
using json      = nlohmann::json;
using SteadyClk = std::chrono::steady_clock;
using SysClock  = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static const std::string ROOT_DIR              = "received_transmissions";
static const std::size_t BATCH_SIZE            = 1000;        // rows per Parquet batch
static const double      FLUSH_INTERVAL_S      = 60.0;        // max seconds before forced flush
static const int         WATCHER_POLL_MS       = 25;          // file watcher poll (ms)
static const int         SESSION_SCAN_INTERVAL_S = 10;        // rescan for new session folders
static const int         STATS_INTERVAL_S      = 10;
static const std::size_t WORK_QUEUE_MAX        = 500'000;
static const unsigned    INGEST_THREADS        = std::min(16u,
    std::max(2u, std::thread::hardware_concurrency() / 2));

// ──────────────────────────────────────────────────────────────
// CELL / ROW TYPES  (typed value storage for Arrow output)
// ──────────────────────────────────────────────────────────────
enum class CellType : uint8_t { I64, F64, BOOL, STR, NIL };
struct Cell {
    CellType    t = CellType::NIL;
    int64_t     i = 0;
    double      f = 0.0;
    bool        b = false;
    std::string s;
    static Cell make_i64 (int64_t v)      { Cell c; c.t=CellType::I64;  c.i=v; return c; }
    static Cell make_f64 (double  v)      { Cell c; c.t=CellType::F64;  c.f=v; return c; }
    static Cell make_bool(bool    v)      { Cell c; c.t=CellType::BOOL; c.b=v; return c; }
    static Cell make_str (std::string v)  { Cell c; c.t=CellType::STR;  c.s=std::move(v); return c; }
    static Cell make_nil ()               { return Cell{}; }
};
using Row = std::vector<Cell>;

// ──────────────────────────────────────────────────────────────
// SAFE EXTRACTORS  (never throw, always return Cell)
// ──────────────────────────────────────────────────────────────
inline Cell jf(const json& j, const char* k) {
    if (!j.is_object() || !j.contains(k) || j[k].is_null()) return Cell::make_nil();
    try {
        if (j[k].is_number_float())   return Cell::make_f64(j[k].get<double>());
        if (j[k].is_number_integer()) return Cell::make_f64(static_cast<double>(j[k].get<int64_t>()));
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell ji(const json& j, const char* k) {
    if (!j.is_object() || !j.contains(k) || j[k].is_null()) return Cell::make_nil();
    try {
        if (j[k].is_number_integer()) return Cell::make_i64(j[k].get<int64_t>());
        if (j[k].is_number_float())   return Cell::make_i64(static_cast<int64_t>(j[k].get<double>()));
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jb(const json& j, const char* k) {
    if (!j.is_object() || !j.contains(k) || j[k].is_null()) return Cell::make_nil();
    try {
        if (j[k].is_boolean()) return Cell::make_bool(j[k].get<bool>());
        if (j[k].is_number())  return Cell::make_bool(j[k].get<int64_t>() != 0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell js(const json& j, const char* k) {
    if (!j.is_object() || !j.contains(k) || j[k].is_null()) return Cell::make_nil();
    try { if (j[k].is_string()) return Cell::make_str(j[k].get<std::string>()); }
    catch (...) {}
    return Cell::make_nil();
}
// Nested path helper: get sub-object.
inline const json* jn(const json& j, std::initializer_list<const char*> path) {
    const json* cur = &j;
    for (auto k : path) {
        if (!cur->is_object() || !cur->contains(k) || (*cur)[k].is_null()) return nullptr;
        cur = &(*cur)[k];
    }
    return cur;
}
inline Cell jnf(const json& j, std::initializer_list<const char*> path) {
    const json* p = jn(j, path); if (!p) return Cell::make_nil();
    try {
        if (p->is_number_float())   return Cell::make_f64(p->get<double>());
        if (p->is_number_integer()) return Cell::make_f64(static_cast<double>(p->get<int64_t>()));
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jni(const json& j, std::initializer_list<const char*> path) {
    const json* p = jn(j, path); if (!p) return Cell::make_nil();
    try {
        if (p->is_number_integer()) return Cell::make_i64(p->get<int64_t>());
        if (p->is_number_float())   return Cell::make_i64(static_cast<int64_t>(p->get<double>()));
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jnb(const json& j, std::initializer_list<const char*> path) {
    const json* p = jn(j, path); if (!p) return Cell::make_nil();
    try {
        if (p->is_boolean()) return Cell::make_bool(p->get<bool>());
        if (p->is_number())  return Cell::make_bool(p->get<int64_t>() != 0);
    } catch (...) {}
    return Cell::make_nil();
}
inline Cell jns(const json& j, std::initializer_list<const char*> path) {
    const json* p = jn(j, path); if (!p) return Cell::make_nil();
    try { if (p->is_string()) return Cell::make_str(p->get<std::string>()); }
    catch (...) {}
    return Cell::make_nil();
}

// ──────────────────────────────────────────────────────────────
// COLUMN SCHEMAS
// ──────────────────────────────────────────────────────────────

static const std::vector<std::string> COMMON_COLUMNS = {
    // identity
    "object_id", "object_type", "object_bucket", "session",
    "sequence_id", "received_utc",
    // GSM timing (ms)
    "gsm_receive_time_ms", "gsm_receive_local_iso_ms",
    "sms_send_time_ms", "sms_to_gsm_trip_ms",
    // packet block (SMS T0 and universe time)
    "frame", "universe_time_ms", "sample_hz",
    "t0_local_clock_iso_ms", "t0_local_unix_time_ms",
    "elapsed_ms_since_t0",
    "current_local_clock_iso_ms", "current_local_unix_time_ms",
    // dispatcher block
    "comm_link_type", "comm_ground_station", "comm_tdrs_relay",
    "comm_propagation_delay_ms", "comm_distance_m",
    // kinematics
    "pos_x_m", "pos_y_m", "pos_z_m",
    "vel_x_mps", "vel_y_mps", "vel_z_mps",
    // physical
    "orbit_class", "active", "destroyed",
    "mass_kg", "physical_radius_m", "central_body",
};

static const std::vector<std::string> SAT_EXTRA_COLUMNS = {
    // UCS/catalog
    "name", "catalog_name", "norad_cat_id", "purpose", "users",
    "operator_owner", "country_of_operator_owner",
    // power
    "battery_percent", "battery_capacity_wh",
    "solar_generation_w", "load_w", "base_load_w",
    "rf_payload_load_w", "radiation_fault_load_w",
    "power_sensor_confidence",
    // voltage
    "solar_panel_voltage_v", "solar_panel_current_a",
    "battery_voltage_v", "bus_voltage_v",
    "bus_current_draw_a", "net_charge_current_a",
    "voltage_sensor_confidence",
    // thermal
    "bus_temperature_c", "battery_temperature_c",
    "processor_temperature_c", "power_amp_temperature_c",
    "temperature_sensor_noise_c", "thermal_sensor_confidence",
    // radiation
    "radiation_region", "shielding_mm_aluminum", "shielding_factor",
    "dose_rate_msv_per_day", "particle_flux_pfu",
    "particle_flux_pfu_gt_10mev",
    "seu_probability_per_day",
    "solar_panel_degradation_fraction_per_day",
    "comms_blackout_probability",
    "cumulative_dose_msv", "electronics_health_percent",
    "solar_storm_active", "sunlit_side", "earth_shadow_shielded",
    // communication
    "link_available", "link_snr_db", "packet_loss_probability",
    "latency_ms", "rf_blackout_probability",
    "antenna_pointing_factor", "comm_sensor_confidence",
    // camera
    "camera_enabled", "camera_sensor_type",
    "image_noise_fraction", "hot_pixel_fraction",
    "dead_pixel_fraction", "dark_current_factor",
    "frame_corruption_probability", "camera_health_percent",
    "camera_sensor_confidence", "camera_radiation_degraded",
    "camera_min_useful_health_percent",
    // imaging geometry
    "imaging_target_name", "slant_range_to_target_m",
    "off_nadir_angle_deg", "camera_half_angle_deg",
    "horizon_visible", "within_camera_cone",
    // attitude
    "roll_deg", "pitch_deg", "yaw_deg", "panel_rotation_deg",
    "attitude_sensor_confidence",
    // tcad
    "tcad_attitude_confidence", "tcad_attitude_safe",
    "tcad_camera_confidence",   "tcad_camera_safe",
    "tcad_comm_confidence",     "tcad_comm_safe",
    "tcad_solar_confidence",    "tcad_solar_safe",
    "tcad_processor_confidence","tcad_processor_safe",
    // sunlight
    "in_sunlight", "in_eclipse", "sun_exposure_factor",
    // selection
    "selected_for_data", "can_maneuver",
};

static const std::vector<std::string> DEBRIS_EXTRA_COLUMNS = {
    "norad_cat_id", "mass_source", "mass_confidence",
    "generated_fragment_boolean", "fragment_event_id",
    "fragment_sequence_number", "parent_object_ids_json",
    "created_at_frame", "created_at_physical_time_ms",
};

static const std::vector<std::string> ASTEROID_EXTRA_COLUMNS = {
    "name", "catalog_name", "norad_cat_id",
    "mass_source", "mass_confidence",
};

// ──────────────────────────────────────────────────────────────
// ROW BUILDERS
// ──────────────────────────────────────────────────────────────

std::string utc_now_iso()
{
    const time_t t = SysClock::to_time_t(SysClock::now());
    std::tm gmt{};
#if defined(_WIN32)
    gmtime_s(&gmt, &t);
#else
    gmtime_r(&t, &gmt);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf),
        "%04d-%02d-%02dT%02d:%02d:%02dZ",
        gmt.tm_year+1900, gmt.tm_mon+1, gmt.tm_mday,
        gmt.tm_hour, gmt.tm_min, gmt.tm_sec);
    return buf;
}

void append_common_row(const json& raw,
                        const std::string& session,
                        int64_t sequence_id,
                        const std::string& received_utc,
                        Row& row)
{
    const json& gsm  = raw.contains("gsm")        && raw["gsm"].is_object()
                       ? raw["gsm"] : json::object();
    const json& pkt  = raw.contains("packet")     && raw["packet"].is_object()
                       ? raw["packet"] : json::object();
    const json& disp = raw.contains("dispatcher") && raw["dispatcher"].is_object()
                       ? raw["dispatcher"] : json::object();
    const json& data = raw.contains("data")       && raw["data"].is_object()
                       ? raw["data"] : json::object();

    // identity
    row.push_back(js(raw,  "object_id"));
    row.push_back(js(raw,  "object_type"));
    row.push_back(js(raw,  "object_bucket"));
    row.push_back(Cell::make_str(session));
    row.push_back(Cell::make_i64(sequence_id));
    row.push_back(Cell::make_str(received_utc));

    // GSM timing
    row.push_back(jf(gsm,  "gsm_receive_time_ms"));
    row.push_back(js(gsm,  "gsm_receive_local_iso_ms"));
    row.push_back(jf(disp, "sms_send_time_ms"));
    row.push_back(jf(gsm,  "sms_to_gsm_trip_ms"));

    // packet block
    row.push_back(ji(pkt,  "frame"));
    row.push_back(ji(pkt,  "universe_time_ms"));
    row.push_back(jf(pkt,  "sample_hz"));
    row.push_back(js(pkt,  "t0_local_clock_iso_ms"));
    row.push_back(jf(pkt,  "t0_local_unix_time_ms"));
    row.push_back(ji(pkt,  "elapsed_ms_since_t0"));
    row.push_back(js(pkt,  "current_local_clock_iso_ms"));
    row.push_back(jf(pkt,  "current_local_unix_time_ms"));

    // dispatcher
    row.push_back(js(disp, "link_type"));
    row.push_back(js(disp, "ground_station"));
    row.push_back(js(disp, "tdrs_relay"));
    row.push_back(jf(disp, "propagation_delay_ms"));
    row.push_back(jf(disp, "distance_m"));

    // kinematics (nested inside data.position_m_eci)
    row.push_back(jnf(data, {"position_m_eci","x"}));
    row.push_back(jnf(data, {"position_m_eci","y"}));
    row.push_back(jnf(data, {"position_m_eci","z"}));
    row.push_back(jnf(data, {"velocity_mps_eci","x"}));
    row.push_back(jnf(data, {"velocity_mps_eci","y"}));
    row.push_back(jnf(data, {"velocity_mps_eci","z"}));

    // physical
    row.push_back(js(data,  "orbit_class"));
    row.push_back(jb(data,  "active"));
    row.push_back(jb(data,  "destroyed"));
    row.push_back(jf(data,  "mass_kg"));
    row.push_back(jf(data,  "physical_radius_m"));
    row.push_back(js(data,  "central_body"));
}

void append_satellite_extra(const json& raw, Row& row)
{
    const json& d    = raw.contains("data") && raw["data"].is_object()
                       ? raw["data"] : json::object();
    const json& pw   = jn(d, {"power_system"})     ? *jn(d, {"power_system"})     : json::object();
    const json& vs   = jn(d, {"voltage_sensors"})  ? *jn(d, {"voltage_sensors"})  : json::object();
    const json& th   = jn(d, {"thermal_profile"})  ? *jn(d, {"thermal_profile"})  : json::object();
    const json& rad  = jn(d, {"radiation"})         ? *jn(d, {"radiation"})         : json::object();
    const json& cl   = jn(d, {"communication_link"})? *jn(d, {"communication_link"}): json::object();
    const json& cam  = jn(d, {"camera_sensor"})    ? *jn(d, {"camera_sensor"})    : json::object();
    const json& img  = jn(d, {"imaging_target_geometry"}) ? *jn(d, {"imaging_target_geometry"}) : json::object();
    const json& att  = jn(d, {"attitude_state"})   ? *jn(d, {"attitude_state"})   : json::object();
    const json& tcad = jn(d, {"tcad_sensor_degradation"}) ? *jn(d, {"tcad_sensor_degradation"}) : json::object();
    const json& sun  = jn(d, {"sunlight_state"})   ? *jn(d, {"sunlight_state"})   : json::object();
    const json& rad_flags = jn(rad, {"flags"}) ? *jn(rad, {"flags"}) : json::object();

    // UCS/catalog
    row.push_back(js(d,"name")); row.push_back(js(d,"catalog_name"));
    row.push_back(ji(d,"norad_cat_id")); row.push_back(js(d,"purpose"));
    row.push_back(js(d,"users")); row.push_back(js(d,"operator_owner"));
    row.push_back(js(d,"country_of_operator_owner"));

    // power
    row.push_back(jf(pw,"battery_percent")); row.push_back(jf(pw,"battery_capacity_wh"));
    row.push_back(jf(pw,"solar_generation_w")); row.push_back(jf(pw,"load_w"));
    row.push_back(jf(pw,"base_load_w")); row.push_back(jf(pw,"rf_payload_load_w"));
    row.push_back(jf(pw,"radiation_fault_load_w")); row.push_back(jf(pw,"sensor_confidence"));

    // voltage
    row.push_back(jf(vs,"solar_panel_voltage_v")); row.push_back(jf(vs,"solar_panel_current_a"));
    row.push_back(jf(vs,"battery_voltage_v")); row.push_back(jf(vs,"bus_voltage_v"));
    row.push_back(jf(vs,"bus_current_draw_a")); row.push_back(jf(vs,"net_charge_current_a"));
    row.push_back(jf(vs,"sensor_confidence"));

    // thermal
    row.push_back(jf(th,"bus_temperature_c")); row.push_back(jf(th,"battery_temperature_c"));
    row.push_back(jf(th,"processor_temperature_c")); row.push_back(jf(th,"power_amp_temperature_c"));
    row.push_back(jf(th,"temperature_sensor_noise_c")); row.push_back(jf(th,"sensor_confidence"));

    // radiation
    row.push_back(js(rad,"radiation_region")); row.push_back(jf(rad,"shielding_mm_aluminum"));
    row.push_back(jf(rad,"shielding_factor")); row.push_back(jf(rad,"dose_rate_msv_per_day"));
    row.push_back(jf(rad,"particle_flux_pfu")); row.push_back(jf(rad,"particle_flux_pfu_gt_10mev"));
    row.push_back(jf(rad,"single_event_upset_probability_per_day"));
    row.push_back(jf(rad,"solar_panel_degradation_fraction_per_day"));
    row.push_back(jf(rad,"communications_blackout_probability"));
    row.push_back(jf(rad,"cumulative_estimated_dose_msv"));
    row.push_back(jf(rad,"electronics_health_percent"));
    row.push_back(jb(rad_flags,"solar_storm_active"));
    row.push_back(jb(rad_flags,"sunlit_side"));
    row.push_back(jb(rad_flags,"earth_shadow_shielded"));

    // communication
    row.push_back(jb(cl,"link_available")); row.push_back(jf(cl,"link_snr_db"));
    row.push_back(jf(cl,"packet_loss_probability")); row.push_back(jf(cl,"latency_ms"));
    row.push_back(jf(cl,"rf_blackout_probability")); row.push_back(jf(cl,"antenna_pointing_factor"));
    row.push_back(jf(cl,"sensor_confidence"));

    // camera
    row.push_back(jb(cam,"enabled")); row.push_back(js(cam,"sensor_type"));
    row.push_back(jf(cam,"image_noise_fraction")); row.push_back(jf(cam,"hot_pixel_fraction"));
    row.push_back(jf(cam,"dead_pixel_fraction")); row.push_back(jf(cam,"dark_current_factor"));
    row.push_back(jf(cam,"frame_corruption_probability")); row.push_back(jf(cam,"camera_health_percent"));
    row.push_back(jf(cam,"sensor_confidence")); row.push_back(jb(cam,"radiation_degraded"));
    row.push_back(jf(cam,"minimum_useful_health_percent"));

    // imaging geometry
    row.push_back(js(img,"target_name")); row.push_back(jf(img,"slant_range_to_target_m"));
    row.push_back(jf(img,"off_nadir_angle_deg")); row.push_back(jf(img,"camera_half_angle_deg"));
    row.push_back(jb(img,"horizon_visible")); row.push_back(jb(img,"within_camera_cone"));

    // attitude
    row.push_back(jf(att,"roll_deg")); row.push_back(jf(att,"pitch_deg"));
    row.push_back(jf(att,"yaw_deg")); row.push_back(jf(att,"panel_rotation_deg"));
    row.push_back(jf(att,"sensor_confidence"));

    // tcad (per-sensor confidence and safe_to_use)
    row.push_back(jnf(tcad,{"attitude_state","sensor_confidence"}));
    row.push_back(jnb(tcad,{"attitude_state","safe_to_use_for_autonomous_control"}));
    row.push_back(jnf(tcad,{"camera_sensor","sensor_confidence"}));
    row.push_back(jnb(tcad,{"camera_sensor","safe_to_use_for_autonomous_control"}));
    row.push_back(jnf(tcad,{"communication_link","sensor_confidence"}));
    row.push_back(jnb(tcad,{"communication_link","safe_to_use_for_autonomous_control"}));
    row.push_back(jnf(tcad,{"solar_panel_system","sensor_confidence"}));
    row.push_back(jnb(tcad,{"solar_panel_system","safe_to_use_for_autonomous_control"}));
    row.push_back(jnf(tcad,{"onboard_processor","sensor_confidence"}));
    row.push_back(jnb(tcad,{"onboard_processor","safe_to_use_for_autonomous_control"}));

    // sunlight
    row.push_back(jb(sun,"in_sunlight")); row.push_back(jb(sun,"in_eclipse"));
    row.push_back(jf(sun,"sun_exposure_factor"));

    // selection / maneuver
    row.push_back(jb(d,"selected_for_data")); row.push_back(jb(d,"can_maneuver"));
}

void append_debris_extra(const json& raw, Row& row)
{
    const json& d = raw.contains("data") && raw["data"].is_object()
                    ? raw["data"] : json::object();
    row.push_back(ji(d,"norad_cat_id"));
    row.push_back(js(d,"mass_source")); row.push_back(jf(d,"mass_confidence"));
    row.push_back(jb(d,"generated_fragment_boolean"));
    row.push_back(js(d,"fragment_event_id"));
    row.push_back(ji(d,"fragment_sequence_number"));
    // parent_object_ids is an array — serialise as JSON string.
    if (d.contains("parent_object_ids") && d["parent_object_ids"].is_array())
        row.push_back(Cell::make_str(d["parent_object_ids"].dump()));
    else
        row.push_back(Cell::make_nil());
    row.push_back(ji(d,"created_at_frame"));
    row.push_back(ji(d,"created_at_physical_time_ms"));
}

void append_asteroid_extra(const json& raw, Row& row)
{
    const json& d = raw.contains("data") && raw["data"].is_object()
                    ? raw["data"] : json::object();
    row.push_back(js(d,"name")); row.push_back(js(d,"catalog_name"));
    row.push_back(ji(d,"norad_cat_id"));
    row.push_back(js(d,"mass_source")); row.push_back(jf(d,"mass_confidence"));
}

struct BuiltRow {
    std::vector<std::string> columns;
    Row                      row;
};

BuiltRow build_row(const json& raw,
                    int64_t sequence_id,
                    const std::string& session,
                    const std::string& received_utc)
{
    BuiltRow br;
    br.columns = COMMON_COLUMNS;

    const std::string bucket = raw.contains("object_bucket") && raw["object_bucket"].is_string()
                               ? raw["object_bucket"].get<std::string>() : "debris";

    append_common_row(raw, session, sequence_id, received_utc, br.row);

    if (bucket == "satellites") {
        br.columns.insert(br.columns.end(), SAT_EXTRA_COLUMNS.begin(), SAT_EXTRA_COLUMNS.end());
        append_satellite_extra(raw, br.row);
    } else if (bucket == "asteroids") {
        br.columns.insert(br.columns.end(), ASTEROID_EXTRA_COLUMNS.begin(), ASTEROID_EXTRA_COLUMNS.end());
        append_asteroid_extra(raw, br.row);
    } else {
        br.columns.insert(br.columns.end(), DEBRIS_EXTRA_COLUMNS.begin(), DEBRIS_EXTRA_COLUMNS.end());
        append_debris_extra(raw, br.row);
    }
    return br;
}

// ──────────────────────────────────────────────────────────────
// ARROW PARQUET WRITER
// ──────────────────────────────────────────────────────────────
std::shared_ptr<arrow::Array> build_arrow_array(const std::vector<Row>& rows,
                                                  std::size_t col_idx)
{
    arrow::MemoryPool* pool = arrow::default_memory_pool();
    CellType ctype = CellType::NIL;
    for (const auto& r : rows) {
        if (col_idx < r.size() && r[col_idx].t != CellType::NIL) {
            ctype = r[col_idx].t; break;
        }
    }
    if (ctype == CellType::NIL) ctype = CellType::STR;

    if (ctype == CellType::I64) {
        arrow::Int64Builder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::I64) (void)b.Append(r[col_idx].i);
            else if (col_idx < r.size() && r[col_idx].t == CellType::F64) {
                arrow::DoubleBuilder b2(pool);
                for (const auto& r2 : rows) {
                    if (col_idx < r2.size() && r2[col_idx].t == CellType::F64) (void)b2.Append(r2[col_idx].f);
                    else if (col_idx < r2.size() && r2[col_idx].t == CellType::I64) (void)b2.Append(static_cast<double>(r2[col_idx].i));
                    else (void)b2.AppendNull();
                }
                std::shared_ptr<arrow::Array> arr; (void)b2.Finish(&arr); return arr;
            } else (void)b.AppendNull();
        }
        std::shared_ptr<arrow::Array> arr; (void)b.Finish(&arr); return arr;
    }
    if (ctype == CellType::F64) {
        arrow::DoubleBuilder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::F64) (void)b.Append(r[col_idx].f);
            else if (col_idx < r.size() && r[col_idx].t == CellType::I64) (void)b.Append(static_cast<double>(r[col_idx].i));
            else (void)b.AppendNull();
        }
        std::shared_ptr<arrow::Array> arr; (void)b.Finish(&arr); return arr;
    }
    if (ctype == CellType::BOOL) {
        arrow::BooleanBuilder b(pool);
        for (const auto& r : rows) {
            if (col_idx < r.size() && r[col_idx].t == CellType::BOOL) (void)b.Append(r[col_idx].b);
            else (void)b.AppendNull();
        }
        std::shared_ptr<arrow::Array> arr; (void)b.Finish(&arr); return arr;
    }
    arrow::StringBuilder b(pool);
    for (const auto& r : rows) {
        if (col_idx < r.size() && r[col_idx].t == CellType::STR) (void)b.Append(r[col_idx].s);
        else (void)b.AppendNull();
    }
    std::shared_ptr<arrow::Array> arr; (void)b.Finish(&arr); return arr;
}

bool write_parquet(const std::string& filepath,
                    const std::vector<std::string>& columns,
                    const std::vector<Row>& rows)
{
    if (rows.empty() || columns.empty()) return true;

    std::vector<std::shared_ptr<arrow::Array>>  arrays;
    std::vector<std::shared_ptr<arrow::Field>>  fields;
    arrays.reserve(columns.size());
    fields.reserve(columns.size());

    for (std::size_t i = 0; i < columns.size(); ++i) {
        auto a = build_arrow_array(rows, i);
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

    const std::string tmp = filepath + ".tmp";
    auto r = arrow::io::FileOutputStream::Open(tmp);
    if (!r.ok()) return false;
    auto outfile = *r;

    parquet::WriterProperties::Builder props_b;
    props_b.compression(parquet::Compression::SNAPPY);
    auto props = props_b.build();

    auto status = parquet::arrow::WriteTable(
        *table, arrow::default_memory_pool(), outfile,
        static_cast<int64_t>(rows.size()), props);
    if (!status.ok()) return false;
    if (!outfile->Close().ok()) return false;

    std::error_code ec;
    fs::rename(tmp, filepath, ec);
    return !ec;
}

// ──────────────────────────────────────────────────────────────
// PER-OBJECT BUFFER
// ──────────────────────────────────────────────────────────────
struct ObjectBuffer {
    std::mutex               mtx;
    std::vector<std::string> columns;
    std::vector<Row>         rows;
    SteadyClk::time_point   last_flush     = SteadyClk::now();
    uint64_t                 batch_seq      = 1;
    uint64_t                 total_rows     = 0;
    std::string              object_id;
    std::string              object_bucket;
    int64_t                  sequence_counter = 0;

    // Column index cache for stats (resolved after first row).
    int idx_trip_ms = -1;

    // Stats
    double uplink_sum_ms = 0.0;
    int64_t uplink_n     = 0;
    double min_trip_ms   = std::numeric_limits<double>::infinity();
    double max_trip_ms   = -std::numeric_limits<double>::infinity();
    std::string oldest_utc, newest_utc;
    std::unordered_map<std::string,int64_t> ground_station_counts;
    int64_t total_link_obs = 0, direct_count = 0, tdrs_count = 0;
};

struct ObjectKey {
    std::string oid, bucket;
    bool operator==(const ObjectKey& o) const noexcept {
        return oid == o.oid && bucket == o.bucket;
    }
};
struct ObjKeyHash {
    std::size_t operator()(const ObjectKey& k) const noexcept {
        return std::hash<std::string>{}(k.oid) ^ (std::hash<std::string>{}(k.bucket) << 1);
    }
};

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────

// Active session paths (discovered at startup / updated on rescan).
static std::string g_session_name;
static std::string g_session_json;
static std::string g_session_parquet;
static std::mutex  g_session_mutex;

std::unordered_map<ObjectKey, std::unique_ptr<ObjectBuffer>, ObjKeyHash> g_objects;
std::shared_mutex g_objects_mutex;

std::queue<std::string>  g_work_queue;
std::mutex               g_work_queue_mtx;
std::condition_variable  g_work_queue_cv;

struct FlushRequest { ObjectKey key; };
std::queue<FlushRequest> g_flush_queue;
std::mutex               g_flush_queue_mtx;
std::condition_variable  g_flush_queue_cv;
std::unordered_set<std::string> g_flush_inflight;
std::mutex                      g_flush_inflight_mtx;

std::atomic<bool> g_index_dirty{false};
std::condition_variable g_index_cv;
std::mutex              g_index_mtx;

// json_index rows accumulated in memory; written as Parquet periodically.
struct JsonIndexRow {
    std::string session, bucket, object_id, filename, received_iso;
    double      received_ms{0.0};
    int64_t     size_bytes{0};
};
std::vector<JsonIndexRow> g_json_index_rows;
std::mutex                g_json_index_mutex;

std::atomic<bool>     g_running{true};
std::atomic<uint64_t> g_stat_files_seen{0};
std::atomic<uint64_t> g_stat_files_parsed{0};
std::atomic<uint64_t> g_stat_files_failed{0};
std::atomic<uint64_t> g_stat_rows{0};
std::atomic<uint64_t> g_stat_batches{0};
std::atomic<uint64_t> g_stat_parquet_errors{0};

// ──────────────────────────────────────────────────────────────
// SESSION DISCOVERY
// Scans ROOT_DIR for the most recently modified session subfolder.
// Called at startup and periodically to detect GSM manager restarts.
// ──────────────────────────────────────────────────────────────
bool discover_session()
{
    std::string best_name;
    fs::file_time_type best_time{};
    try {
        for (const auto& entry : fs::directory_iterator(ROOT_DIR)) {
            if (!entry.is_directory()) continue;
            const std::string name = entry.path().filename().string();
            // Session folders look like 2026-05-05T12-43-05.
            if (name.size() < 16) continue;
            auto mtime = entry.last_write_time();
            if (best_name.empty() || mtime > best_time) {
                best_time = mtime;
                best_name = name;
            }
        }
    } catch (...) { return false; }
    if (best_name.empty()) return false;

    std::lock_guard<std::mutex> lk(g_session_mutex);
    if (best_name == g_session_name) return false; // no change

    g_session_name    = best_name;
    g_session_json    = ROOT_DIR + "/" + best_name + "/json";
    g_session_parquet = ROOT_DIR + "/" + best_name + "/parquet";
    std::cout << "[session] active session: " << g_session_name << "\n";
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
        seq = buf->batch_seq++;

        std::string sess_parquet;
        { std::lock_guard<std::mutex> sl(g_session_mutex); sess_parquet = g_session_parquet; }
        obj_dir = (fs::path(sess_parquet) / buf->object_bucket
                   / buf->object_id).string();
    }

    fs::create_directories(obj_dir);
    char fname[64];
    std::snprintf(fname, sizeof(fname), "batch_%06llu.parquet",
                  static_cast<unsigned long long>(seq));
    const std::string filepath = obj_dir + "/" + fname;

    if (write_parquet(filepath, columns, rows)) {
        ++g_stat_batches;
        std::cout << "[flush] " << buf->object_bucket << "/" << buf->object_id
                  << " → " << fname << " (" << rows.size() << " rows)\n";
    } else {
        ++g_stat_parquet_errors;
        std::lock_guard<std::mutex> lk(buf->mtx);
        buf->rows.insert(buf->rows.end(),
                          std::make_move_iterator(rows.begin()),
                          std::make_move_iterator(rows.end()));
    }

    g_index_dirty.store(true);
    g_index_cv.notify_one();
}

// ──────────────────────────────────────────────────────────────
// INGEST ONE FILE
// ──────────────────────────────────────────────────────────────
void ingest_one_file(const std::string& path)
{
    ++g_stat_files_seen;
    std::ifstream f(path);
    if (!f.is_open()) { ++g_stat_files_failed; return; }
    std::ostringstream ss; ss << f.rdbuf(); f.close();
    const std::string raw_str = ss.str();

    json raw;
    try { raw = json::parse(raw_str); }
    catch (...) { ++g_stat_files_failed; return; }

    std::string oid;
    if (raw.contains("object_id") && raw["object_id"].is_string())
        oid = raw["object_id"].get<std::string>();
    if (oid.empty()) { ++g_stat_files_failed; return; }

    std::string bucket = "debris";
    if (raw.contains("object_bucket") && raw["object_bucket"].is_string())
        bucket = raw["object_bucket"].get<std::string>();

    std::string sess_name;
    { std::lock_guard<std::mutex> lk(g_session_mutex); sess_name = g_session_name; }

    ObjectKey key{oid, bucket};

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
            p->object_id     = oid;
            p->object_bucket = bucket;
            buf = p.get();
            g_objects.emplace(key, std::move(p));
        } else buf = it->second.get();
    }

    const std::string received_utc = utc_now_iso();
    bool needs_flush = false;

    {
        std::lock_guard<std::mutex> lk(buf->mtx);
        const int64_t seq_id = ++buf->sequence_counter;
        BuiltRow br = build_row(raw, seq_id, sess_name, received_utc);
        if (buf->columns.empty()) {
            buf->columns = br.columns;
            // Cache column indices for stats.
            for (std::size_t i = 0; i < buf->columns.size(); ++i)
                if (buf->columns[i] == "sms_to_gsm_trip_ms") {
                    buf->idx_trip_ms = static_cast<int>(i); break;
                }
        }

        ++buf->total_rows;
        if (buf->oldest_utc.empty()) buf->oldest_utc = received_utc;
        buf->newest_utc = received_utc;

        // Trip time stats
        if (buf->idx_trip_ms >= 0 &&
            static_cast<std::size_t>(buf->idx_trip_ms) < br.row.size()) {
            const auto& c = br.row[buf->idx_trip_ms];
            double v = 0.0;
            if (c.t == CellType::F64) v = c.f;
            else if (c.t == CellType::I64) v = static_cast<double>(c.i);
            else goto skip_trip;
            buf->uplink_sum_ms += v; ++buf->uplink_n;
            if (v < buf->min_trip_ms) buf->min_trip_ms = v;
            if (v > buf->max_trip_ms) buf->max_trip_ms = v;
        }
        skip_trip:;

        buf->rows.push_back(std::move(br.row));
        ++g_stat_rows;

        const double elapsed = std::chrono::duration<double>(
            SteadyClk::now() - buf->last_flush).count();
        if (buf->rows.size() >= BATCH_SIZE || elapsed >= FLUSH_INTERVAL_S)
            needs_flush = true;
    }

    // Update json_index
    {
        const fs::path fp(path);
        JsonIndexRow jr;
        jr.session      = sess_name;
        jr.bucket       = bucket;
        jr.object_id    = oid;
        jr.filename     = fp.filename().string();
        jr.received_iso = received_utc;
        try {
            jr.size_bytes = static_cast<int64_t>(fs::file_size(fp));
        } catch (...) {}
        std::lock_guard<std::mutex> lk(g_json_index_mutex);
        g_json_index_rows.push_back(std::move(jr));
    }

    if (needs_flush) {
        const std::string inf_key = key.oid + "|" + key.bucket;
        bool enq = false;
        {
            std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
            if (!g_flush_inflight.count(inf_key)) {
                g_flush_inflight.insert(inf_key);
                enq = true;
            }
        }
        if (enq) {
            std::lock_guard<std::mutex> lk(g_flush_queue_mtx);
            g_flush_queue.push(FlushRequest{key});
            g_flush_queue_cv.notify_one();
        }
    }

    ++g_stat_files_parsed;
}

// ──────────────────────────────────────────────────────────────
// MASTER INDEX + SUMMARY + JSON INDEX  (rebuild after flushes)
// ──────────────────────────────────────────────────────────────
void rebuild_indexes()
{
    std::string sess_parquet, sess_name, sess_dir;
    {
        std::lock_guard<std::mutex> lk(g_session_mutex);
        sess_parquet = g_session_parquet;
        sess_name    = g_session_name;
        sess_dir     = ROOT_DIR + "/" + sess_name;
    }
    if (sess_parquet.empty()) return;

    // Snapshot all object stats.
    struct ObjSnap {
        std::string oid, bucket, oldest_utc, newest_utc;
        uint64_t total_rows{0}, total_batches{0};
        double avg_trip_ms{0.0}, min_trip_ms{0.0}, max_trip_ms{0.0};
    };
    std::vector<ObjSnap> snaps;
    {
        std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
        for (auto& [k, buf] : g_objects) {
            std::lock_guard<std::mutex> blk(buf->mtx);
            ObjSnap s;
            s.oid          = buf->object_id;
            s.bucket       = buf->object_bucket;
            s.oldest_utc   = buf->oldest_utc;
            s.newest_utc   = buf->newest_utc;
            s.total_rows   = buf->total_rows;
            s.total_batches = buf->batch_seq - 1;
            s.avg_trip_ms  = buf->uplink_n > 0 ? buf->uplink_sum_ms / buf->uplink_n : 0.0;
            s.min_trip_ms  = buf->uplink_n > 0 ? buf->min_trip_ms : 0.0;
            s.max_trip_ms  = buf->uplink_n > 0 ? buf->max_trip_ms : 0.0;
            snaps.push_back(std::move(s));
        }
    }

    // Group by bucket.
    std::unordered_map<std::string, std::vector<const ObjSnap*>> by_bucket;
    for (const auto& s : snaps) by_bucket[s.bucket].push_back(&s);

    static const std::vector<std::string> IDX_COLS = {
        "object_id", "object_bucket", "total_records", "total_batches",
        "oldest_record_utc", "newest_record_utc",
        "avg_trip_ms", "min_trip_ms", "max_trip_ms",
        "session", "last_updated_utc",
    };
    const std::string now_iso = utc_now_iso();

    for (const auto& [bucket, items] : by_bucket) {
        if (items.empty()) continue;
        std::vector<Row> rows;
        rows.reserve(items.size());
        for (const auto* s : items) {
            Row r;
            r.push_back(Cell::make_str(s->oid));
            r.push_back(Cell::make_str(s->bucket));
            r.push_back(Cell::make_i64(static_cast<int64_t>(s->total_rows)));
            r.push_back(Cell::make_i64(static_cast<int64_t>(s->total_batches)));
            r.push_back(s->oldest_utc.empty() ? Cell::make_nil() : Cell::make_str(s->oldest_utc));
            r.push_back(s->newest_utc.empty() ? Cell::make_nil() : Cell::make_str(s->newest_utc));
            r.push_back(Cell::make_f64(s->avg_trip_ms));
            r.push_back(Cell::make_f64(s->min_trip_ms));
            r.push_back(Cell::make_f64(s->max_trip_ms));
            r.push_back(Cell::make_str(sess_name));
            r.push_back(Cell::make_str(now_iso));
            rows.push_back(std::move(r));
        }
        const std::string bdir = (fs::path(sess_parquet) / bucket).string();
        fs::create_directories(bdir);
        write_parquet(bdir + "/master_index.parquet", IDX_COLS, rows);
    }

    // Summary parquet (one row per bucket).
    static const std::vector<std::string> SUM_COLS = {
        "bucket", "object_count", "total_records", "total_batches",
        "session", "last_updated_utc",
    };
    std::vector<Row> srows;
    for (const auto& [bucket, items] : by_bucket) {
        uint64_t tr = 0, tb = 0;
        for (const auto* s : items) { tr += s->total_rows; tb += s->total_batches; }
        Row r;
        r.push_back(Cell::make_str(bucket));
        r.push_back(Cell::make_i64(static_cast<int64_t>(items.size())));
        r.push_back(Cell::make_i64(static_cast<int64_t>(tr)));
        r.push_back(Cell::make_i64(static_cast<int64_t>(tb)));
        r.push_back(Cell::make_str(sess_name));
        r.push_back(Cell::make_str(now_iso));
        srows.push_back(std::move(r));
    }
    if (!srows.empty()) {
        fs::create_directories(sess_parquet);
        write_parquet(sess_parquet + "/summary.parquet", SUM_COLS, srows);
    }

    // json_index.parquet — all JSON files received this session.
    std::vector<JsonIndexRow> idx_rows;
    {
        std::lock_guard<std::mutex> lk(g_json_index_mutex);
        idx_rows = g_json_index_rows;
    }
    if (!idx_rows.empty()) {
        static const std::vector<std::string> JIDX_COLS = {
            "session", "bucket", "object_id", "filename",
            "received_utc", "size_bytes",
        };
        std::vector<Row> jrows;
        jrows.reserve(idx_rows.size());
        for (const auto& jr : idx_rows) {
            Row r;
            r.push_back(Cell::make_str(jr.session));
            r.push_back(Cell::make_str(jr.bucket));
            r.push_back(Cell::make_str(jr.object_id));
            r.push_back(Cell::make_str(jr.filename));
            r.push_back(Cell::make_str(jr.received_iso));
            r.push_back(Cell::make_i64(jr.size_bytes));
            jrows.push_back(std::move(r));
        }
        fs::create_directories(sess_dir);
        write_parquet(sess_dir + "/json_index.parquet", JIDX_COLS, jrows);
    }
}

// ──────────────────────────────────────────────────────────────
// THREADS
// ──────────────────────────────────────────────────────────────

void watcher_thread_fn()
{
    std::unordered_set<std::string> seen;
    int session_scan_countdown = 0;

    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(WATCHER_POLL_MS));
        if (!g_running.load()) break;

        // Periodically rescan for new session folders.
        if (++session_scan_countdown >= (SESSION_SCAN_INTERVAL_S * 1000 / WATCHER_POLL_MS)) {
            session_scan_countdown = 0;
            discover_session();
        }

        std::string sess_json;
        { std::lock_guard<std::mutex> lk(g_session_mutex); sess_json = g_session_json; }
        if (sess_json.empty()) continue;

        std::vector<std::string> new_files;
        try {
            for (const auto& bucket_entry : fs::directory_iterator(sess_json)) {
                if (!bucket_entry.is_directory()) continue;
                for (const auto& obj_entry : fs::directory_iterator(bucket_entry)) {
                    if (!obj_entry.is_directory()) continue;
                    for (const auto& f : fs::directory_iterator(obj_entry)) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().extension() != ".json") continue;
                        const std::string s = f.path().string();
                        if (s.find(".tmp") != std::string::npos) continue;
                        if (seen.count(s)) continue;
                        new_files.push_back(s);
                    }
                }
            }
        } catch (...) { continue; }
        if (new_files.empty()) continue;

        std::sort(new_files.begin(), new_files.end());
        {
            std::lock_guard<std::mutex> lk(g_work_queue_mtx);
            for (auto& nf : new_files) {
                if (g_work_queue.size() >= WORK_QUEUE_MAX) g_work_queue.pop();
                g_work_queue.push(nf);
            }
            g_work_queue_cv.notify_all();
        }
        for (auto& nf : new_files) seen.insert(nf);

        if (seen.size() > 500'000) {
            std::unordered_set<std::string> on_disk;
            try {
                for (const auto& be : fs::directory_iterator(sess_json)) {
                    if (!be.is_directory()) continue;
                    for (const auto& oe : fs::directory_iterator(be)) {
                        if (!oe.is_directory()) continue;
                        for (const auto& fe : fs::directory_iterator(oe))
                            if (fe.is_regular_file()) on_disk.insert(fe.path().string());
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
            g_work_queue_cv.wait(lk, []{ return !g_work_queue.empty() || !g_running.load(); });
            if (!g_running.load()) return;
            path = std::move(g_work_queue.front());
            g_work_queue.pop();
        }
        try { ingest_one_file(path); }
        catch (const std::exception& e) {
            ++g_stat_files_failed;
            std::cerr << "[ingest] " << path << ": " << e.what() << "\n";
        }
    }
}

void flush_thread_fn()
{
    while (g_running.load()) {
        FlushRequest req;
        {
            std::unique_lock<std::mutex> lk(g_flush_queue_mtx);
            g_flush_queue_cv.wait(lk, []{ return !g_flush_queue.empty() || !g_running.load(); });
            if (!g_running.load()) return;
            req = g_flush_queue.front();
            g_flush_queue.pop();
        }
        try { perform_flush(req.key); }
        catch (const std::exception& e) {
            ++g_stat_parquet_errors;
            std::cerr << "[flush] " << e.what() << "\n";
        }
        {
            std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
            g_flush_inflight.erase(req.key.oid + "|" + req.key.bucket);
        }
    }
}

void interval_flush_thread_fn()
{
    while (g_running.load()) {
        std::this_thread::sleep_for(
            std::chrono::seconds(static_cast<int>(FLUSH_INTERVAL_S / 2)));
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
            const std::string inf_key = key.oid + "|" + key.bucket;
            bool enq = false;
            { std::lock_guard<std::mutex> lk(g_flush_inflight_mtx);
              if (!g_flush_inflight.count(inf_key)) { g_flush_inflight.insert(inf_key); enq = true; } }
            if (enq) {
                std::lock_guard<std::mutex> lk(g_flush_queue_mtx);
                g_flush_queue.push(FlushRequest{key});
                g_flush_queue_cv.notify_one();
            }
        }
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
            try { rebuild_indexes(); }
            catch (const std::exception& e) {
                std::cerr << "[index] " << e.what() << "\n";
            }
        }
    }
}

void stats_thread_fn()
{
    uint64_t last_parsed = 0;
    while (g_running.load()) {
        for (int i = 0; i < STATS_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const uint64_t parsed = g_stat_files_parsed.load();
        const double   rate   = static_cast<double>(parsed - last_parsed) / STATS_INTERVAL_S;
        last_parsed = parsed;

        std::size_t wq, fq, n_obj;
        { std::lock_guard<std::mutex> lk(g_work_queue_mtx);  wq = g_work_queue.size(); }
        { std::lock_guard<std::mutex> lk(g_flush_queue_mtx); fq = g_flush_queue.size(); }
        { std::shared_lock<std::shared_mutex> lk(g_objects_mutex); n_obj = g_objects.size(); }

        std::cout << "[stats] seen=" << g_stat_files_seen.load()
                  << " parsed=" << parsed << " (" << static_cast<int>(rate) << "/s)"
                  << " failed=" << g_stat_files_failed.load()
                  << " rows=" << g_stat_rows.load()
                  << " batches=" << g_stat_batches.load()
                  << " objects=" << n_obj
                  << " wq=" << wq << " fq=" << fq
                  << " errs=" << g_stat_parquet_errors.load() << "\n";
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────
void install_signal_handlers()
{
    auto h = [](int){
        g_running.store(false);
        g_work_queue_cv.notify_all();
        g_flush_queue_cv.notify_all();
        g_index_cv.notify_all();
    };
    std::signal(SIGINT,  h);
    std::signal(SIGTERM, h);
}

// ──────────────────────────────────────────────────────────────
// MAIN
// ──────────────────────────────────────────────────────────────
int main()
{
    std::cout << "================================================\n"
              << " ground_station_processor.cpp v2  —  GSM Processor\n"
              << "================================================\n"
              << "[main] root:    " << ROOT_DIR << "\n"
              << "[main] batch:   " << BATCH_SIZE << " rows\n"
              << "[main] flush:   " << FLUSH_INTERVAL_S << " s\n"
              << "[main] ingest:  " << INGEST_THREADS << " threads\n"
              << "[main] parquet: permanent (never deleted)\n"
              << "[main] buckets: satellites/ asteroids/ debris/\n"
              << "================================================\n";

    fs::create_directories(ROOT_DIR);
    install_signal_handlers();

    // Wait for a session folder to appear.
    std::cout << "[main] waiting for session folder in " << ROOT_DIR << " ...\n";
    while (g_running.load() && !discover_session()) {
        std::this_thread::sleep_for(std::chrono::seconds(1));
    }
    if (!g_running.load()) return 0;

    std::thread t_watcher (watcher_thread_fn);
    std::vector<std::thread> ingest_threads;
    for (unsigned i = 0; i < INGEST_THREADS; ++i)
        ingest_threads.emplace_back(ingest_thread_fn);
    std::thread t_flush    (flush_thread_fn);
    std::thread t_interval (interval_flush_thread_fn);
    std::thread t_index    (index_thread_fn);
    std::thread t_stats    (stats_thread_fn);

    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutdown — draining ...\n";
    g_work_queue_cv.notify_all();
    g_flush_queue_cv.notify_all();
    g_index_cv.notify_all();

    t_watcher.join();
    for (auto& t : ingest_threads) t.join();
    t_flush.join();
    t_interval.join();
    t_index.join();
    t_stats.join();

    // Final flush of all remaining rows.
    std::cout << "[main] final flush ...\n";
    {
        std::vector<ObjectKey> all_keys;
        { std::shared_lock<std::shared_mutex> lk(g_objects_mutex);
          for (auto& [k,_] : g_objects) all_keys.push_back(k); }
        for (auto& k : all_keys) try { perform_flush(k); } catch (...) {}
    }
    try { rebuild_indexes(); } catch (...) {}

    std::cout << "[main] clean exit."
              << " parsed=" << g_stat_files_parsed
              << " rows=" << g_stat_rows
              << " batches=" << g_stat_batches
              << " errors=" << g_stat_parquet_errors << "\n";
    return 0;
}
