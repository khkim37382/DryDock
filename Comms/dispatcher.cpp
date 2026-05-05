// ============================================================
// dispatcher.cpp  —  v3  (new SDK payload, priority-queue, ms timing)
// ============================================================
//
// WHAT THIS FILE DOES
// ───────────────────
// Reads combined telemetry JSON files from ready_to_send_telemetry/
// produced by satellite_orbit.py at up to 40 Hz.
//
// For each file it:
//   1. Reads the top-level packet{} block for T0 and timing.
//   2. Iterates 5 object arrays (satellites, asteroids,
//      catalog_debris_hazards, debris, generated_fragments).
//      Skips hazards[] (union duplicate) entirely.
//   3. For each object: checks line-of-sight to ground stations
//      and TDRS relays.  Objects in blackout are dropped — no
//      mini-payload is formed.
//   4. For objects with a viable comm path: builds a mini-payload
//      containing:
//        - packet timing: t0_local_clock_iso_ms, t0_local_unix_time_ms,
//                         elapsed_ms_since_t0, universe_time_ms,
//                         current_local_clock_iso_ms, frame, sample_hz
//        - dispatcher stamp: sms_send_time_ms (wall-clock ms at publish),
//                            propagation_delay_ms, comm_path details
//        - object data: all fields from the object's SDK block
//        - context: recent_collision_events and hazards arrays from the
//                   same frame (ride along as metadata, no extra topics)
//   5. Schedules the mini-payload on a priority min-heap, released
//      after propagation_delay_ms (milliseconds, speed of light * dist).
//   6. At release: stamps sms_send_time_ms and publishes to
//        satellite/telemetry/{object_id}
//
// BIG JSON FILE TTL
// ─────────────────
// Files in ready_to_send_telemetry/ are NOT deleted by the dispatcher
// after reading.  A cleanup_thread deletes them after SNAPSHOT_TTL_MS
// (3 minutes = 180,000 ms).  This gives the dispatcher a 3-minute
// window to process any file it may have missed on a brief outage.
//
// TIMING — MILLISECONDS THROUGHOUT
// ─────────────────────────────────
// All delays, trip times, and timestamps are in milliseconds.
//   propagation_delay_ms = (distance_m / SPEED_OF_LIGHT_MPS) * 1000.0
//   sms_send_time_ms     = wall-clock Unix ms at MQTT publish moment
//   The GSM reads sms_send_time_ms, records gsm_receive_time_ms,
//   computes sms_to_gsm_trip_ms = gsm_receive_time_ms - sms_send_time_ms.
//
// THREAD ARCHITECTURE (4 long-lived threads + main)
// ───────────────────────────────────────────────────
//   producer_thread   — polls input dir, parses JSON, enqueues events
//   scheduler_thread  — drains min-heap, sleeps until release_time_ms,
//                       then pushes to publish channel
//   publish_thread    — stamps sms_send_time_ms, calls mqtt publish
//   cleanup_thread    — deletes snapshot files older than 3 minutes
//   stats_thread      — periodic console heartbeat
//
// macOS BUILD  (Apple Silicon and Intel)
// ──────────────────────────────────────
//   brew install eclipse-paho-mqtt-cpp nlohmann-json
//   ./build_dispatcher.sh
//   ./dispatcher --broker tcp://BROKER_IP:1883
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

namespace fs    = std::filesystem;
using json      = nlohmann::json;
using SteadyClk = std::chrono::steady_clock;
using SystemClk = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static const std::string INPUT_DIR             = "ready_to_send_telemetry";
static const std::string GROUND_NETWORK_CONFIG = "ground_network_config.json";
static std::string       BROKER_IP             = "tcp://localhost:1883";
static const std::string CLIENT_ID             = "dispatcher_cpp_v3";
static const std::string TOPIC_PREFIX          = "satellite/telemetry";
static const int         MQTT_QOS              = 1;

// File watcher poll interval (ms). At 40 Hz the sim writes every 25 ms;
// polling at 15 ms ensures we never miss a frame.
static const int    POLL_INTERVAL_MS           = 15;

// Snapshot TTL: 3 minutes = 180,000 ms.
// Files stay on disk for 3 min so a brief dispatcher restart can catch up.
static const double SNAPSHOT_TTL_MS            = 180'000.0;
static const int    CLEANUP_INTERVAL_S         = 20;

// JSON landing zone TTL on GSM side is separate; this only controls
// the SMS/dispatcher side.

// Physics constants.
static const double SPEED_OF_LIGHT_MPS         = 299'792'458.0;
static const double R_EARTH_M                  = 6'371'008.4;
static const double GEO_ALT_M                  = 35'786'000.0;
static const double EARTH_ROT_RATE_RAD_S       = 7.2921159e-5;

// Per-type throttle (ms between publishes per object).
// At 40 Hz sim, LEO objects emit every 25 ms real-time.
// We don't throttle harder than the sim rate.
static const double THROTTLE_SATELLITE_MS      = 25.0;   // ~40 Hz
static const double THROTTLE_ASTEROID_MS       = 100.0;  // 10 Hz
static const double THROTTLE_DEBRIS_MS         = 200.0;  //  5 Hz
static const double THROTTLE_DEFAULT_MS        = 25.0;

// White Sands NM — TDRS ground terminal.
static const double WS_LAT_DEG  =  32.5;
static const double WS_LON_DEG  = -106.6;

// Priority heap cap (back-pressure safety).
static const std::size_t MAX_HEAP_SIZE         = 5'000'000;

// ──────────────────────────────────────────────────────────────
// VECTOR MATH
// ──────────────────────────────────────────────────────────────
struct Vec3 {
    double x{0}, y{0}, z{0};
    Vec3() = default;
    Vec3(double xi, double yi, double zi) : x(xi), y(yi), z(zi) {}
    Vec3 operator-(const Vec3& o) const { return {x-o.x, y-o.y, z-o.z}; }
    double dot(const Vec3& o)     const { return x*o.x + y*o.y + z*o.z; }
    double mag()                  const { return std::sqrt(x*x+y*y+z*z); }
};

// ──────────────────────────────────────────────────────────────
// COORDINATE CONVERSION
// ──────────────────────────────────────────────────────────────
inline Vec3 latlon_to_eci(double lat_deg, double lon_deg,
                           double physical_time_s, double alt_m = 0.0)
{
    const double lat = lat_deg * M_PI / 180.0;
    const double lon = lon_deg * M_PI / 180.0
                       + EARTH_ROT_RATE_RAD_S * physical_time_s;
    const double r   = R_EARTH_M + alt_m;
    return { r*std::cos(lat)*std::cos(lon),
             r*std::cos(lat)*std::sin(lon),
             r*std::sin(lat) };
}

inline Vec3 geo_lon_to_eci(double lon_deg, double physical_time_s) {
    return latlon_to_eci(0.0, lon_deg, physical_time_s, GEO_ALT_M);
}

// ──────────────────────────────────────────────────────────────
// LINE-OF-SIGHT  (ray-segment vs Earth-sphere intersection)
// ──────────────────────────────────────────────────────────────
// p(s) = a + s*(b-a),  s in [0,1].
// Blocked iff a real root of |p(s)|^2 = R^2 lies in (0,1).
inline bool has_los(const Vec3& a, const Vec3& b)
{
    const Vec3   d  = b - a;
    const double A  = d.dot(d);
    if (A < 1e-9) return true;
    const double B   = 2.0 * a.dot(d);
    const double C   = a.dot(a) - R_EARTH_M * R_EARTH_M;
    const double disc = B*B - 4.0*A*C;
    if (disc < 0.0) return true;
    const double sq  = std::sqrt(disc);
    const double s1  = (-B - sq) / (2.0*A);
    const double s2  = (-B + sq) / (2.0*A);
    constexpr double EPS = 1e-6;
    if (s1 > EPS && s1 < 1.0-EPS) return false;
    if (s2 > EPS && s2 < 1.0-EPS) return false;
    return true;
}

// ──────────────────────────────────────────────────────────────
// CONFIG STRUCTS
// ──────────────────────────────────────────────────────────────
struct GroundStation { std::string name; double lat_deg{0}, lon_deg{0}; };
struct TDRSRelay     { std::string name; double lon_deg{0};             };
enum class LinkType  { DIRECT, TDRS, BLACKOUT };

struct CommPath {
    LinkType    type{LinkType::BLACKOUT};
    std::string ground_station;
    std::string relay_name;
    double      propagation_delay_ms{0.0};
    double      distance_m{0.0};
};

// ──────────────────────────────────────────────────────────────
// BUCKET MAPPING
// ──────────────────────────────────────────────────────────────
// Maps sim array names and object types to the 3 canonical buckets:
//   satellites / asteroids / debris
// ──────────────────────────────────────────────────────────────
inline std::string bucket_for_array(const std::string& array_key)
{
    if (array_key == "satellites")               return "satellites";
    if (array_key == "asteroids")                return "asteroids";
    if (array_key == "catalog_debris_hazards")   return "debris";
    if (array_key == "debris")                   return "debris";
    if (array_key == "generated_fragments")      return "debris";
    return "debris"; // safe fallback
}

inline double throttle_for_bucket(const std::string& bucket)
{
    if (bucket == "satellites") return THROTTLE_SATELLITE_MS;
    if (bucket == "asteroids")  return THROTTLE_ASTEROID_MS;
    return THROTTLE_DEBRIS_MS;
}

// ──────────────────────────────────────────────────────────────
// DISPATCH EVENT  (min-heap by release_time_ms)
// ──────────────────────────────────────────────────────────────
struct DispatchEvent {
    double      release_time_ms;  // wall-clock ms when to publish
    json        payload;
    std::string object_id;
    CommPath    comm_path;
};
struct EventGreater {
    bool operator()(const DispatchEvent& l, const DispatchEvent& r) const {
        return l.release_time_ms > r.release_time_ms;
    }
};
using EventHeap = std::priority_queue<
    DispatchEvent, std::vector<DispatchEvent>, EventGreater>;

struct PublishItem { json payload; std::string object_id; };

struct BlackoutSlot {
    json        payload;
    double      first_queued_wall_ms{0.0};
    double      physical_time_s{0.0};
};

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────
std::vector<GroundStation> g_ground_stations;
std::vector<TDRSRelay>     g_tdrs_relays;

std::unordered_map<std::string, double> g_last_send_ms;  // object_id → wall ms
std::mutex g_last_send_mutex;

std::unordered_map<std::string, BlackoutSlot> g_blackout;
std::mutex g_blackout_mutex;

EventHeap               g_events;
std::mutex              g_events_mutex;
std::condition_variable g_events_cv;

std::queue<PublishItem> g_publish_queue;
std::mutex              g_publish_queue_mutex;
std::condition_variable g_publish_queue_cv;

std::unique_ptr<mqtt::async_client> g_mqtt;

std::default_random_engine       g_rng(std::random_device{}());
std::normal_distribution<double> g_noise_dist(0.0, 0.5); // ±0.5 ms jitter
std::mutex                       g_noise_mutex;

std::atomic<bool> g_running{true};

std::atomic<uint64_t> g_stat_files_seen{0};
std::atomic<uint64_t> g_stat_objects_processed{0};
std::atomic<uint64_t> g_stat_blackouts{0};
std::atomic<uint64_t> g_stat_throttled{0};
std::atomic<uint64_t> g_stat_events_enqueued{0};
std::atomic<uint64_t> g_stat_events_published{0};
std::atomic<uint64_t> g_stat_publish_errors{0};

// ──────────────────────────────────────────────────────────────
// HELPERS
// ──────────────────────────────────────────────────────────────
inline double wall_time_ms()
{
    return std::chrono::duration<double, std::milli>(
        SystemClk::now().time_since_epoch()).count();
}

inline double steady_ms()
{
    return std::chrono::duration<double, std::milli>(
        SteadyClk::now().time_since_epoch()).count();
}

inline double noise_ms()
{
    std::lock_guard<std::mutex> lk(g_noise_mutex);
    return g_noise_dist(g_rng);
}

bool read_file(const std::string& path, std::string& out)
{
    std::ifstream f(path);
    if (!f.is_open()) return false;
    std::ostringstream ss; ss << f.rdbuf();
    out = ss.str();
    return true;
}

double file_mtime_ms(const fs::path& p)
{
    using namespace std::chrono;
    auto ftime = fs::last_write_time(p);
    auto sctp  = time_point_cast<milliseconds>(fs::file_clock::to_sys(ftime));
    return static_cast<double>(sctp.time_since_epoch().count());
}

// ──────────────────────────────────────────────────────────────
// COMM PATH SOLVER
// ──────────────────────────────────────────────────────────────
CommPath find_comm_path(const Vec3& pos, double physical_time_s)
{
    CommPath best;
    best.type = LinkType::BLACKOUT;

    // 1. Direct ground station — closest visible wins.
    double best_dist = std::numeric_limits<double>::infinity();
    for (const auto& gs : g_ground_stations) {
        const Vec3 gs_pos = latlon_to_eci(gs.lat_deg, gs.lon_deg, physical_time_s);
        if (!has_los(pos, gs_pos)) continue;
        const double dist = (pos - gs_pos).mag();
        if (dist < best_dist) {
            best_dist                  = dist;
            best.type                  = LinkType::DIRECT;
            best.ground_station        = gs.name;
            best.distance_m            = dist;
            best.propagation_delay_ms  =
                (dist / SPEED_OF_LIGHT_MPS) * 1000.0
                + std::abs(noise_ms());
        }
    }
    if (best.type == LinkType::DIRECT) return best;

    // 2. TDRS relay — object → TDRS → White Sands.
    const Vec3 ws = latlon_to_eci(WS_LAT_DEG, WS_LON_DEG, physical_time_s);
    double best_delay = std::numeric_limits<double>::infinity();
    for (const auto& tdrs : g_tdrs_relays) {
        const Vec3 t = geo_lon_to_eci(tdrs.lon_deg, physical_time_s);
        if (!has_los(pos, t)) continue;
        if (!has_los(t,   ws)) continue;
        const double d1    = (pos - t).mag();
        const double d2    = (t   - ws).mag();
        const double total = d1 + d2;
        const double delay = (total / SPEED_OF_LIGHT_MPS) * 1000.0
                             + std::abs(noise_ms()) * 2.0;
        if (delay < best_delay) {
            best_delay                = delay;
            best.type                 = LinkType::TDRS;
            best.relay_name           = tdrs.name;
            best.ground_station       = "White Sands NM (TDRS terminal)";
            best.distance_m           = total;
            best.propagation_delay_ms = delay;
        }
    }
    return best;
}

// ──────────────────────────────────────────────────────────────
// HEAP ENQUEUE
// ──────────────────────────────────────────────────────────────
void enqueue_event(DispatchEvent&& ev)
{
    std::unique_lock<std::mutex> lk(g_events_mutex);
    if (g_events.size() >= MAX_HEAP_SIZE) {
        // Overflow: keep closest 90% (drop most-distant events).
        std::vector<DispatchEvent> kept;
        kept.reserve(g_events.size());
        while (!g_events.empty()) { kept.push_back(g_events.top()); g_events.pop(); }
        const std::size_t keep_n  = (kept.size() * 9) / 10;
        const std::size_t dropped = kept.size() - keep_n;
        kept.resize(keep_n);
        for (auto& k : kept) g_events.push(std::move(k));
        std::cerr << "[heap] WARN overflow, dropped " << dropped << " events\n";
    }
    g_events.push(std::move(ev));
    ++g_stat_events_enqueued;
    g_events_cv.notify_one();
}

// ──────────────────────────────────────────────────────────────
// PROCESS ONE OBJECT
// Called for every object in every routable array.
// ──────────────────────────────────────────────────────────────
void process_object(const json&        item,
                    const std::string& array_key,
                    const json&        packet,
                    const json&        context_collision_events,
                    const json&        context_hazards,
                    double             physical_time_s)
{
    // ── identity ────────────────────────────────────────────────
    std::string oid;
    try { oid = item.at("id").get<std::string>(); }
    catch (...) { return; }
    if (oid.empty()) return;

    const std::string bucket = bucket_for_array(array_key);

    // ── position ─────────────────────────────────────────────────
    // New sim uses position_m_eci as a nested {x,y,z} object.
    Vec3 pos;
    try {
        const auto& p = item.at("position_m_eci");
        pos.x = p.at("x").get<double>();
        pos.y = p.at("y").get<double>();
        pos.z = p.at("z").get<double>();
    } catch (...) { return; }

    // ── throttle ─────────────────────────────────────────────────
    const double throttle_ms = throttle_for_bucket(bucket);
    {
        std::lock_guard<std::mutex> lk(g_last_send_mutex);
        auto it = g_last_send_ms.find(oid);
        if (it != g_last_send_ms.end()) {
            const double elapsed = wall_time_ms() - it->second;
            if (elapsed < throttle_ms) {
                ++g_stat_throttled;
                return;
            }
        }
    }

    // ── geometry → comm path ─────────────────────────────────────
    const CommPath path = find_comm_path(pos, physical_time_s);

    // BLACKOUT → drop entirely, no mini-payload formed.
    if (path.type == LinkType::BLACKOUT) {
        ++g_stat_blackouts;
        return;
    }

    ++g_stat_objects_processed;

    // ── build mini-payload ───────────────────────────────────────
    json payload;

    // Routing fields
    payload["object_id"]     = oid;
    payload["object_type"]   = item.value("type", array_key);
    payload["object_bucket"] = bucket;

    // Packet timing block — copied from the big JSON's packet{}.
    // sms_send_time_ms is stamped later, at actual MQTT publish time.
    payload["packet"] = {
        {"frame",                    packet.value("frame", 0)},
        {"universe_time_ms",         packet.value("universe_time_ms", 0)},
        {"sample_hz",                packet.value("sample_hz", 0.0)},
        // T0 anchor — SMS computer local clock at sim initialization.
        {"t0_local_clock_iso_ms",    packet.value("t0_local_clock_iso_ms",    nullptr)},
        {"t0_local_unix_time_ms",    packet.value("t0_local_unix_time_ms",    nullptr)},
        // Elapsed ms since T0 (monotonic perf_counter on SMS machine).
        {"elapsed_ms_since_t0",      packet.value("elapsed_ms_since_t0",      nullptr)},
        // SMS wall clock at the moment this snapshot was written.
        {"current_local_clock_iso_ms", packet.value("current_local_clock_iso_ms", nullptr)},
        {"current_local_unix_time_ms", packet.value("current_local_unix_time_ms", nullptr)},
        // Backward-compatible aliases.
        {"simulation_local_start_iso",      packet.value("simulation_local_start_iso",      nullptr)},
        {"simulation_local_start_unix_time_ms", packet.value("simulation_local_start_unix_time_ms", nullptr)},
        {"elapsed_local_time_ms_since_sim_start", packet.value("elapsed_local_time_ms_since_sim_start", nullptr)},
    };

    // Dispatcher comm-path block (filled now, sms_send_time_ms added at publish).
    payload["dispatcher"] = {
        {"link_type",             path.type == LinkType::DIRECT ? "direct" : "tdrs_relay"},
        {"ground_station",        path.ground_station},
        {"tdrs_relay",            path.relay_name},
        {"propagation_delay_ms",  path.propagation_delay_ms},
        {"distance_m",            path.distance_m},
        // sms_send_time_ms stamped at actual MQTT publish by publish_thread
    };

    // Full object data block.
    payload["data"] = item;

    // Context: collision events and hazards from same frame.
    // These ride as metadata — no separate MQTT topic needed.
    payload["context"] = {
        {"recent_collision_events", context_collision_events},
        {"hazards",                 context_hazards},
    };

    // ── schedule on priority heap ────────────────────────────────
    DispatchEvent ev;
    ev.release_time_ms = wall_time_ms() + path.propagation_delay_ms;
    ev.payload         = std::move(payload);
    ev.object_id       = oid;
    ev.comm_path       = path;
    enqueue_event(std::move(ev));

    // Mark last-send time (throttle gate).
    {
        std::lock_guard<std::mutex> lk(g_last_send_mutex);
        g_last_send_ms[oid] = wall_time_ms();
    }
}

// ──────────────────────────────────────────────────────────────
// PROCESS SNAPSHOT FILE
// ──────────────────────────────────────────────────────────────
void process_snapshot(const fs::path& filepath)
{
    ++g_stat_files_seen;

    std::string raw;
    if (!read_file(filepath.string(), raw)) {
        std::cerr << "[producer] cannot read " << filepath << "\n";
        return;
    }

    json snap;
    try { snap = json::parse(raw); }
    catch (const json::parse_error& e) {
        std::cerr << "[producer] parse error " << filepath.filename()
                  << ": " << e.what() << "\n";
        return;
    }

    // ── packet block (timing anchor) ─────────────────────────────
    const json packet   = snap.value("packet", json::object());
    const double universe_time_ms = packet.value("universe_time_ms", 0.0);

    // Derive physical_time_s for LOS geometry (universe_time_ms → seconds).
    const double physical_time_s  = universe_time_ms / 1000.0;

    // ── context arrays (ride along as metadata) ───────────────────
    // hazards[] is the union — we pass it as context only, never route it.
    const json ctx_collisions = snap.value("recent_collision_events", json::array());
    const json ctx_hazards    = snap.value("hazards",                 json::array());

    // ── route each object array ───────────────────────────────────
    // Order: satellites first (highest priority), then asteroids, then debris-family.
    static const std::vector<std::string> ROUTABLE_ARRAYS = {
        "satellites",
        "asteroids",
        "catalog_debris_hazards",
        "debris",
        "generated_fragments",
        // "hazards" intentionally omitted — union duplicate
    };

    for (const auto& array_key : ROUTABLE_ARRAYS) {
        if (!snap.contains(array_key) || !snap[array_key].is_array()) continue;
        for (const auto& item : snap[array_key]) {
            try {
                process_object(item, array_key,
                               packet,
                               ctx_collisions,
                               ctx_hazards,
                               physical_time_s);
            } catch (const std::exception& e) {
                std::cerr << "[producer] object error in " << array_key
                          << ": " << e.what() << "\n";
            }
        }
    }

    // NOTE: File is NOT deleted here.
    // cleanup_thread deletes files older than SNAPSHOT_TTL_MS (3 min).
}

// ──────────────────────────────────────────────────────────────
// PRODUCER THREAD  (file watcher)
// ──────────────────────────────────────────────────────────────
void producer_thread_fn()
{
    std::set<std::string> seen;

    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(POLL_INTERVAL_MS));
        if (!g_running.load()) break;

        std::vector<fs::path> new_files;
        try {
            for (const auto& entry : fs::directory_iterator(INPUT_DIR)) {
                if (!entry.is_regular_file()) continue;
                const auto& p = entry.path();
                if (p.extension() != ".json") continue;
                const std::string name = p.filename().string();
                if (name.find(".tmp") != std::string::npos) continue;
                if (seen.count(name)) continue;
                new_files.push_back(p);
            }
        } catch (...) { continue; }
        if (new_files.empty()) continue;

        // Process chronologically (filename contains timestamp).
        std::sort(new_files.begin(), new_files.end(),
                  [](const fs::path& a, const fs::path& b){
                      return a.filename().string() < b.filename().string();
                  });
        for (const auto& fp : new_files) {
            seen.insert(fp.filename().string());
            process_snapshot(fp);
        }

        // Prune seen set.
        if (seen.size() > 100'000) {
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
// SCHEDULER THREAD  (drains heap, sleeps until release_time_ms)
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

        // Sleep until release_time_ms in chunks ≤ 50 ms for shutdown responsiveness.
        double remaining_ms = ev.release_time_ms - wall_time_ms();
        while (remaining_ms > 50.0 && g_running.load()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            remaining_ms = ev.release_time_ms - wall_time_ms();
        }
        if (!g_running.load()) return;
        if (remaining_ms > 0.0) {
            std::this_thread::sleep_for(
                std::chrono::microseconds(static_cast<int64_t>(remaining_ms * 1000.0)));
        }
        if (!g_running.load()) return;

        {
            std::lock_guard<std::mutex> lk(g_publish_queue_mutex);
            g_publish_queue.push(PublishItem{std::move(ev.payload),
                                              std::move(ev.object_id)});
        }
        g_publish_queue_cv.notify_one();
    }
}

// ──────────────────────────────────────────────────────────────
// PUBLISH THREAD  (stamps sms_send_time_ms, calls mqtt)
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

        // Stamp sms_send_time_ms at the exact moment of publish.
        const double send_ms = wall_time_ms();
        item.payload["dispatcher"]["sms_send_time_ms"] = send_ms;

        // Sanitise object_id for MQTT topic (strip +, #, /).
        std::string safe_id;
        safe_id.reserve(item.object_id.size());
        for (char c : item.object_id) {
            if (c == '+' || c == '#' || c == '/' || c == 0) continue;
            safe_id.push_back(c);
        }
        if (safe_id.empty()) safe_id = "UNKNOWN";
        const std::string topic = TOPIC_PREFIX + "/" + safe_id;

        try {
            const std::string body = item.payload.dump();
            auto msg = mqtt::make_message(topic, body, MQTT_QOS, false);
            if (!g_mqtt || !g_mqtt->is_connected()) {
                ++g_stat_publish_errors;
                continue;
            }
            g_mqtt->publish(msg)->wait_for(std::chrono::seconds(5));
            ++g_stat_events_published;
        } catch (const mqtt::exception& e) {
            std::cerr << "[publish] mqtt error " << item.object_id
                      << ": " << e.what() << "\n";
            ++g_stat_publish_errors;
        } catch (const std::exception& e) {
            std::cerr << "[publish] error: " << e.what() << "\n";
            ++g_stat_publish_errors;
        }
    }
}

// ──────────────────────────────────────────────────────────────
// CLEANUP THREAD  (3-minute TTL on snapshot files)
// ──────────────────────────────────────────────────────────────
// Files are deleted here, NOT in process_snapshot().
// 3 minutes gives the dispatcher time to recover from a brief outage.
void cleanup_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < CLEANUP_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const double cutoff_ms = wall_time_ms() - SNAPSHOT_TTL_MS;
        int deleted = 0, pending = 0;
        try {
            for (const auto& entry : fs::directory_iterator(INPUT_DIR)) {
                if (!entry.is_regular_file()) continue;
                if (entry.path().extension() != ".json") continue;
                const std::string name = entry.path().filename().string();
                if (name.find(".tmp") != std::string::npos) continue;
                const double mtime = file_mtime_ms(entry.path());
                if (mtime < cutoff_ms) {
                    std::error_code ec;
                    fs::remove(entry.path(), ec);
                    if (!ec) ++deleted;
                } else {
                    ++pending;
                }
            }
        } catch (const std::exception& e) {
            std::cerr << "[cleanup] " << e.what() << "\n";
        }
        if (deleted > 0 || pending > 100) {
            std::cout << "[cleanup] deleted=" << deleted
                      << " pending=" << pending
                      << " ttl=180s\n";
        }
    }
}

// ──────────────────────────────────────────────────────────────
// STATS THREAD
// ──────────────────────────────────────────────────────────────
void stats_thread_fn()
{
    uint64_t last_pub = 0;
    while (g_running.load()) {
        for (int i = 0; i < 10 && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const uint64_t pub  = g_stat_events_published.load();
        const double   rate = static_cast<double>(pub - last_pub) / 10.0;
        last_pub = pub;

        std::size_t heap_sz, pub_q_sz;
        { std::lock_guard<std::mutex> lk(g_events_mutex);        heap_sz  = g_events.size(); }
        { std::lock_guard<std::mutex> lk(g_publish_queue_mutex); pub_q_sz = g_publish_queue.size(); }

        std::cout << "[stats] files=" << g_stat_files_seen.load()
                  << " objects=" << g_stat_objects_processed.load()
                  << " enq=" << g_stat_events_enqueued.load()
                  << " pub=" << pub
                  << " (" << rate << "/s)"
                  << " heap=" << heap_sz
                  << " pubq=" << pub_q_sz
                  << " blackout=" << g_stat_blackouts.load()
                  << " thr=" << g_stat_throttled.load()
                  << " err=" << g_stat_publish_errors.load()
                  << "\n";
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
                      << " TDRS relays\n";
            return;
        } catch (const std::exception& e) {
            std::cerr << "[config] parse error: " << e.what() << " — using defaults\n";
        }
    } else {
        std::cerr << "[config] " << GROUND_NETWORK_CONFIG
                  << " not found — using defaults\n";
    }

    g_ground_stations = {
        {"White Sands NM USA",     32.5, -106.6},
        {"Guam USA",               13.5,  144.8},
        {"Kiruna Sweden",          67.8,   20.2},
        {"Santiago Chile",        -33.4,  -70.6},
        {"Dongara Australia",     -29.2,  114.9},
        {"North Pole Alaska USA",  64.7, -163.0},
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
            std::cerr << "[mqtt] attempt " << attempts << " failed: " << e.what() << "\n";
            if (attempts >= 5) throw;
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────
void install_signal_handlers()
{
    auto h = [](int){
        g_running.store(false);
        g_events_cv.notify_all();
        g_publish_queue_cv.notify_all();
    };
    std::signal(SIGINT,  h);
    std::signal(SIGTERM, h);
}

// ──────────────────────────────────────────────────────────────
// MAIN
// ──────────────────────────────────────────────────────────────
// Usage:
//   ./dispatcher                                    # localhost broker
//   ./dispatcher --broker tcp://192.168.1.50:1883  # remote broker
int main(int argc, char* argv[])
{
    for (int i = 1; i < argc - 1; ++i) {
        if (std::string(argv[i]) == "--broker") {
            BROKER_IP = argv[i+1];
            if (BROKER_IP.find("://") == std::string::npos)
                BROKER_IP = "tcp://" + BROKER_IP;
            ++i;
        }
    }

    std::cout << "================================================\n"
              << " dispatcher.cpp v3  —  SMS Telemetry Router\n"
              << "================================================\n"
              << "[main] broker:       " << BROKER_IP << "\n"
              << "[main] input dir:    " << INPUT_DIR << "\n"
              << "[main] topic:        " << TOPIC_PREFIX << "/{object_id}\n"
              << "[main] snapshot ttl: 180 s (3 min)\n"
              << "[main] poll:         " << POLL_INTERVAL_MS << " ms\n"
              << "[main] arrays:       satellites asteroids catalog_debris_hazards"
                 " debris generated_fragments\n"
              << "[main] buckets:      satellites/ asteroids/ debris/\n"
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

    std::cout << "[main] running — Ctrl-C to stop\n";

    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutdown ...\n";
    g_events_cv.notify_all();
    g_publish_queue_cv.notify_all();
    t_producer.join();
    t_scheduler.join();
    t_publisher.join();
    t_cleanup.join();
    t_stats.join();

    try { if (g_mqtt && g_mqtt->is_connected()) g_mqtt->disconnect()->wait(); }
    catch (...) {}

    std::cout << "[main] clean exit."
              << " files=" << g_stat_files_seen
              << " objects=" << g_stat_objects_processed
              << " pub=" << g_stat_events_published
              << " err=" << g_stat_publish_errors << "\n";
    return 0;
}
