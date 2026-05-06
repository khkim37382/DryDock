// ============================================================
// ground_station_manager.cpp  —  v2  (session folders, ms timing)
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

static std::string SESSION_DIR;    
static std::string SESSION_JSON;   
static std::string SESSION_PARQUET;
static std::string SESSION_NAME;   

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

// Portable file modified time to epoch ms
inline double file_mtime_ms(const fs::path& p)
{
    auto ftime = fs::last_write_time(p);
    auto age = fs::file_time_type::clock::now() - ftime;
    return wall_time_ms() - std::chrono::duration<double, std::milli>(age).count();
}

// Local ISO-8601 string at millisecond resolution.
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
    if (payload.contains("object_bucket") && payload["object_bucket"].is_string()) {
        const std::string b = payload["object_bucket"].get<std::string>();
        if (b == "satellites" || b == "satellite") return "satellites";
        if (b == "asteroids"  || b == "asteroid")  return "asteroids";
        return "debris";
    }
    if (payload.contains("object_type") && payload["object_type"].is_string()) {
        const std::string t = payload["object_type"].get<std::string>();
        if (t == "satellite") return "satellites";
        if (t == "asteroid")  return "asteroids";
        return "debris";
    }
    return "debris"; 
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
        const double gsm_receive_ms      = wall_time_ms();
        const std::string gsm_local_iso  = local_iso_ms();
        const std::string& topic         = msg->get_topic();

        json payload;
        try { payload = json::parse(msg->get_payload_str()); }
        catch (...) { ++g_total_invalid; return; }

        {
            std::lock_guard<std::mutex> lk(g_stats_mutex);
            g_topic_counts[topic]++;
        }
        ++g_total_received;

        std::string oid;
        if (payload.contains("object_id") && payload["object_id"].is_string())
            oid = payload["object_id"].get<std::string>();
        if (oid.empty()) {
            const auto pos = topic.find_last_of('/');
            if (pos != std::string::npos && pos+1 < topic.size())
                oid = topic.substr(pos+1);
        }
        if (oid.empty()) { ++g_total_invalid; return; }

        const std::string bucket = resolve_bucket(payload, topic);

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

        payload["gsm"] = {
            {"gsm_receive_time_ms",    gsm_receive_ms},
            {"gsm_receive_local_iso_ms", gsm_local_iso},
            {"sms_to_gsm_trip_ms",
                have_sms_send ? json(gsm_receive_ms - sms_send_ms) : json(nullptr)},
            {"mqtt_topic",             topic},
            {"session",                SESSION_NAME},
        };

        const std::string safe_oid = safe_path_part(oid);
        fs::path obj_dir = fs::path(SESSION_JSON) / bucket / safe_oid;
        try { fs::create_directories(obj_dir); }
        catch (...) { return; }

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
// JSON CLEANUP THREAD  (3-minute TTL)
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
                            const double mtime = file_mtime_ms(f.path());
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
            std::cout << "[cleanup-json] deleted " << deleted << " JSON file(s) (>3 min)\n";
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
        const double   rate  = static_cast<double>(total - last_total) / STATS_INTERVAL_S;
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
            oss << " " << (slash==std::string::npos ? t : t.substr(slash+1)) << "=" << n;
        }
        std::cout << oss.str() << "\n";
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
              << "[main] json ttl:  3 min\n"
              << "================================================\n";

    init_session();

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

    std::thread t_cleanup(json_cleanup_thread_fn);
    std::thread t_stats  (stats_thread_fn);

    std::cout << "[main] running — Ctrl-C to stop\n";
    while (g_running.load())
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

    std::cout << "\n[main] shutting down ...\n";
    t_cleanup.join();
    t_stats.join();

    try { if (g_mqtt && g_mqtt->is_connected()) g_mqtt->disconnect()->wait(); }
    catch (...) {}

    return 0;
}