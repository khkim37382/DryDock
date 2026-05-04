// ============================================================
// ground_station_manager.cpp  —  v1
// ============================================================
// Replaces ground_station_manager.py.  Acts as the GSM-side MQTT
// bridge: subscribes with wildcard satellite/telemetry/+, stamps
// gsm_receive_time_ns + sms_to_gsm_trip_ns onto each incoming
// payload, writes per-object JSON files to disk for the C++
// processor to ingest.
//
// PIPELINE POSITION
// ─────────────────
//
//   dispatcher.cpp  ──► MQTT broker  ──► ground_station_manager.cpp
//                                              │
//                                              ▼
//                  received_transmissions/{type}/{object_id}/
//                       ping_<receive_ns>.json
//                                              │
//                                              ▼
//                                ground_station_processor.cpp
//
// THREADS
// ───────
//   1× MQTT message thread (Paho async client's internal callback)
//   1× outbound watchdog thread (polls ready_to_send_transmissions/
//                                 — preserved for future bidirectional
//                                 use; currently inactive in pipeline)
//   1× cleanup thread (deletes JSON files older than 5 min)
//   1× stats heartbeat thread
//
// MUTEXES
// ───────
//   stats_mtx    — protects per-topic counters
//   publish_mtx  — protects MQTT client when outbound watchdog publishes
//
// ATOMIC FILE WRITES
// ──────────────────
//   Each file is written to ".tmp" then std::filesystem::rename'd
//   into place so the processor's watcher never reads a half-written
//   file.
//
// macOS BUILD INSTRUCTIONS
// ────────────────────────
//   brew install eclipse-paho-mqtt-cpp nlohmann-json
//   ./build_gsm_manager.sh
// ============================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <queue>
#include <unordered_map>
#include <thread>
#include <mutex>
#include <atomic>
#include <condition_variable>
#include <chrono>
#include <filesystem>
#include <algorithm>
#include <csignal>
#include <ctime>

#include <nlohmann/json.hpp>
#include <mqtt/async_client.h>

namespace fs = std::filesystem;
using json   = nlohmann::json;
using SystemClk = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static const std::string BROKER_IP            = "tcp://localhost:1883";
static const std::string CLIENT_ID            = "gsm_manager_cpp_v1";

// Wildcard subscription — every object_id under satellite/telemetry
// auto-flows through, no resubscribe needed when new IDs appear.
static const std::string TOPIC_TELEMETRY_WILD = "satellite/telemetry/+";

// Outbound topic (preserved for future bidirectional work, NOT
// currently in active pipeline).
static const std::string TOPIC_THRUSTER       = "satellite/thruster";

// Filesystem layout — must match what the processor watches.
static const std::string SAVE_DIR             = "received_transmissions";
static const std::string OUTPUT_DIR           = "ready_to_send_transmissions";

// Retention — match the Python original.
static const double JSON_TTL_SECONDS          = 5.0 * 60.0;   // 5 min

static const int MQTT_QOS                     = 1;
static const int CLEANUP_INTERVAL_S           = 60;
static const int OUTBOUND_POLL_MS             = 100;
static const int STATS_INTERVAL_S             = 10;

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────

std::unique_ptr<mqtt::async_client> g_mqtt;
std::mutex                          g_publish_mtx;     // serialises outbound publishes

// Per-topic stats.
std::unordered_map<std::string, uint64_t> g_topic_counters;
std::unordered_map<std::string, double>   g_topic_last_seen;
std::mutex g_stats_mtx;

std::atomic<uint64_t> g_total_received{0};
std::atomic<uint64_t> g_total_invalid{0};
std::atomic<uint64_t> g_total_written{0};
std::atomic<uint64_t> g_total_outbound_sent{0};

std::atomic<bool> g_running{true};

// ──────────────────────────────────────────────────────────────
// HELPERS
// ──────────────────────────────────────────────────────────────

inline int64_t wall_time_ns()
{
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
        SystemClk::now().time_since_epoch()).count();
}

inline double now_seconds()
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

// Sanitise a path component — remove anything that could break
// the filesystem layout (slashes, control chars, etc).
std::string safe_path_part(const std::string& s)
{
    if (s.empty()) return "unknown";
    static const std::string bad = "/\\:*?\"<>|";
    std::string out; out.reserve(s.size());
    for (char c : s) {
        if (c == '\0' || (unsigned char)c < 0x20 ||
            bad.find(c) != std::string::npos) {
            out.push_back('_');
        } else {
            out.push_back(c);
        }
    }
    return out;
}

// Atomic JSON write: write to .tmp, rename into place.
bool atomic_write_json(const fs::path& target, const std::string& body)
{
    const fs::path tmp = target.string() + ".tmp";
    try {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f.is_open()) return false;
        f.write(body.data(), static_cast<std::streamsize>(body.size()));
        if (!f.good()) return false;
        f.close();
        std::error_code ec;
        fs::rename(tmp, target, ec);
        if (ec) {
            // best-effort cleanup on rename failure
            std::error_code ec2;
            fs::remove(tmp, ec2);
            return false;
        }
        return true;
    } catch (const std::exception& e) {
        std::cerr << "[gsm] atomic_write failed " << target
                  << ": " << e.what() << "\n";
        return false;
    }
}

// ──────────────────────────────────────────────────────────────
// MQTT CALLBACK CLASS
// ──────────────────────────────────────────────────────────────
// Paho's recommended pattern is to subclass mqtt::callback so we
// can override message_arrived, connection_lost, etc.  This keeps
// the MQTT internals in Paho's worker thread without any extra
// scheduling on our side.
// ──────────────────────────────────────────────────────────────

class GsmCallback : public virtual mqtt::callback
{
public:
    void connected(const std::string& cause) override
    {
        std::cout << "[mqtt] connected (" << cause << "), subscribing to "
                  << TOPIC_TELEMETRY_WILD << "\n";
        try {
            g_mqtt->subscribe(TOPIC_TELEMETRY_WILD, MQTT_QOS)->wait();
        } catch (const mqtt::exception& e) {
            std::cerr << "[mqtt] subscribe failed: " << e.what() << "\n";
        }
    }

    void connection_lost(const std::string& cause) override
    {
        std::cerr << "[mqtt] connection lost: " << cause
                  << "  (auto_reconnect will retry)\n";
    }

    void message_arrived(mqtt::const_message_ptr msg) override
    {
        // Stamp receive time IMMEDIATELY for the truest possible value.
        const int64_t gsm_receive_ns = wall_time_ns();

        const std::string& topic = msg->get_topic();
        const std::string& body  = msg->get_payload_str();

        json payload;
        try {
            payload = json::parse(body);
        } catch (const json::parse_error& e) {
            ++g_total_invalid;
            std::cerr << "[mqtt] invalid JSON on " << topic
                      << ": " << e.what() << "\n";
            return;
        }

        // ── Per-topic stats ──────────────────────────────────
        {
            std::lock_guard<std::mutex> lk(g_stats_mtx);
            g_topic_counters[topic] += 1;
            g_topic_last_seen[topic] = now_seconds();
        }
        ++g_total_received;

        // ── Identify object ──────────────────────────────────
        std::string object_id;
        std::string object_type   = "unknown";   // raw/specific type, kept in payload
        std::string object_bucket = "unknown";   // canonical bucket for filesystem
                                                  // routing: satellite/asteroid/debris

        if (payload.contains("id") && payload["id"].is_string()) {
            object_id = payload["id"].get<std::string>();
        } else if (payload.contains("object_id") && payload["object_id"].is_string()) {
            object_id = payload["object_id"].get<std::string>();
        }

        if (payload.contains("type") && payload["type"].is_string()) {
            object_type = payload["type"].get<std::string>();
        } else if (payload.contains("object_type") && payload["object_type"].is_string()) {
            object_type = payload["object_type"].get<std::string>();
        }

        // The dispatcher stamps "object_bucket" with the canonical routing
        // bucket (collapses all debris-family sub-types into "debris").
        // If absent we derive it from object_type with the same rules.
        if (payload.contains("object_bucket") && payload["object_bucket"].is_string()) {
            object_bucket = payload["object_bucket"].get<std::string>();
        } else {
            if (object_type == "satellite") {
                object_bucket = "satellite";
            } else if (object_type == "asteroid") {
                object_bucket = "asteroid";
            } else if (object_type == "debris" ||
                       object_type == "generated_debris_fragment" ||
                       object_type == "debris_debris_fragmentation" ||
                       object_type == "satellite_or_object_breakup") {
                object_bucket = "debris";
            } else {
                object_bucket = "unknown";
            }
            payload["object_bucket"] = object_bucket;  // fill it in so processor sees it
        }

        // Fall back to topic suffix for object_id.
        if (object_id.empty()) {
            const auto pos = topic.find_last_of('/');
            if (pos != std::string::npos && pos + 1 < topic.size()) {
                object_id = topic.substr(pos + 1);
            }
        }

        if (object_id.empty()) {
            ++g_total_invalid;
            std::cerr << "[mqtt] no object_id in payload from " << topic
                      << ", dropping\n";
            return;
        }

        // ── Compute uplink trip (sms_to_gsm) ─────────────────
        int64_t sms_send_ns = 0;
        bool    have_sms_send = false;
        if (payload.contains("sms_send_time_ns") &&
            payload["sms_send_time_ns"].is_number_integer()) {
            sms_send_ns = payload["sms_send_time_ns"].get<int64_t>();
            have_sms_send = true;
        }

        payload["gsm_receive_time_ns"] = gsm_receive_ns;
        if (have_sms_send) {
            payload["sms_to_gsm_trip_ns"] = gsm_receive_ns - sms_send_ns;
        } else {
            payload["sms_to_gsm_trip_ns"] = nullptr;
        }
        payload["mqtt_topic"] = topic;

        // ── Write atomically ─────────────────────────────────
        // Folder routing uses the BUCKET (debris/satellite/asteroid),
        // not the raw sub-type, so generated_debris_fragment ends up in
        // received_transmissions/debris/<id>/  alongside dataset debris.
        const std::string safe_bucket = safe_path_part(object_bucket);
        const std::string safe_oid    = safe_path_part(object_id);
        fs::path dir = fs::path(SAVE_DIR) / safe_bucket / safe_oid;
        try {
            fs::create_directories(dir);
        } catch (const std::exception& e) {
            std::cerr << "[mqtt] mkdir " << dir << ": " << e.what() << "\n";
            return;
        }

        char filename[64];
        std::snprintf(filename, sizeof(filename),
                      "ping_%020lld.json",
                      static_cast<long long>(gsm_receive_ns));

        fs::path target = dir / filename;
        const std::string out_body = payload.dump();

        if (atomic_write_json(target, out_body)) {
            ++g_total_written;
        }
    }

    void delivery_complete(mqtt::delivery_token_ptr) override {}
};

// ──────────────────────────────────────────────────────────────
// OUTBOUND THRUST-VECTOR WATCHDOG  (preserved, NOT in active pipeline)
// ──────────────────────────────────────────────────────────────
//
// Polls OUTPUT_DIR for thrust-vector JSON files dropped by an
// external decision script.  Stamps gsm_send_time_ns and publishes
// to TOPIC_THRUSTER, then deletes the file.
//
// The SMS-side handler (satellite_manager.py) is held in reserve.
// This watchdog is wired up so when bidirectional is activated,
// no GSM-side change is needed.
// ──────────────────────────────────────────────────────────────

void outbound_watchdog_thread_fn()
{
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(OUTBOUND_POLL_MS));
        if (!g_running.load()) break;

        std::vector<fs::path> files;
        try {
            for (const auto& entry : fs::directory_iterator(OUTPUT_DIR)) {
                if (!entry.is_regular_file()) continue;
                const auto& p = entry.path();
                if (p.extension() != ".json") continue;
                const std::string name = p.filename().string();
                if (name.find(".tmp") != std::string::npos) continue;
                files.push_back(p);
            }
        } catch (const std::exception& e) {
            // OUTPUT_DIR may not exist yet — fine, just retry next tick.
            continue;
        }
        if (files.empty()) continue;

        // Process oldest first (chronological filenames).
        std::sort(files.begin(), files.end(),
                  [](const fs::path& a, const fs::path& b){
                      return a.filename().string() < b.filename().string();
                  });

        for (const auto& fp : files) {
            // Wait briefly for atomic-rename writers to finish.
            std::this_thread::sleep_for(std::chrono::milliseconds(50));

            std::ifstream f(fp);
            if (!f.is_open()) continue;
            std::ostringstream ss; ss << f.rdbuf();
            f.close();

            json payload;
            try {
                payload = json::parse(ss.str());
            } catch (const json::parse_error& e) {
                std::cerr << "[outbound] invalid JSON, deleting: "
                          << fp.filename() << "  (" << e.what() << ")\n";
                std::error_code ec; fs::remove(fp, ec);
                continue;
            }

            // Stamp send time at moment of publish.
            payload["gsm_send_time_ns"] = wall_time_ns();
            const std::string body = payload.dump();

            try {
                std::lock_guard<std::mutex> lk(g_publish_mtx);
                if (!g_mqtt || !g_mqtt->is_connected()) {
                    std::cerr << "[outbound] not connected, leaving "
                              << fp.filename() << " for later\n";
                    break;
                }
                auto msg = mqtt::make_message(TOPIC_THRUSTER,
                                               body, MQTT_QOS, false);
                g_mqtt->publish(msg)->wait_for(std::chrono::seconds(5));
                ++g_total_outbound_sent;
                std::error_code ec; fs::remove(fp, ec);
                std::cout << "[outbound] sent + deleted: "
                          << fp.filename() << "\n";
            } catch (const mqtt::exception& e) {
                std::cerr << "[outbound] publish failed "
                          << fp.filename() << ": " << e.what() << "\n";
                break;
            }
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

        const double cutoff = now_seconds() - JSON_TTL_SECONDS;
        int deleted = 0;
        try {
            for (const auto& type_entry : fs::directory_iterator(SAVE_DIR)) {
                if (!type_entry.is_directory()) continue;
                for (const auto& obj_entry :
                     fs::directory_iterator(type_entry.path())) {
                    if (!obj_entry.is_directory()) continue;
                    for (const auto& f :
                         fs::directory_iterator(obj_entry.path())) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().extension() != ".json") continue;
                        try {
                            if (file_mtime_seconds(f.path()) < cutoff) {
                                fs::remove(f.path());
                                ++deleted;
                            }
                        } catch (...) {}
                    }
                }
            }
        } catch (const std::exception& e) {
            // SAVE_DIR may not exist briefly at startup — ignore.
        }
        if (deleted > 0) {
            std::cout << "[cleanup] deleted " << deleted
                      << " stale JSON file(s) (>" << JSON_TTL_SECONDS
                      << "s)\n";
        }
    }
}

// ──────────────────────────────────────────────────────────────
// STATS THREAD
// ──────────────────────────────────────────────────────────────

void stats_thread_fn()
{
    uint64_t last_total = 0;
    while (g_running.load()) {
        for (int i = 0; i < STATS_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const uint64_t total = g_total_received.load();
        const double rate = static_cast<double>(total - last_total)
                             / STATS_INTERVAL_S;
        last_total = total;

        std::vector<std::pair<std::string, uint64_t>> top;
        std::size_t n_topics = 0;
        {
            std::lock_guard<std::mutex> lk(g_stats_mtx);
            n_topics = g_topic_counters.size();
            top.reserve(g_topic_counters.size());
            for (auto& kv : g_topic_counters) top.emplace_back(kv);
        }
        std::sort(top.begin(), top.end(),
                  [](const auto& a, const auto& b){ return a.second > b.second; });
        if (top.size() > 5) top.resize(5);

        std::ostringstream oss;
        oss << "[stats] received=" << total
            << " (" << rate << "/s)"
            << " invalid="  << g_total_invalid.load()
            << " written="  << g_total_written.load()
            << " outbound=" << g_total_outbound_sent.load()
            << " topics="   << n_topics;
        if (!top.empty()) {
            oss << "  top:";
            for (auto& [t, n] : top) {
                const auto pos = t.find_last_of('/');
                const std::string name =
                    (pos == std::string::npos) ? t : t.substr(pos + 1);
                oss << " " << name << "=" << n;
            }
        }
        std::cout << oss.str() << "\n";
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────

void install_signal_handlers()
{
    auto handler = [](int) {
        g_running.store(false);
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
              << " ground_station_manager.cpp v1\n"
              << " (replaces ground_station_manager.py)\n"
              << "================================================\n";

    fs::create_directories(SAVE_DIR);
    fs::create_directories(OUTPUT_DIR);
    install_signal_handlers();

    std::cout << "[main] broker:        " << BROKER_IP        << "\n"
              << "[main] sub topic:     " << TOPIC_TELEMETRY_WILD << "\n"
              << "[main] pub topic:     " << TOPIC_THRUSTER
              <<      "  (outbound, kept for later)\n"
              << "[main] save dir:      " << SAVE_DIR
              <<      "/{type}/{object_id}/\n"
              << "[main] output dir:    " << OUTPUT_DIR
              <<      "/  (outbound thrust vectors)\n"
              << "[main] json ttl:      " << JSON_TTL_SECONDS << " s\n"
              << "================================================\n";

    g_mqtt = std::make_unique<mqtt::async_client>(BROKER_IP, CLIENT_ID);

    GsmCallback cb;
    g_mqtt->set_callback(cb);

    mqtt::connect_options opts;
    opts.set_keep_alive_interval(20);
    opts.set_clean_session(true);
    opts.set_automatic_reconnect(true);
    opts.set_connect_timeout(std::chrono::seconds(10));

    int attempts = 0;
    while (g_running.load()) {
        try {
            g_mqtt->connect(opts)->wait();
            break;
        } catch (const mqtt::exception& e) {
            ++attempts;
            std::cerr << "[mqtt] connect attempt " << attempts
                      << " failed: " << e.what() << "\n";
            if (attempts >= 5) {
                std::cerr << "[mqtt] giving up — start mosquitto first.\n";
                return 1;
            }
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
    if (!g_running.load()) return 0;

    std::thread t_outbound(outbound_watchdog_thread_fn);
    std::thread t_cleanup (cleanup_thread_fn);
    std::thread t_stats   (stats_thread_fn);

    std::cout << "[main] running. press Ctrl-C to stop.\n";

    // Idle main thread; signal handler flips g_running.
    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutting down ...\n";
    t_outbound.join();
    t_cleanup.join();
    t_stats.join();

    try {
        if (g_mqtt && g_mqtt->is_connected())
            g_mqtt->disconnect()->wait();
    } catch (...) {}

    std::cout << "[main] clean exit. received=" << g_total_received
              << " written="  << g_total_written
              << " invalid="  << g_total_invalid
              << " outbound=" << g_total_outbound_sent << "\n";
    return 0;
}
