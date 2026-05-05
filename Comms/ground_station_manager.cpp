// ============================================================
// ground_station_manager.cpp  —  v2  (session folders, ms timing)
// ============================================================
//
// WHAT THIS FILE DOES
// ───────────────────
// Receives per-object MQTT mini-payloads from the dispatcher on
// satellite/telemetry/+ and writes them to disk for the processor.
//
// SESSION FOLDER
// ──────────────
// On startup, a session folder is created named with the GSM's own
// local clock at initialization time:
//
//   received_transmissions/
//     2026-05-05T12-43-05/           ← GSM init time
//       json/                        ← 3-min TTL, then deleted
//         satellites/NORAD-20580/ping_*.json
//         asteroids/AST-001/ping_*.json
//         debris/NORAD-33757/ping_*.json
//       parquet/                     ← permanent, written by processor
//         satellites/...
//         asteroids/...
//         debris/...
//         summary.parquet
//       json_index.parquet           ← one row per JSON file (updated lazily)
//
// The session folder name is the GSM's clock (not the SMS's T0),
// because the GSM might start at a different time than the SMS.
// The SMS's T0 is embedded inside every mini-payload's packet{} block.
//
// BUCKET MAPPING  (3 buckets, matches dispatcher)
// ────────────────────────────────────────────────
//   object_bucket == "satellites"  → json/satellites/{object_id}/
//   object_bucket == "asteroids"   → json/asteroids/{object_id}/
//   object_bucket == "debris"      → json/debris/{object_id}/
//   (anything else)                → json/debris/{object_id}/  (safe fallback)
//
// TIMING  (all milliseconds)
// ──────────────────────────
//   sms_send_time_ms   — read from payload["dispatcher"]["sms_send_time_ms"]
//   gsm_receive_time_ms — stamped here at MQTT callback entry
//   sms_to_gsm_trip_ms — gsm_receive_time_ms - sms_send_time_ms
//   gsm_receive_local_iso_ms — GSM local wall clock at receive time
//
// JSON TTL  (3 minutes)
// ─────────────────────
// A cleanup_thread sweeps the json/ subdirectory every 30 seconds and
// deletes files older than 3 minutes.  The parquet/ subdirectory is
// NEVER cleaned — Parquet is permanent.
//
// OUTBOUND WATCHDOG  (commented out — one-way ingest only)
// ─────────────────────────────────────────────────────────
// Preserved for future bidirectional use. Uncomment in main() and
// the companion thread launch to activate.
//
// macOS BUILD  (Apple Silicon and Intel)
// ──────────────────────────────────────
//   brew install eclipse-paho-mqtt-cpp nlohmann-json
//   ./build_gsm_manager.sh
//   ./ground_station_manager --broker tcp://BROKER_IP:1883
// ============================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <unordered_map>
#include <thread>
#include <mutex>
#include <atomic>
#include <chrono>
#include <filesystem>
#include <algorithm>
#include <csignal>
#include <ctime>
#include <iomanip>

#include <nlohmann/json.hpp>
#include <mqtt/async_client.h>

namespace fs  = std::filesystem;
using json    = nlohmann::json;
using SysClock = std::chrono::system_clock;

// ──────────────────────────────────────────────────────────────
// CONFIG
// ──────────────────────────────────────────────────────────────

static std::string BROKER_IP                = "tcp://localhost:1883";
static const std::string CLIENT_ID          = "gsm_manager_cpp_v2";
static const std::string TOPIC_WILD         = "satellite/telemetry/+";
static const std::string TOPIC_THRUSTER     = "satellite/thruster"; // outbound (inactive)
static const int         MQTT_QOS           = 1;

// Root of received transmissions.
static const std::string ROOT_DIR           = "received_transmissions";

// Outbound dir (kept for future bidirectional use).
static const std::string OUTPUT_DIR         = "ready_to_send_transmissions";

// JSON files in json/ are deleted after 3 minutes.
static const double JSON_TTL_MS             = 180'000.0;
static const int    CLEANUP_INTERVAL_S      = 30;
static const int    OUTBOUND_POLL_MS        = 100;
static const int    STATS_INTERVAL_S        = 10;

// ──────────────────────────────────────────────────────────────
// SESSION — created once at startup
// ──────────────────────────────────────────────────────────────

static std::string SESSION_DIR;    // e.g. received_transmissions/2026-05-05T12-43-05
static std::string SESSION_JSON;   // SESSION_DIR/json
static std::string SESSION_PARQUET;// SESSION_DIR/parquet (processor writes here)
static std::string SESSION_NAME;   // just the datetime part e.g. 2026-05-05T12-43-05

// ──────────────────────────────────────────────────────────────
// GLOBAL STATE
// ──────────────────────────────────────────────────────────────

std::unique_ptr<mqtt::async_client> g_mqtt;
std::mutex g_publish_mutex;

std::unordered_map<std::string, uint64_t> g_topic_counts;
std::mutex g_stats_mutex;
std::atomic<uint64_t> g_total_received{0};
std::atomic<uint64_t> g_total_written{0};
std::atomic<uint64_t> g_total_invalid{0};
std::atomic<uint64_t> g_total_outbound{0};

std::atomic<bool> g_running{true};

// ──────────────────────────────────────────────────────────────
// TIME HELPERS
// ──────────────────────────────────────────────────────────────

inline double wall_time_ms()
{
    return std::chrono::duration<double, std::milli>(
        SysClock::now().time_since_epoch()).count();
}

// Local ISO-8601 string at millisecond resolution.
// Format: 2026-05-05T12:43:05.123-05:00
std::string local_iso_ms()
{
    using namespace std::chrono;
    const auto now_ms   = static_cast<int64_t>(wall_time_ms());
    const time_t t      = static_cast<time_t>(now_ms / 1000LL);
    const int ms_part   = static_cast<int>(now_ms % 1000LL);
    std::tm local_tm{}, gmt_tm{};
#if defined(_WIN32)
    localtime_s(&local_tm, &t);
    gmtime_s(&gmt_tm, &t);
#else
    localtime_r(&t, &local_tm);
    gmtime_r(&t, &gmt_tm);
#endif
    std::tm lc2 = local_tm, gc2 = gmt_tm;
    const int offset_min = static_cast<int>(
        (std::mktime(&lc2) - std::mktime(&gc2)) / 60);
    const int oh = std::abs(offset_min) / 60;
    const int om = std::abs(offset_min) % 60;
    char buf[64];
    std::snprintf(buf, sizeof(buf),
        "%04d-%02d-%02dT%02d:%02d:%02d.%03d%+03d:%02d",
        local_tm.tm_year+1900, local_tm.tm_mon+1, local_tm.tm_mday,
        local_tm.tm_hour, local_tm.tm_min, local_tm.tm_sec,
        ms_part,
        (offset_min < 0 ? -oh : oh), om);
    return buf;
}

// Filesystem-safe datetime string (colons → dashes).
// e.g. "2026-05-05T12:43:05.123-05:00" → "2026-05-05T12-43-05"
std::string safe_datetime_for_path()
{
    using namespace std::chrono;
    const time_t t = SysClock::to_time_t(SysClock::now());
    std::tm local_tm{};
#if defined(_WIN32)
    localtime_s(&local_tm, &t);
#else
    localtime_r(&t, &local_tm);
#endif
    char buf[32];
    std::snprintf(buf, sizeof(buf),
        "%04d-%02d-%02dT%02d-%02d-%02d",
        local_tm.tm_year+1900, local_tm.tm_mon+1, local_tm.tm_mday,
        local_tm.tm_hour, local_tm.tm_min, local_tm.tm_sec);
    return buf;
}

// ──────────────────────────────────────────────────────────────
// SESSION INIT
// ──────────────────────────────────────────────────────────────
void init_session()
{
    SESSION_NAME    = safe_datetime_for_path();
    SESSION_DIR     = ROOT_DIR + "/" + SESSION_NAME;
    SESSION_JSON    = SESSION_DIR + "/json";
    SESSION_PARQUET = SESSION_DIR + "/parquet";

    fs::create_directories(SESSION_JSON    + "/satellites");
    fs::create_directories(SESSION_JSON    + "/asteroids");
    fs::create_directories(SESSION_JSON    + "/debris");
    fs::create_directories(SESSION_PARQUET + "/satellites");
    fs::create_directories(SESSION_PARQUET + "/asteroids");
    fs::create_directories(SESSION_PARQUET + "/debris");
    fs::create_directories(OUTPUT_DIR);

    std::cout << "[session] dir:     " << SESSION_DIR     << "\n"
              << "[session] json:    " << SESSION_JSON     << "/{bucket}/{object_id}/\n"
              << "[session] parquet: " << SESSION_PARQUET  << "/{bucket}/\n";
}

// ──────────────────────────────────────────────────────────────
// BUCKET RESOLUTION
// ──────────────────────────────────────────────────────────────
inline std::string resolve_bucket(const json& payload, const std::string& topic)
{
    // Prefer dispatcher-stamped object_bucket field.
    if (payload.contains("object_bucket") && payload["object_bucket"].is_string()) {
        const std::string b = payload["object_bucket"].get<std::string>();
        if (b == "satellites" || b == "satellite") return "satellites";
        if (b == "asteroids"  || b == "asteroid")  return "asteroids";
        return "debris";
    }
    // Fall back to object_type field.
    if (payload.contains("object_type") && payload["object_type"].is_string()) {
        const std::string t = payload["object_type"].get<std::string>();
        if (t == "satellite") return "satellites";
        if (t == "asteroid")  return "asteroids";
        return "debris";
    }
    return "debris"; // safe fallback
}

// ──────────────────────────────────────────────────────────────
// SAFE PATH COMPONENT
// ──────────────────────────────────────────────────────────────
std::string safe_path_part(const std::string& s)
{
    if (s.empty()) return "unknown";
    static const std::string bad = "/\\:*?\"<>|";
    std::string out; out.reserve(s.size());
    for (char c : s) {
        if (c == '\0' || (unsigned char)c < 0x20 || bad.find(c) != std::string::npos)
            out.push_back('_');
        else
            out.push_back(c);
    }
    return out;
}

// ──────────────────────────────────────────────────────────────
// ATOMIC FILE WRITE
// ──────────────────────────────────────────────────────────────
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
        if (ec) { fs::remove(tmp, ec); return false; }
        return true;
    } catch (...) { return false; }
}

// ──────────────────────────────────────────────────────────────
// MQTT CALLBACK
// ──────────────────────────────────────────────────────────────
class GsmCallback : public virtual mqtt::callback
{
public:
    void connected(const std::string& cause) override
    {
        std::cout << "[mqtt] connected (" << cause
                  << "), subscribing to " << TOPIC_WILD << "\n";
        try { g_mqtt->subscribe(TOPIC_WILD, MQTT_QOS)->wait(); }
        catch (const mqtt::exception& e) {
            std::cerr << "[mqtt] subscribe failed: " << e.what() << "\n";
        }
    }

    void connection_lost(const std::string& cause) override
    {
        std::cerr << "[mqtt] connection lost: " << cause
                  << " (auto-reconnect active)\n";
    }

    void message_arrived(mqtt::const_message_ptr msg) override
    {
        // Stamp receive time immediately.
        const double gsm_receive_ms      = wall_time_ms();
        const std::string gsm_local_iso  = local_iso_ms();
        const std::string& topic         = msg->get_topic();

        json payload;
        try { payload = json::parse(msg->get_payload_str()); }
        catch (...) {
            ++g_total_invalid;
            return;
        }

        // ── per-topic stats ──────────────────────────────────────
        {
            std::lock_guard<std::mutex> lk(g_stats_mutex);
            g_topic_counts[topic]++;
        }
        ++g_total_received;

        // ── object identity ─────────────────────────────────────
        std::string oid;
        if (payload.contains("object_id") && payload["object_id"].is_string())
            oid = payload["object_id"].get<std::string>();
        if (oid.empty()) {
            // Fall back: extract from topic satellite/telemetry/{object_id}
            const auto pos = topic.find_last_of('/');
            if (pos != std::string::npos && pos+1 < topic.size())
                oid = topic.substr(pos+1);
        }
        if (oid.empty()) { ++g_total_invalid; return; }

        // ── bucket ──────────────────────────────────────────────
        const std::string bucket = resolve_bucket(payload, topic);

        // ── timing stamp ─────────────────────────────────────────
        // Read sms_send_time_ms from dispatcher block.
        double sms_send_ms   = 0.0;
        bool   have_sms_send = false;
        try {
            if (payload.contains("dispatcher") &&
                payload["dispatcher"].is_object() &&
                payload["dispatcher"].contains("sms_send_time_ms") &&
                payload["dispatcher"]["sms_send_time_ms"].is_number()) {
                sms_send_ms   = payload["dispatcher"]["sms_send_time_ms"].get<double>();
                have_sms_send = true;
            }
        } catch (...) {}

        // Stamp GSM receive timing onto payload.
        payload["gsm"] = {
            {"gsm_receive_time_ms",    gsm_receive_ms},
            {"gsm_receive_local_iso_ms", gsm_local_iso},
            {"sms_to_gsm_trip_ms",
                have_sms_send ? json(gsm_receive_ms - sms_send_ms) : json(nullptr)},
            {"mqtt_topic",             topic},
            {"session",                SESSION_NAME},
        };

        // ── write JSON atomically ────────────────────────────────
        const std::string safe_oid = safe_path_part(oid);
        fs::path obj_dir = fs::path(SESSION_JSON) / bucket / safe_oid;
        try { fs::create_directories(obj_dir); }
        catch (...) { return; }

        // Filename: ms timestamp guarantees uniqueness at 40 Hz.
        char fname[64];
        std::snprintf(fname, sizeof(fname), "ping_%020lld.json",
                      static_cast<long long>(gsm_receive_ms));
        const fs::path target = obj_dir / fname;
        const std::string body = payload.dump();

        if (atomic_write_json(target, body)) {
            ++g_total_written;
        }
    }

    void delivery_complete(mqtt::delivery_token_ptr) override {}
};

// ──────────────────────────────────────────────────────────────
// JSON CLEANUP THREAD  (3-minute TTL on json/ subdir)
// ──────────────────────────────────────────────────────────────
void json_cleanup_thread_fn()
{
    while (g_running.load()) {
        for (int i = 0; i < CLEANUP_INTERVAL_S && g_running.load(); ++i)
            std::this_thread::sleep_for(std::chrono::seconds(1));
        if (!g_running.load()) break;

        const double cutoff_ms = wall_time_ms() - JSON_TTL_MS;
        int deleted = 0;
        try {
            for (const auto& bucket_entry : fs::directory_iterator(SESSION_JSON)) {
                if (!bucket_entry.is_directory()) continue;
                for (const auto& obj_entry : fs::directory_iterator(bucket_entry)) {
                    if (!obj_entry.is_directory()) continue;
                    for (const auto& f : fs::directory_iterator(obj_entry)) {
                        if (!f.is_regular_file()) continue;
                        if (f.path().extension() != ".json") continue;
                        try {
                            // Use file mtime (ms since epoch).
                            const double mtime = std::chrono::duration<double, std::milli>(
                                fs::file_clock::to_sys(fs::last_write_time(f.path()))
                                .time_since_epoch()).count();
                            if (mtime < cutoff_ms) {
                                std::error_code ec;
                                fs::remove(f.path(), ec);
                                if (!ec) ++deleted;
                            }
                        } catch (...) {}
                    }
                }
            }
        } catch (...) {}
        if (deleted > 0)
            std::cout << "[cleanup-json] deleted " << deleted
                      << " JSON file(s) (>3 min)\n";
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
        const double   rate  = static_cast<double>(total - last_total)
                               / STATS_INTERVAL_S;
        last_total = total;

        std::vector<std::pair<std::string,uint64_t>> top;
        std::size_t n_topics = 0;
        {
            std::lock_guard<std::mutex> lk(g_stats_mutex);
            n_topics = g_topic_counts.size();
            for (auto& kv : g_topic_counts) top.emplace_back(kv);
        }
        std::sort(top.begin(), top.end(),
                  [](const auto& a, const auto& b){ return a.second > b.second; });
        if (top.size() > 5) top.resize(5);

        std::ostringstream oss;
        oss << "[stats] received=" << total
            << " (" << static_cast<int>(rate) << "/s)"
            << " written=" << g_total_written.load()
            << " invalid=" << g_total_invalid.load()
            << " topics=" << n_topics;
        for (auto& [t, n] : top) {
            const auto slash = t.find_last_of('/');
            oss << " " << (slash==std::string::npos ? t : t.substr(slash+1))
                << "=" << n;
        }
        std::cout << oss.str() << "\n";
    }
}

// ──────────────────────────────────────────────────────────────
// OUTBOUND WATCHDOG  (INACTIVE — one-way ingest only)
// ──────────────────────────────────────────────────────────────
// Uncomment the thread launch in main() when bidirectional is ready.
void outbound_watchdog_thread_fn()
{
    // *** NOT STARTED IN main() — preserved for future use ***
    while (g_running.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(OUTBOUND_POLL_MS));
        if (!g_running.load()) break;
        std::vector<fs::path> files;
        try {
            for (const auto& e : fs::directory_iterator(OUTPUT_DIR)) {
                if (!e.is_regular_file()) continue;
                if (e.path().extension() != ".json") continue;
                const std::string n = e.path().filename().string();
                if (n.find(".tmp") != std::string::npos) continue;
                files.push_back(e.path());
            }
        } catch (...) { continue; }
        if (files.empty()) continue;
        std::sort(files.begin(), files.end(),
                  [](const fs::path& a, const fs::path& b){
                      return a.filename().string() < b.filename().string();
                  });
        for (const auto& fp : files) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            std::ifstream f(fp); if (!f.is_open()) continue;
            std::ostringstream ss; ss << f.rdbuf(); f.close();
            json payload;
            try { payload = json::parse(ss.str()); }
            catch (...) { std::error_code ec; fs::remove(fp, ec); continue; }
            payload["gsm_send_time_ms"] = wall_time_ms();
            try {
                std::lock_guard<std::mutex> lk(g_publish_mutex);
                if (!g_mqtt || !g_mqtt->is_connected()) break;
                auto m = mqtt::make_message(TOPIC_THRUSTER, payload.dump(), MQTT_QOS, false);
                g_mqtt->publish(m)->wait_for(std::chrono::seconds(5));
                ++g_total_outbound;
                std::error_code ec; fs::remove(fp, ec);
            } catch (...) { break; }
        }
    }
}

// ──────────────────────────────────────────────────────────────
// SIGNALS
// ──────────────────────────────────────────────────────────────
void install_signal_handlers()
{
    auto h = [](int){ g_running.store(false); };
    std::signal(SIGINT,  h);
    std::signal(SIGTERM, h);
}

// ──────────────────────────────────────────────────────────────
// MAIN
// ──────────────────────────────────────────────────────────────
// Usage:
//   ./ground_station_manager                                    # localhost
//   ./ground_station_manager --broker tcp://192.168.1.50:1883  # remote
int main(int argc, char* argv[])
{
    for (int i = 1; i < argc-1; ++i) {
        if (std::string(argv[i]) == "--broker") {
            BROKER_IP = argv[i+1];
            if (BROKER_IP.find("://") == std::string::npos)
                BROKER_IP = "tcp://" + BROKER_IP;
            ++i;
        }
    }

    install_signal_handlers();

    std::cout << "================================================\n"
              << " ground_station_manager.cpp v2  —  GSM Bridge\n"
              << "================================================\n"
              << "[main] broker:    " << BROKER_IP << "\n"
              << "[main] subscribe: " << TOPIC_WILD << "\n"
              << "[main] mode:      ONE-WAY INGEST\n"
              << "[main] json ttl:  3 min\n"
              << "[main] parquet:   permanent (written by processor)\n"
              << "================================================\n";

    // Create session folder structure.
    init_session();

    // Connect MQTT.
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
        try { g_mqtt->connect(opts)->wait(); break; }
        catch (const mqtt::exception& e) {
            ++attempts;
            std::cerr << "[mqtt] attempt " << attempts << " failed: " << e.what() << "\n";
            if (attempts >= 5) { std::cerr << "[mqtt] giving up\n"; return 1; }
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
    if (!g_running.load()) return 0;

    // std::thread t_outbound(outbound_watchdog_thread_fn);  // INACTIVE
    std::thread t_cleanup(json_cleanup_thread_fn);
    std::thread t_stats  (stats_thread_fn);

    std::cout << "[main] running — Ctrl-C to stop\n";
    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutting down ...\n";
    t_cleanup.join();
    t_stats.join();
    // t_outbound.join();  // INACTIVE

    try { if (g_mqtt && g_mqtt->is_connected()) g_mqtt->disconnect()->wait(); }
    catch (...) {}

    std::cout << "[main] clean exit. received=" << g_total_received
              << " written=" << g_total_written
              << " invalid=" << g_total_invalid << "\n";
    return 0;
}
