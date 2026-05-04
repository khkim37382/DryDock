# Satellite Constellation Telemetry Pipeline

End-to-end system for streaming satellite telemetry from a vpython
orbital simulation through a fully-C++ MQTT layer to a multi-threaded
C++ Parquet writer that builds an AI training dataset.

---

## Pipeline (one-way ingest)

```
satellite_orbit.py           Python (vpython sim, 10 Hz)
        |
        |  writes one combined JSON per tick
        v
ready_to_send_telemetry/     File queue, 5 hr TTL
        |
        v
dispatcher.cpp               C++ priority-queue scheduler
  - Splits combined JSON per object
  - LOS geometry to 6 ground stations + 3 TDRS relays
  - Per-orbit-class throttle (LEO 10Hz, MEO 5Hz, HEO 2-8Hz, etc.)
  - Stamps sms_send_time_ns + comm_path metadata
  - Publishes to satellite/telemetry/{object_id}
        |
        v
Mosquitto broker             One local broker, per-object topics
        |
        v
ground_station_manager.cpp   C++ MQTT bridge (replaces former .py)
  - Wildcard subscribe satellite/telemetry/+
  - Stamps gsm_receive_time_ns + sms_to_gsm_trip_ns
  - Atomic write to received_transmissions/{type}/{object_id}/ping_TS.json
        |
        v
received_transmissions/      JSON landing zone, 5 min TTL
        |
        v
ground_station_processor.cpp C++ multi-threaded ingest
  - Watcher + N ingest + flush + index + cleanup threads
  - Parses JSON, builds per-type rows (sat / asteroid / debris)
  - Buffers per object, flushes Parquet at 1000 rows or 60 s
        |
        v
training_data/{type}/{object_id}/batch_NNNNNN.parquet   5 hr TTL
training_data/{type}/master_index.parquet               Per-type summary
training_data/summary.parquet                           Top-level
```

---

## Files

| File | Language | Status | Purpose |
|---|---|---|---|
| `satellite_orbit.py` | Python (vpython) | active | Orbital sim, writes combined JSONs at 10 Hz; ground stations + TDRS visuals; exports `ground_network_config.json` |
| `dispatcher.cpp` | C++20 | active | Splits combined JSON, applies LOS geometry + throttle + propagation delay, publishes per-object MQTT topics |
| `ground_station_manager.cpp` | C++20 | active | MQTT wildcard subscribe + atomic JSON writes to per-object folders |
| `ground_station_processor.cpp` | C++20 | active | Multi-threaded JSON-to-Parquet processor (Apache Arrow) |
| `build_dispatcher.sh` | bash | helper | Builds `dispatcher.cpp` on macOS |
| `build_gsm_manager.sh` | bash | helper | Builds `ground_station_manager.cpp` on macOS |
| `build_processor.sh` | bash | helper | Builds `ground_station_processor.cpp` on macOS |
| `ground_station_processor.py` | Python | backup | Older Python version of the processor; kept as reference |
| `satellite_manager.py` | Python | reserve | Satellite-side MQTT bridge for FUTURE bidirectional support; not in active pipeline |

---

## One-time setup (macOS)

```bash
# 1. Mosquitto MQTT broker
brew install mosquitto
brew services start mosquitto

# 2. C++ libraries (covers all 3 binaries)
brew install eclipse-paho-mqtt-cpp apache-arrow nlohmann-json

# 3. Python — only needed for the simulation now
pip install vpython

# 4. Make build scripts executable
chmod +x build_dispatcher.sh build_gsm_manager.sh build_processor.sh

# 5. Build all three binaries
./build_dispatcher.sh    # produces ./dispatcher
./build_gsm_manager.sh   # produces ./ground_station_manager
./build_processor.sh     # produces ./ground_station_processor
```

You should now have three binaries in your folder:
`dispatcher`, `ground_station_manager`, `ground_station_processor`.

---

## Configuration

Default broker is `tcp://localhost:1883`. If your broker runs elsewhere,
edit the `BROKER_IP` constant near the top of:

- `dispatcher.cpp`
- `ground_station_manager.cpp`

and rebuild with the matching `build_*.sh` script.

---

## Run order

Open 5 terminals in `project_root/` and start in this order:

```bash
# Terminal 1 — broker
mosquitto -v
# (skip if you used `brew services start mosquitto` — it's already running)

# Terminal 2 — simulation
python satellite_orbit.py

# Terminal 3 — dispatcher (publishes per-object topics)
./dispatcher

# Terminal 4 — GSM bridge (MQTT subscriber, writes JSONs)
./ground_station_manager

# Terminal 5 — processor (turns JSONs into Parquet)
./ground_station_processor
```

Stop any of them with Ctrl-C. They are decoupled via file queues —
you can stop and restart any single component without losing the
in-flight pipeline.

---

## Folder layout at runtime

```
project_root/
|-- satellite_orbit.py
|-- dispatcher                          (binary)
|-- ground_station_manager              (binary)
|-- ground_station_processor            (binary)
|-- ground_network_config.json          (sim writes on startup)
|-- quantum_commands.json               (external commands, optional)
|
|-- ready_to_send_telemetry/            (sim output, dispatcher consumes)
|     telemetry_20260504_*.json
|
|-- received_transmissions/             (GSM landing zone, 5 min TTL)
|     satellite/
|         SAT-1/
|             ping_1714000000000123456.json
|     asteroid/
|     debris/
|
|-- training_data/                      (Parquet, 5 hr TTL)
|     summary.parquet
|     satellites/
|         master_index.parquet
|         SAT-1/
|             batch_000001.parquet
|             batch_000002.parquet
|     asteroids/
|         master_index.parquet
|     debris/
|         master_index.parquet
|
|-- ready_to_send_transmissions/        (outbound thrust vectors,
|                                        kept for FUTURE bidirectional)
```

---

## Verify the pipeline is working

After running for ~1 minute:

```bash
# Sim is producing snapshots (most are deleted by dispatcher quickly)
ls ready_to_send_telemetry/ | wc -l

# GSM is receiving and writing per-object JSONs
ls received_transmissions/satellite/SAT-1/ | head

# Processor is writing Parquet batches
ls training_data/satellites/SAT-1/

# Read a batch
python3 -c "
import pandas as pd
df = pd.read_parquet('training_data/satellites/SAT-1/batch_000001.parquet')
print('Columns:', len(df.columns))
print('Rows:   ', len(df))
print(df[['object_id','sequence_id','altitude_km',
          'comm_link_type','current_uplink_trip_ms']].head())
"

# Read the master index
python3 -c "
import pandas as pd
print(pd.read_parquet('training_data/satellites/master_index.parquet'))
"
```

(`pandas` and `pyarrow` are only needed for these inspection commands,
not for running the pipeline. Install with `pip install pandas pyarrow`
when you want to inspect the data.)

---

## Where the timing logic lives

**10 Hz cadence** comes from `BASE_TELEMETRY_SAMPLE_HZ = 10.0`
inside `satellite_orbit.py`.

**Decision to send**, in order, all in `dispatcher.cpp`:

1. **Throttle gate** (`process_snapshot` -> `throttle_for(...)`):
   skips an object if too little time has passed since its last publish.
2. **Geometry gate** (`find_comm_path(...)`):
   tries direct ground-station LOS first, then TDRS relay; if neither
   is visible, parks the payload in `g_blackout` until the next snapshot.
3. **Release-time gate** (priority queue):
   `release_time = now + propagation_delay_ms` — scheduler thread
   sleeps until then, then publishes.

Only when all three gates pass does the message hit MQTT.

**Roundtrip / link-latency calculation** lives in
`ground_station_processor.cpp` -> `append_common(...)`. It reads
fields the dispatcher and GSM already stamped (`sms_to_gsm_trip_ns`,
`sms_send_time_ns`) — never recomputes geometry.

---

## Terminology

- **MQTT broker** = Mosquitto server. Exactly one, on localhost.
  Not geographically distributed.
- **Ground stations** = simulated NASA / ESA Earth stations
  (White Sands, Guam, Kiruna, Santiago, Dongara, North Pole).
  Used by the dispatcher for LOS geometry, NOT MQTT brokers.
- **TDRS relays** = simulated geostationary relays (East 41W,
  West 171W, Guam 174E). Geometry only.

---

## Object types and bucket routing

The simulation's drydock dataset-backed schema produces several
object sub-types. They all collapse into **three canonical buckets**
for filesystem and Parquet routing:

| Sub-type from sim                  | Bucket    | Notes |
|---|---|---|
| `satellite`                        | satellite | Dataset-backed (NORAD-XXXXX) or generated SAT-N |
| `asteroid`                         | asteroid  | Legacy synthetic asteroids only |
| `debris`                           | debris    | Dataset-backed debris hazards |
| `generated_debris_fragment`        | debris    | Dynamically spawned during collisions |
| `debris_debris_fragmentation`      | debris    | Same family |
| `satellite_or_object_breakup`      | debris    | Same family |

The pipeline trusts each item's `"type"` field, NOT the parent JSON
array name. Dispatcher stamps a separate `object_bucket` field on the
outbound payload so GSM and processor can route without re-deriving.

This means dynamically-spawned fragments automatically:
- get a per-object MQTT topic `satellite/telemetry/<fragment_id>`
- land in `received_transmissions/debris/<fragment_id>/`
- accumulate into `training_data/debris/<fragment_id>/batch_NNNNNN.parquet`
- show up in `training_data/debris/master_index.parquet`

No code change is needed when new fragments appear.

---

## Future bidirectional work

When you want to send thrust commands back to the satellite side:

1. Activate `satellite_manager.py` (in this bundle as reserve)
2. Have a decision script drop JSON files into
   `ready_to_send_transmissions/`
3. The C++ GSM manager's outbound watchdog publishes them to
   `satellite/thruster` automatically — already wired up
4. `satellite_manager.py` receives, saves to `received_commands/`,
   and a burn-execution script reads from there

The plumbing is in place; just turn it on.
