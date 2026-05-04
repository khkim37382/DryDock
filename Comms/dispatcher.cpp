// ============================================================
// dispatcher.cpp  —  v2  (priority-queue event loop)
// ============================================================
// Reads combined telemetry JSON files from ready_to_send_telemetry/
// produced by satellite_orbit.py at 10 Hz.
//
// ARCHITECTURE (this is the v2 rewrite, fixing v1's thread explosion):
// ───────────────────────────────────────────────────────────────────
// v1 spawned one std::thread per object per snapshot.  At 1000 objects
// × 10 Hz × 250 ms typical TDRS delay you would accumulate ~10,000
// concurrent sleeping threads — this collapses on macOS (each thread
// has a 512 KB default stack = 5 GB just for stacks).
//
// v2 uses four long-lived threads + a min-heap priority queue:
//
//   ┌──────────────────────┐
//   │ producer_thread      │  watches ready_to_send_telemetry/,
//   │                      │  parses each new combined JSON,
//   │                      │  for every object splits a slice,
//   │                      │  computes LOS + propagation delay,
//   │                      │  enqueues a DispatchEvent into the
//   │                      │  min-heap (sorted by release_time).
//   └──────────┬───────────┘
//              │ event_queue (std::priority_queue, mutex+cv)
//              ▼
//   ┌──────────────────────┐
//   │ scheduler_thread     │  peeks at top of heap, sleeps until
//   │                      │  release_time, pops, hands payload
//   │                      │  to publish_thread via channel.
//   └──────────┬───────────┘
//              │ publish_channel (std::queue, mutex+cv)
//              ▼
//   ┌──────────────────────┐
//   │ publish_thread       │  pulls payloads, stamps
//   │                      │  sms_send_time_ns AT moment of publish,
//   │                      │  calls g_mqtt->publish().  Paho is
//   │                      │  serialised through this single
//   │                      │  thread so its async client is safe.
//   └──────────────────────┘
//
//   ┌──────────────────────┐
//   │ cleanup_thread       │  every CLEANUP_INTERVAL_S deletes
//   │                      │  snapshot files older than 5 hours.
//   └──────────────────────┘
//
// This design handles 100,000+ objects with constant memory:  the heap
// is the only growing structure, individual events are ~1 KB each.
//
// FIELD-NAME ALIGNMENT WITH satellite_orbit.py  (drydock dataset-backed v1)
// ────────────────────────────────────────────────────────────────────────
// Each object_state_dict in the sim's JSON has the following structure
// (because object_state_dict() does state.update(extra) — fields are
//  flattened into the top level of each object dict):
//
//   {
//     "id":                  "NORAD-20580" | "SAT-1" | etc.   // dataset-backed IDs
//     "type":                "satellite" | "debris" | "asteroid"
//                            | "generated_debris_fragment"    // <- spawned dynamically
//                            | "debris_debris_fragmentation"  //    by collisions
//                            | "satellite_or_object_breakup",
//     "active":              true,
//     "position_m_eci":      {"x": ..., "y": ..., "z": ...},
//     "velocity_mps_eci":    {"x": ..., "y": ..., "z": ...},
//     "speed_mps":           ...,
//     "altitude_m":          ...,
//     "altitude_km":         ...,
//     "orbit_class":         "LEO" | "MEO" | "HEO" | "unknown",
//     "orbit_description":   "...",
//     "name":                "Catalog name string",
//     "catalog_name":        "Same as name (alias)",
//     "json_object_id":      "NORAD-20580",
//     "norad_cat_id":        20580,
//     "mass_kg":             ...,
//     "mass_source":         "...",
//     "mass_confidence":     ...,
//     "physical_radius_m":   ...,
//     "orbit_elements":      { mean_motion_rev_per_day, eccentricity, ... },
//     "raw_celestrak_gp":    { OBJECT_NAME, EPOCH, MEAN_MOTION, ... },
//     "ucs_metadata":        { country_of_operator_owner, purpose, ... },
//     "environment_vectors": { ... },
//     "sunlight_state":      { ... },
//     "attitude_state":      { ... },
//     "solar_panel_system":  { ... },
//     "voltage_sensors":     { ... },
//     "power_system":        { ... },
//     "thermal_profile":     { ... },
//     "communication_link":  { ... },
//     "radiation":           { ... },
//     "sensor_fusion_state": { ... },
//   }
//
// Top-level snapshot keys:  "satellites" / "asteroids" / "debris".
//   NOTE: "asteroids[]" may now contain DEBRIS records (the dataset-backed
//   schema repurposes the legacy array name).  We trust each item's own
//   "type" field rather than the parent array name to decide its bucket.
//   All debris-family sub-types collapse to a single "debris" bucket.
//
// ROUNDTRIP HONESTY
// ─────────────────
// The dispatcher only stamps sms_send_time_ns and appends comm-path
// metadata.  It does not compute or label any "roundtrip" — the
// GSM-side processor uses honest names like "current_uplink_trip_ns"
// and "last_known_downlink_trip_ns".
//
// macOS BUILD INSTRUCTIONS  (Apple Silicon and Intel)
// ───────────────────────────────────────────────────
//   # Install dependencies
//   brew install eclipse-paho-mqtt-cpp nlohmann-json
//
//   # Build (auto-detects Homebrew prefix)
//   ./build_dispatcher.sh
//
//   # Or manually:
//   BREW_PREFIX="$(brew --prefix)"
//   clang++ -std=c++20 -O2 -pthread \
//       -I"${BREW_PREFIX}/include" \
//       -L"${BREW_PREFIX}/lib" \
//       dispatcher.cpp \
//       -lpaho-mqttpp3 -lpaho-mqtt3a \
//       -o dispatcher
//
// Run:
//   ./dispatcher
// ============================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <unordered_map>
#include <set>
#include <queue>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <atomic>
#include <chrono>
#include <cmath>
#include <random>
#include <filesystem>
#include <algorithm>
#include <stdexcept>
#include <csignal>
#include <limits>

#include <nlohmann/json.hpp>
#include <mqtt/async_client.h>

namespace fs = std::filesystem;
using json        = nlohmann::json;
using SteadyClock = std::chrono::steady_clock;
using SystemClock = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static const std::string INPUT_DIR              = "ready_to_send_telemetry";
static const std::string GROUND_NETWORK_CONFIG  = "ground_network_config.json";
static const std::string BROKER_IP              = "tcp://localhost:1883";
static const std::string CLIENT_ID              = "dispatcher_cpp_v2";
// Topic structure: satellite/telemetry/{object_id}
//   The dispatcher publishes each object on its OWN topic so that:
//     * MQTT broker routes per-topic (better when many small messages
//       go to many subscribers vs. one fat firehose topic).
//     * GSM can wildcard-subscribe satellite/telemetry/+ and let the
//       broker hand it the object_id via msg.topic.
//     * Future per-object QoS/retention policies are trivial.
static const std::string TOPIC_PREFIX           = "satellite/telemetry";
static const int         MQTT_QOS               = 1;

static const int    POLL_INTERVAL_MS            = 20;
static const double QUEUE_MAX_AGE_SECONDS       = 18000.0;     // 5 h
static const int    CLEANUP_INTERVAL_S          = 60;
static const double DELAY_NOISE_STD_MS          = 1.5;

static const double SPEED_OF_LIGHT_MPS          = 299792458.0;
static const double R_EARTH_M                   = 6371008.4;
static const double GEO_ALT_M                   = 35786000.0;
static const double EARTH_ROT_RATE              = 7.2921159e-5;

// Per-orbit-class throttle (seconds).
static const double THROTTLE_LEO_S              = 0.10;   // 10 Hz
static const double THROTTLE_MEO_S              = 0.20;   //  5 Hz
// HEO throttle is computed dynamically per altitude (see throttle_for).
static const double THROTTLE_AST_S              = 1.00;
static const double THROTTLE_DEBRIS_S           = 2.00;
static const double THROTTLE_DEFAULT_S          = 0.10;

// White Sands NM (TDRS ground terminal location, Earth-fixed lat/lon).
static const double WS_LAT_DEG = 32.5;
static const double WS_LON_DEG = -106.6;

// Bound the in-memory event queue so a runaway producer can't OOM us.
// At 1000 objects × 10 Hz × 5 minutes = 3 million events. We cap at 5 M.
static const std::size_t MAX_EVENT_QUEUE_SIZE   = 5'000'000;

// ──────────────────────────────────────────────────────────────
// VECTOR MATH
// ──────────────────────────────────────────────────────────────
struct Vec3 {
    double x{0}, y{0}, z{0};
    constexpr Vec3() = default;
    constexpr Vec3(double xi, double yi, double zi) : x(xi), y(yi), z(zi) {}
    Vec3 operator+(const Vec3& o) const { return {x+o.x, y+o.y, z+o.z}; }
    Vec3 operator-(const Vec3& o) const { return {x-o.x, y-o.y, z-o.z}; }
    Vec3 operator*(double s)      const { return {x*s,   y*s,   z*s};   }
    double dot(const Vec3& o)     const { return x*o.x + y*o.y + z*o.z; }
    double mag()                  const { return std::sqrt(x*x + y*y + z*z); }
};

// ──────────────────────────────────────────────────────────────
// COORDINATE CONVERSION
// ──────────────────────────────────────────────────────────────
//
// The sim's frame:
//   - z-axis = Earth's rotational axis (north pole)
//   - x-axis = vernal-equinox-like body-frame reference at t=0
//   - Earth at origin (no heliocentric translation in object positions)
//   - Object position_m_eci values are Earth-centred quasi-inertial
//
// Ground stations / TDRS are defined in lat/lon on Earth's rotating
// body frame.  To compute their ECI positions at simulation
// physical_time_s we add the cumulative Earth rotation to longitude.
// This matches update_ground_station_visuals() in the sim.
// ──────────────────────────────────────────────────────────────

inline Vec3 latlon_to_eci(double lat_deg, double lon_deg,
                          double physical_time_s, double altitude_m = 0.0)
{
    const double lat       = lat_deg * M_PI / 180.0;
    const double lon_inert = lon_deg * M_PI / 180.0
                             + EARTH_ROT_RATE * physical_time_s;
    const double r = R_EARTH_M + altitude_m;
    return Vec3{
        r * std::cos(lat) * std::cos(lon_inert),
        r * std::cos(lat) * std::sin(lon_inert),
        r * std::sin(lat)
    };
}

inline Vec3 geo_lon_to_eci(double lon_deg, double physical_time_s)
{
    return latlon_to_eci(0.0, lon_deg, physical_time_s, GEO_ALT_M);
}

// ──────────────────────────────────────────────────────────────
// LINE-OF-SIGHT (correct ray-segment vs Earth-sphere intersection)
// ──────────────────────────────────────────────────────────────
//
// Parametrise the segment: p(s) = a + s*(b-a),  s in [0, 1].
// We need |p(s)| > R_EARTH for ALL s in [0, 1] — otherwise LOS is
// blocked.  Solve |p(s)|^2 = R_EARTH^2:
//
//   A s^2 + B s + C = 0
//     A = (b-a)·(b-a)
//     B = 2 a·(b-a)
//     C = a·a - R^2
//
// Both endpoints are always outside Earth in our use case (sats &
// surface stations), so the segment is blocked iff a real root lies
// strictly inside (0, 1).
// ──────────────────────────────────────────────────────────────

inline bool has_los(const Vec3& a, const Vec3& b)
{
    const Vec3   d   = b - a;
    const double A   = d.dot(d);
    if (A < 1e-9) return true;                 // a == b
    const double B   = 2.0 * a.dot(d);
    const double C   = a.dot(a) - R_EARTH_M * R_EARTH_M;
    const double disc = B*B - 4.0*A*C;
    if (disc < 0.0) return true;               // ray never crosses Earth
    const double sq = std::sqrt(disc);
    const double s1 = (-B - sq) / (2.0 * A);
    const double s2 = (-B + sq) / (2.0 * A);
    constexpr double EPS = 1e-6;
    if (s1 > EPS && s1 < 1.0 - EPS) return false;
    if (s2 > EPS && s2 < 1.0 - EPS) return false;
    return true;
}

// ──────────────────────────────────────────────────────────────
// DATA STRUCTS
// ──────────────────────────────────────────────────────────────
struct GroundStation { std::string name; double lat_deg{0}, lon_deg{0}; };
struct TDRSRelay     { std::string name; double lon_deg{0};            };

enum class LinkType { DIRECT, TDRS, BLACKOUT };

struct CommPath {
    LinkType    type{LinkType::BLACKOUT};
    std::string ground_station;
    std::string relay_name;
    double      propagation_delay_ms{0.0};
    double      distance_m{0.0};
};

struct DispatchEvent {
    SteadyClock::time_point release_time;
    json                    payload;
    std::string             object_id;
    CommPath                comm_path;
};

struct DispatchEventGreater {
    bool operator()(const DispatchEvent& l, const DispatchEvent& r) const {
        return l.release_time > r.release_time;
    }
};

using EventHeap = std::priority_queue<
    DispatchEvent, std::vector<DispatchEvent>, DispatchEventGreater
>;

struct PublishItem { json payload; std::string object_id; };

struct BlackoutSlot {
    json                    payload;
    SteadyClock::time_point first_queued_at;
    double                  physical_time_s{0.0};
};

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────
std::vector<GroundStation> g_ground_stations;
std::vector<TDRSRelay>     g_tdrs_relays;

std::unordered_map<std::string, SteadyClock::time_point> g_last_send;
std::mutex g_last_send_mutex;

std::unordered_map<std::string, BlackoutSlot> g_blackout;
std::mutex g_blackout_mutex;

EventHeap                g_events;
std::mutex               g_events_mutex;
std::condition_variable  g_events_cv;

std::queue<PublishItem>  g_publish_queue;
std::mutex               g_publish_queue_mutex;
std::condition_variable  g_publish_queue_cv;

std::unique_ptr<mqtt::async_client> g_mqtt;

std::default_random_engine       g_rng(std::random_device{}());
std::normal_distribution<double> g_noise(0.0, DELAY_NOISE_STD_MS);
std::mutex                       g_noise_mutex;

std::atomic<bool> g_running{true};

std::atomic<uint64_t> g_stat_snapshots_processed{0};
std::atomic<uint64_t> g_stat_events_enqueued{0};
std::atomic<uint64_t> g_stat_events_published{0};
std::atomic<uint64_t> g_stat_blackouts{0};
std::atomic<uint64_t> g_stat_throttled{0};
std::atomic<uint64_t> g_stat_publish_errors{0};

// ──────────────────────────────────────────────────────────────
// HELPERS
// ──────────────────────────────────────────────────────────────
inline int64_t wall_time_ns()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        SystemClock::now().time_since_epoch()).count();
}

inline double now_seconds()
{
    return std::chrono::duration<double>(
        SystemClock::now().time_since_epoch()).count();
}

inline double gauss_noise_ms()
{
    std::lock_guard<std::mutex> lk(g_noise_mutex);
    return g_noise(g_rng);
}

bool read_file(const std::string& path, std::string& out)
{
    std::ifstream f(path);
    if (!f.is_open()) return false;
    std::ostringstream ss; ss << f.rdbuf();
    out = ss.str();
    return true;
}

double file_mtime_seconds(const fs::path& p)
{
    using namespace std::chrono;
    auto ftime = fs::last_write_time(p);
    auto sctp  = time_point_cast<seconds>(fs::file_clock::to_sys(ftime));
    return static_cast<double>(sctp.time_since_epoch().count());
}

double throttle_for(const std::string& obj_type,
                    const std::string& orbit_class,
                    double altitude_m)
{
    // Map dataset-backed sub-types to their canonical bucket.
    //   "generated_debris_fragment" - fragments spawned by collisions
    //   "debris_debris_fragmentation" - same family, may appear in events
    //   "satellite_or_object_breakup" - same
    // All of these throttle at the debris rate.
    if (obj_type == "satellite") {
        if (orbit_class == "LEO") return THROTTLE_LEO_S;
        if (orbit_class == "MEO") return THROTTLE_MEO_S;
        if (orbit_class == "HEO") {
            const double frac = std::clamp(
                (altitude_m - 1'000'000.0) /
                (39'700'000.0 - 1'000'000.0), 0.0, 1.0);
            return 0.125 + frac * 0.375;
        }
        return THROTTLE_DEFAULT_S;
    }
    if (obj_type == "asteroid") return THROTTLE_AST_S;
    if (obj_type == "debris" ||
        obj_type == "generated_debris_fragment" ||
        obj_type == "debris_debris_fragmentation" ||
        obj_type == "satellite_or_object_breakup") {
        return THROTTLE_DEBRIS_S;
    }
    return THROTTLE_DEFAULT_S;
}

// Canonicalise an object type to its routing bucket.
// All debris-family types funnel into the debris bucket.
inline std::string canonical_bucket(const std::string& obj_type)
{
    if (obj_type == "satellite") return "satellite";
    if (obj_type == "asteroid")  return "asteroid";
    if (obj_type == "debris" ||
        obj_type == "generated_debris_fragment" ||
        obj_type == "debris_debris_fragmentation" ||
        obj_type == "satellite_or_object_breakup") {
        return "debris";
    }
    return "unknown";
}

// ──────────────────────────────────────────────────────────────
// COMM PATH SOLVER
// ──────────────────────────────────────────────────────────────
CommPath find_comm_path(const Vec3& obj_pos, double physical_time_s)
{
    CommPath best;
    best.type = LinkType::BLACKOUT;

    // 1. Direct ground station — closest visible wins.
    double best_dist = std::numeric_limits<double>::infinity();
    for (const auto& gs : g_ground_stations) {
        const Vec3 gs_pos = latlon_to_eci(
            gs.lat_deg, gs.lon_deg, physical_time_s);
        if (!has_los(obj_pos, gs_pos)) continue;
        const double dist = (obj_pos - gs_pos).mag();
        if (dist < best_dist) {
            best_dist           = dist;
            best.type           = LinkType::DIRECT;
            best.ground_station = gs.name;
            best.distance_m     = dist;
            best.propagation_delay_ms =
                (dist / SPEED_OF_LIGHT_MPS) * 1000.0
                + std::abs(gauss_noise_ms());
        }
    }
    if (best.type == LinkType::DIRECT) return best;

    // 2. TDRS relay — object → TDRS → White Sands.
    const Vec3 ws_pos = latlon_to_eci(WS_LAT_DEG, WS_LON_DEG, physical_time_s);
    double best_relay_delay = std::numeric_limits<double>::infinity();
    for (const auto& tdrs : g_tdrs_relays) {
        const Vec3 t_pos = geo_lon_to_eci(tdrs.lon_deg, physical_time_s);
        if (!has_los(obj_pos, t_pos)) continue;
        if (!has_los(t_pos,   ws_pos)) continue;
        const double d1    = (obj_pos - t_pos).mag();
        const double d2    = (t_pos   - ws_pos).mag();
        const double total = d1 + d2;
        const double delay = (total / SPEED_OF_LIGHT_MPS) * 1000.0
                             + std::abs(gauss_noise_ms()) * 2.0;
        if (delay < best_relay_delay) {
            best_relay_delay          = delay;
            best.type                 = LinkType::TDRS;
            best.relay_name           = tdrs.name;
            best.ground_station       = "White Sands NM, USA (TDRS terminal)";
            best.distance_m           = total;
            best.propagation_delay_ms = delay;
        }
    }
    return best;
}

// ──────────────────────────────────────────────────────────────
// EVENT ENQUEUE
// ──────────────────────────────────────────────────────────────
void enqueue_event(DispatchEvent&& ev)
{
    std::unique_lock<std::mutex> lk(g_events_mutex);

    if (g_events.size() >= MAX_EVENT_QUEUE_SIZE) {
        // Overflow: dump heap, keep the closest 90% to release.
        std::vector<DispatchEvent> kept;
        kept.reserve(g_events.size());
        while (!g_events.empty()) { kept.push_back(g_events.top()); g_events.pop(); }
        const std::size_t keep_n = (kept.size() * 9) / 10;
        const std::size_t dropped = kept.size() - keep_n;
        kept.resize(keep_n);
        for (auto& k : kept) g_events.push(std::move(k));
        std::cerr << "[scheduler] WARN: heap overflow, dropped "
                  << dropped << " distant events\n";
    }

    g_events.push(std::move(ev));
    ++g_stat_events_enqueued;
    g_events_cv.notify_one();
}

// ──────────────────────────────────────────────────────────────
// SNAPSHOT PROCESSOR
// ──────────────────────────────────────────────────────────────
void process_snapshot(const fs::path& filepath)
{
    std::string raw;
    if (!read_file(filepath.string(), raw)) {
        std::cerr << "[producer] cannot read " << filepath << "\n";
        return;
    }

    json snap;
    try { snap = json::parse(raw); }
    catch (const json::parse_error& e) {
        std::cerr << "[producer] parse error in "
                  << filepath.filename() << ": " << e.what() << "\n";
        try { fs::remove(filepath); } catch (...) {}
        return;
    }

    const double physical_time_s = snap.value("physical_time_s", 0.0);
    const double sim_unix_time_s = snap.value("unix_time_s",     0.0);
    const double visual_time_s   = snap.value("visual_time_s",   0.0);
    const int    frame           = snap.value("frame",           0);
    const std::string utc_iso    = snap.value("utc_iso",         std::string{});

    auto handle_array = [&](const std::string& key,
                             const std::string& fallback_obj_type) {
        if (!snap.contains(key) || !snap[key].is_array()) return;

        for (const auto& item : snap[key]) {
            std::string oid;
            try { oid = item.at("id").get<std::string>(); }
            catch (...) { continue; }

            // Trust the per-item "type" field (the new dataset-backed schema
            // can put debris records in the "asteroids" array, generated
            // fragments in "debris", etc).  Fall back to the array name
            // only if "type" is missing or not a string.
            std::string item_type = fallback_obj_type;
            try {
                if (item.contains("type") && item.at("type").is_string()) {
                    item_type = item.at("type").get<std::string>();
                }
            } catch (...) {}
            const std::string bucket = canonical_bucket(item_type);

            // Position from "position_m_eci"  (sim's actual key).
            double px=0, py=0, pz=0;
            try {
                const auto& p = item.at("position_m_eci");
                px = p.at("x").get<double>();
                py = p.at("y").get<double>();
                pz = p.at("z").get<double>();
            } catch (...) { continue; }
            const Vec3 pos{px, py, pz};
            const double altitude_m = item.value("altitude_m",
                                                  pos.mag() - R_EARTH_M);
            const std::string orbit_class =
                item.value("orbit_class", std::string{"unknown"});

            // Throttle uses the canonical bucket, not the sub-type.
            const double throttle_s =
                throttle_for(bucket, orbit_class, altitude_m);
            {
                std::lock_guard<std::mutex> lk(g_last_send_mutex);
                auto it = g_last_send.find(oid);
                if (it != g_last_send.end()) {
                    const double elapsed = std::chrono::duration<double>(
                        SteadyClock::now() - it->second).count();
                    if (elapsed < throttle_s) {
                        ++g_stat_throttled;
                        continue;
                    }
                }
            }

            const CommPath path = find_comm_path(pos, physical_time_s);

            // Build the per-object payload that goes onto MQTT.
            // Preserve the original sub-type ("generated_debris_fragment",
            // "satellite_or_object_breakup", etc.) so the GSM and processor
            // can record the exact provenance.  Also stamp a separate
            // "object_bucket" field for fast routing on the GSM side.
            json payload                            = item;
            payload["type"]                         = item_type;
            payload["object_bucket"]                = bucket;
            payload["sim_frame"]                    = frame;
            payload["sim_physical_time_s"]          = physical_time_s;
            payload["sim_visual_time_s"]            = visual_time_s;
            payload["sim_unix_time_s"]              = sim_unix_time_s;
            payload["sim_utc_iso"]                  = utc_iso;

            if (path.type == LinkType::BLACKOUT) {
                std::lock_guard<std::mutex> lk(g_blackout_mutex);
                auto it = g_blackout.find(oid);
                if (it == g_blackout.end()) {
                    g_blackout[oid] = BlackoutSlot{
                        std::move(payload),
                        SteadyClock::now(),
                        physical_time_s
                    };
                } else {
                    it->second.payload         = std::move(payload);
                    it->second.physical_time_s = physical_time_s;
                }
                ++g_stat_blackouts;
                continue;
            }

            DispatchEvent ev;
            ev.release_time = SteadyClock::now()
                + std::chrono::microseconds(static_cast<int64_t>(
                    path.propagation_delay_ms * 1000.0));
            ev.payload      = std::move(payload);
            ev.object_id    = oid;
            ev.comm_path    = path;
            enqueue_event(std::move(ev));
        }
    };

    handle_array("satellites", "satellite");
    handle_array("asteroids",  "asteroid");
    handle_array("debris",     "debris");

    // Drain blackout queue with current physical_time_s.
    std::vector<std::pair<std::string, BlackoutSlot>> to_release;
    {
        std::lock_guard<std::mutex> lk(g_blackout_mutex);
        for (auto& [oid, slot] : g_blackout)
            to_release.emplace_back(oid, slot);
    }
    for (auto& [oid, slot] : to_release) {
        double sx=0, sy=0, sz=0;
        try {
            const auto& p = slot.payload.at("position_m_eci");
            sx = p.at("x").get<double>();
            sy = p.at("y").get<double>();
            sz = p.at("z").get<double>();
        } catch (...) { continue; }
        const Vec3 spos{sx, sy, sz};
        const CommPath p = find_comm_path(spos, physical_time_s);
        if (p.type == LinkType::BLACKOUT) continue;

        const double blackout_duration_s = std::chrono::duration<double>(
            SteadyClock::now() - slot.first_queued_at).count();
        slot.payload["was_blackout_recovery"] = true;
        slot.payload["blackout_duration_s"]   = blackout_duration_s;

        DispatchEvent ev;
        ev.release_time = SteadyClock::now()
            + std::chrono::microseconds(static_cast<int64_t>(
                p.propagation_delay_ms * 1000.0));
        ev.payload   = std::move(slot.payload);
        ev.object_id = oid;
        ev.comm_path = p;
        enqueue_event(std::move(ev));

        std::lock_guard<std::mutex> lk(g_blackout_mutex);
        g_blackout.erase(oid);
    }

    try { fs::remove(filepath); }
    catch (const std::exception& e) {
        std::cerr << "[producer] could not delete " << filepath
                  << ": " << e.what() << "\n";
    }
    ++g_stat_snapshots_processed;
}

// ──────────────────────────────────────────────────────────────
// PRODUCER THREAD
// ──────────────────────────────────────────────────────────────
void producer_thread_fn()
{
    std::set<std::string> seen;

    while (g_running.load()) {
        std::this_thread::sleep_for(
            std::chrono::milliseconds(POLL_INTERVAL_MS));
        if (!g_running.load()) break;

        std::vector<fs::path> new_files;
        try {
            for (const auto& entry : fs::directory_iterator(INPUT_DIR)) {
                if (!entry.is_regular_file()) continue;
                const fs::path& p = entry.path();
                if (p.extension() != ".json") continue;
                const std::string name = p.filename().string();
                if (name.find(".tmp") != std::string::npos) continue;
                if (seen.count(name)) continue;
                new_files.push_back(p);
            }
        } catch (const std::exception& e) {
            std::cerr << "[producer] dir scan: " << e.what() << "\n";
            continue;
        }
        if (new_files.empty()) continue;

        std::sort(new_files.begin(), new_files.end(),
                  [](const fs::path& a, const fs::path& b){
                      return a.filename().string() < b.filename().string();
                  });

        for (const auto& fp : new_files) {
            const std::string name = fp.filename().string();
            seen.insert(name);
            process_snapshot(fp);
        }

        if (seen.size() > 50'000) {
            std::set<std::string> on_disk;
            try {
                for (const auto& e : fs::directory_iterator(INPUT_DIR))
                    on_disk.insert(e.path().filename().string());
            } catch (...) {}
            seen = std::move(on_disk);
        }
    }
}

// ──────────────────────────────────────────────────────────────
// SCHEDULER THREAD
// ──────────────────────────────────────────────────────────────
void scheduler_thread_fn()
{
    while (g_running.load()) {
        DispatchEvent ev;
        {
            std::unique_lock<std::mutex> lk(g_events_mutex);
            g_events_cv.wait(lk, []{
                return !g_events.empty() || !g_running.load();
            });
            if (!g_running.load()) return;
            ev = g_events.top();
            g_events.pop();
        }

        // Sleep until release_time, but in chunks of <= 100 ms so
        // shutdown is responsive.  If not yet ready, re-push and loop.
        const auto now_sc = SteadyClock::now();
        if (ev.release_time > now_sc) {
            const auto gap_us = std::chrono::duration_cast<
                std::chrono::microseconds>(ev.release_time - now_sc).count();
            if (gap_us > 100'000) {
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
                if (!g_running.load()) return;
                std::lock_guard<std::mutex> lk(g_events_mutex);
                g_events.push(std::move(ev));
                g_events_cv.notify_one();
                continue;
            }
            std::this_thread::sleep_until(ev.release_time);
        }
        if (!g_running.load()) return;

        {
            std::lock_guard<std::mutex> lk(g_last_send_mutex);
            g_last_send[ev.object_id] = SteadyClock::now();
        }

        const int64_t send_ns = wall_time_ns();
        ev.payload["sms_send_time_ns"] = send_ns;
        ev.payload["dispatcher_comm_path"] = {
            {"link_type", ev.comm_path.type == LinkType::DIRECT
                          ? "direct" : "tdrs_relay"},
            {"ground_station",       ev.comm_path.ground_station},
            {"tdrs_relay",           ev.comm_path.relay_name},
            {"propagation_delay_ms", ev.comm_path.propagation_delay_ms},
            {"distance_m",           ev.comm_path.distance_m},
        };

        {
            std::lock_guard<std::mutex> lk(g_publish_queue_mutex);
            g_publish_queue.push(PublishItem{
                std::move(ev.payload), std::move(ev.object_id)
            });
        }
        g_publish_queue_cv.notify_one();
    }
}

// ──────────────────────────────────────────────────────────────
// PUBLISH THREAD
// ──────────────────────────────────────────────────────────────
void publish_thread_fn()
{
    while (g_running.load()) {
        PublishItem item;
        {
            std::unique_lock<std::mutex> lk(g_publish_queue_mutex);
            g_publish_queue_cv.wait(lk, []{
                return !g_publish_queue.empty() || !g_running.load();
            });
            if (!g_running.load()) return;
            item = std::move(g_publish_queue.front());
            g_publish_queue.pop();
        }

        try {
            // Per-object topic: satellite/telemetry/{object_id}
            // Sanitize object_id to a valid MQTT topic level: MQTT 3.1.1
            // forbids '+', '#', '/' inside a single level, and zero-length
            // levels. Object IDs from the sim ("SAT-1", "AST-2", "DEBRIS-001")
            // are already safe; we strip just in case.
            std::string safe_id;
            safe_id.reserve(item.object_id.size());
            for (char c : item.object_id) {
                if (c == '+' || c == '#' || c == '/' || c == 0) continue;
                safe_id.push_back(c);
            }
            if (safe_id.empty()) safe_id = "UNKNOWN";

            const std::string topic = TOPIC_PREFIX + "/" + safe_id;
            const std::string body  = item.payload.dump();
            auto msg = mqtt::make_message(topic, body, MQTT_QOS, false);
            if (!g_mqtt || !g_mqtt->is_connected()) {
                ++g_stat_publish_errors;
                continue;
            }
            g_mqtt->publish(msg)->wait_for(std::chrono::seconds(5));
            ++g_stat_events_published;
        } catch (const mqtt::exception& e) {
            std::cerr << "[publish] error " << item.object_id
                      << ": " << e.what() << "\n";
            ++g_stat_publish_errors;
        } catch (const std::exception& e) {
            std::cerr << "[publish] generic error: " << e.what() << "\n";
            ++g_stat_publish_errors;
        }
    }
}

// ──────────────────────────────────────────────────────────────
// CLEANUP THREAD
// ──────────────────────────────────────────────────────────────
void cleanup_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < CLEANUP_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const double cutoff = now_seconds() - QUEUE_MAX_AGE_SECONDS;
        int deleted = 0;
        try {
            for (const auto& entry : fs::directory_iterator(INPUT_DIR)) {
                if (!entry.is_regular_file()) continue;
                if (entry.path().extension() != ".json") continue;
                if (file_mtime_seconds(entry.path()) < cutoff) {
                    fs::remove(entry.path());
                    ++deleted;
                }
            }
        } catch (const std::exception& e) {
            std::cerr << "[cleanup] " << e.what() << "\n";
        }
        if (deleted > 0)
            std::cout << "[cleanup] deleted " << deleted
                      << " stale snapshot(s)\n";
    }
}

// ──────────────────────────────────────────────────────────────
// STATS THREAD
// ──────────────────────────────────────────────────────────────
void stats_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < 10 && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        std::size_t heap_size, blackout_size, pub_q_size;
        { std::lock_guard<std::mutex> lk(g_events_mutex);        heap_size     = g_events.size(); }
        { std::lock_guard<std::mutex> lk(g_blackout_mutex);      blackout_size = g_blackout.size(); }
        { std::lock_guard<std::mutex> lk(g_publish_queue_mutex); pub_q_size    = g_publish_queue.size(); }

        std::cout << "[stats] snapshots=" << g_stat_snapshots_processed.load()
                  << " enq=" << g_stat_events_enqueued.load()
                  << " pub=" << g_stat_events_published.load()
                  << " heap=" << heap_size
                  << " blackout=" << blackout_size
                  << " pubq=" << pub_q_size
                  << " thr=" << g_stat_throttled.load()
                  << " err=" << g_stat_publish_errors.load() << "\n";
    }
}

// ──────────────────────────────────────────────────────────────
// CONFIG LOADER
// ──────────────────────────────────────────────────────────────
void load_ground_network_config()
{
    std::string raw;
    if (read_file(GROUND_NETWORK_CONFIG, raw)) {
        try {
            auto cfg = json::parse(raw);
            if (cfg.contains("ground_stations")) {
                for (const auto& gs : cfg["ground_stations"]) {
                    g_ground_stations.push_back({
                        gs.value("name",    std::string{"unknown"}),
                        gs.value("lat_deg", 0.0),
                        gs.value("lon_deg", 0.0)
                    });
                }
            }
            if (cfg.contains("tdrs_relays")) {
                for (const auto& td : cfg["tdrs_relays"]) {
                    g_tdrs_relays.push_back({
                        td.value("name",    std::string{"unknown"}),
                        td.value("lon_deg", 0.0)
                    });
                }
            }
            std::cout << "[config] loaded " << g_ground_stations.size()
                      << " ground stations and " << g_tdrs_relays.size()
                      << " TDRS relays from " << GROUND_NETWORK_CONFIG << "\n";
            return;
        } catch (const std::exception& e) {
            std::cerr << "[config] parse error: " << e.what()
                      << " — using defaults\n";
        }
    } else {
        std::cerr << "[config] " << GROUND_NETWORK_CONFIG
                  << " not found — using defaults\n";
    }

    g_ground_stations = {
        {"White Sands NM, USA",    32.5,  -106.6},
        {"Guam USA",               13.5,   144.8},
        {"Kiruna Sweden",          67.8,    20.2},
        {"Santiago Chile",        -33.4,   -70.6},
        {"Dongara Australia",     -29.2,   114.9},
        {"North Pole Alaska USA",  64.7,  -163.0},
    };
    g_tdrs_relays = {
        {"TDRS-East 41W GEO",   -41.0},
        {"TDRS-West 171W GEO", -171.0},
        {"TDRS-Guam 174E GEO",  174.0},
    };
}

// ──────────────────────────────────────────────────────────────
// MQTT
// ──────────────────────────────────────────────────────────────
void connect_mqtt()
{
    mqtt::connect_options opts;
    opts.set_keep_alive_interval(20);
    opts.set_clean_session(true);
    opts.set_automatic_reconnect(true);
    opts.set_connect_timeout(std::chrono::seconds(10));

    std::cout << "[mqtt] connecting to " << BROKER_IP << " ...\n";
    int attempts = 0;
    while (g_running.load()) {
        try {
            g_mqtt->connect(opts)->wait();
            std::cout << "[mqtt] connected\n";
            return;
        } catch (const mqtt::exception& e) {
            ++attempts;
            std::cerr << "[mqtt] attempt " << attempts
                      << " failed: " << e.what() << "\n";
            if (attempts >= 5) {
                std::cerr << "[mqtt] giving up after 5 attempts\n";
                throw;
            }
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────
void install_signal_handlers()
{
    auto handler = [](int){
        g_running.store(false);
        g_events_cv.notify_all();
        g_publish_queue_cv.notify_all();
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
              << " dispatcher.cpp v2  —  Priority-Queue Telemetry Router\n"
              << "================================================\n";

    fs::create_directories(INPUT_DIR);
    install_signal_handlers();
    load_ground_network_config();

    g_mqtt = std::make_unique<mqtt::async_client>(BROKER_IP, CLIENT_ID);
    connect_mqtt();

    std::thread t_producer  (producer_thread_fn);
    std::thread t_scheduler (scheduler_thread_fn);
    std::thread t_publisher (publish_thread_fn);
    std::thread t_cleanup   (cleanup_thread_fn);
    std::thread t_stats     (stats_thread_fn);

    std::cout << "[main] watching " << INPUT_DIR
              << " (poll " << POLL_INTERVAL_MS << " ms)\n";
    std::cout << "[main] press Ctrl-C to stop\n";

    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutdown initiated\n";
    g_events_cv.notify_all();
    g_publish_queue_cv.notify_all();
    t_producer.join();
    t_scheduler.join();
    t_publisher.join();
    t_cleanup.join();
    t_stats.join();

    try {
        if (g_mqtt && g_mqtt->is_connected())
            g_mqtt->disconnect()->wait();
    } catch (...) {}

    std::cout << "[main] clean exit. snapshots=" << g_stat_snapshots_processed
              << " enq=" << g_stat_events_enqueued
              << " pub=" << g_stat_events_published
              << " err=" << g_stat_publish_errors << "\n";
    return 0;
}
