# Satellite Constellation Telemetry Pipeline



######
Instuctions for person running SMS:

cd ~/comms
chmod +x run_sms.sh
./run_sms.sh


Instructions for person running GSM:

cd ~/comms
chmod +x run_gsm.sh
./run_gsm.sh





A real-time telemetry pipeline that streams orbital simulation data from a simulation computer, through an MQTT broker, to a ground station computer that stores everything as Parquet files for AI training and analysis.

---

## What This Is

A satellite orbital simulation runs on one Mac and produces telemetry data at up to 40 Hz. A C++ dispatcher reads that data, figures out which satellites can actually communicate with ground stations based on line-of-sight geometry, simulates the radio propagation delay, and publishes each satellite's data to its own MQTT channel. A ground station Mac subscribes to all those channels, receives the data, and stores it in an organized folder structure as both raw JSON logs and compressed Parquet files ready for analysis.

---

## The Three Machines

| Machine | Role | OS |
|---|---|---|
| SMS Mac | Runs the simulation + dispatcher | macOS |
| Dell Windows | Runs the MQTT broker (message router) | Windows |
| GSM Mac | Receives telemetry + stores data | macOS |

The Dell sits in the middle and routes messages between the two Macs. It does nothing else.

---

## Files in This Bundle

| File | Runs On | Purpose |
|---|---|---|
| `satellite_orbit.py` | SMS Mac | The orbital simulation (vpython) |
| `dispatcher.cpp` | SMS Mac | Reads sim output, routes per-satellite MQTT messages |
| `build_dispatcher.sh` | SMS Mac | Builds the dispatcher binary |
| `ground_station_manager.cpp` | GSM Mac | Receives MQTT messages, writes JSON to disk |
| `build_gsm_manager.sh` | GSM Mac | Builds the GSM manager binary |
| `ground_station_processor.cpp` | GSM Mac | Converts JSON files to Parquet training data |
| `build_processor.sh` | GSM Mac | Builds the processor binary |
| `satellite_manager.py` | SMS Mac | Reserved for future two-way communication (not active) |
| `ground_station_processor.py` | — | Older Python version, kept as reference only |

---

## One-Time Setup

### Dell Windows — MQTT Broker

**1. Install Mosquitto**

Download from https://mosquitto.org/download and run the installer. Default install path is `C:\Program Files\mosquitto\`.

**2. Configure it to accept remote connections**

Open `C:\Program Files\mosquitto\mosquitto.conf` as Administrator and replace everything with:

```
listener 1883
allow_anonymous true
log_type all
log_dest stdout
log_timestamp true
```

**3. Open the firewall**

Run PowerShell as Administrator:

```powershell
New-NetFirewallRule -DisplayName "Mosquitto MQTT" -Direction Inbound -Protocol TCP -LocalPort 1883 -Action Allow
```

**4. Find the Dell's IP address** — you will need this for both Macs:

```powershell
ipconfig
```

Look for the `IPv4 Address` under your active network adapter. Write it down, e.g. `192.168.1.50`.

---

### SMS Mac — Simulation + Dispatcher

**1. Install dependencies**

```bash
brew install eclipse-paho-mqtt-cpp nlohmann-json
pip install vpython
```

**2. Place these files in one folder:**

```
project_sms/
├── satellite_orbit.py
├── dispatcher.cpp
├── build_dispatcher.sh
├── celestrak_ucs_orbit_dataset.json
└── celestrak_debris_dataset_with_estimated_masses.json
```

**3. Build the dispatcher**

```bash
chmod +x build_dispatcher.sh
./build_dispatcher.sh
```

You should see: `Build OK: ./dispatcher`

**4. Test that you can reach the broker**

```bash
nc -zv 192.168.1.50 1883
# Should print: Connection to 192.168.1.50 port 1883 succeeded
```

---

### GSM Mac — Ground Station

**1. Install dependencies**

```bash
brew install eclipse-paho-mqtt-cpp apache-arrow nlohmann-json
pip install pandas pyarrow
```

`pandas` and `pyarrow` are only needed for inspecting the Parquet output. The pipeline itself does not require Python on the GSM side.

**2. Place these files in one folder:**

```
project_gsm/
├── ground_station_manager.cpp
├── build_gsm_manager.sh
├── ground_station_processor.cpp
└── build_processor.sh
```

**3. Build both binaries**

```bash
chmod +x build_gsm_manager.sh build_processor.sh
./build_gsm_manager.sh
./build_processor.sh
```

You should see two new binaries: `ground_station_manager` and `ground_station_processor`.

**4. Test that you can reach the broker**

```bash
nc -zv 192.168.1.50 1883
```

---

## Running Everything

Start in this order. Each step should be running before you start the next.

**Step 1 — Dell: Start the broker**

Open Command Prompt as Administrator:

```cmd
"C:\Program Files\mosquitto\mosquitto.exe" -c "C:\Program Files\mosquitto\mosquitto.conf" -v
```

Leave this window open. You will see every connection, subscription, and message flowing through here in real time.

**Step 2 — SMS Mac: Start the simulation** (Terminal 1)

```bash
cd project_sms
python satellite_orbit.py
```

A vpython window opens showing the orbital simulation. Wait until you see it running and the terminal confirms it wrote `ground_network_config.json`.

**Step 3 — SMS Mac: Start the dispatcher** (Terminal 2)

```bash
cd project_sms
./dispatcher --broker tcp://192.168.1.50:1883
```

Replace `192.168.1.50` with your Dell's actual IP. You should see `[mqtt] connected` and then stats printing every 10 seconds.

**Step 4 — GSM Mac: Start the GSM manager** (Terminal 1)

```bash
cd project_gsm
./ground_station_manager --broker tcp://192.168.1.50:1883
```

You should see it connect and subscribe to `satellite/telemetry/+`. After a few seconds the stats line will show messages being received and written to disk.

**Step 5 — GSM Mac: Start the processor** (Terminal 2)

```bash
cd project_gsm
./ground_station_processor
```

Start this after the GSM manager so the session folder already exists. After about 60 seconds you will see Parquet batches being written.

---

## Stop Order

Always stop in reverse:

```
Ctrl-C  ground_station_processor   (finishes writing any open Parquet batches)
Ctrl-C  ground_station_manager
Ctrl-C  dispatcher
Ctrl-C  satellite_orbit.py
Ctrl-C  mosquitto
```

---

## Where Your Data Goes

Every time you start the GSM manager it creates a new session folder named with the current date and time:

```
received_transmissions/
  2026-05-05T12-43-05/
    json/                          <- Raw incoming data (deleted after 3 minutes)
      satellites/
        NORAD-20580/
          ping_1746372185123.json
      asteroids/
      debris/
    parquet/                       <- Permanent training data (never deleted)
      satellites/
        NORAD-20580/
          batch_000001.parquet
          batch_000002.parquet
        master_index.parquet
      asteroids/
        master_index.parquet
      debris/
        master_index.parquet
      summary.parquet
    json_index.parquet             <- Table of contents for all JSON received
```

JSON files are temporary — they exist for 3 minutes and are then automatically deleted. They are just a buffer between the MQTT receiver and the Parquet writer.

Parquet files are permanent and never deleted. Each session's data stays in its own timestamped folder indefinitely.

---

## Reading the Parquet Data

From the GSM Mac or any machine with Python:

```python
import pandas as pd
import glob, os

# See what sessions exist
sessions = os.listdir('received_transmissions')
print(sessions)

# Read all satellite batches from a session
session = '2026-05-05T12-43-05'
files = glob.glob(f'received_transmissions/{session}/parquet/satellites/*/batch_*.parquet')
df = pd.concat([pd.read_parquet(f) for f in files])

# Key columns
print(df[['object_id', 'universe_time_ms', 'elapsed_ms_since_t0',
          'sms_to_gsm_trip_ms', 'comm_link_type',
          'pos_x_m', 'pos_y_m', 'pos_z_m']].head())

# What objects are in this session and how much data
index = pd.read_parquet(f'received_transmissions/{session}/parquet/satellites/master_index.parquet')
print(index[['object_id', 'total_records', 'avg_trip_ms']])

# High-level summary across all buckets
summary = pd.read_parquet(f'received_transmissions/{session}/parquet/summary.parquet')
print(summary)

# Full audit trail of every JSON file received
json_idx = pd.read_parquet(f'received_transmissions/{session}/json_index.parquet')
print(f"Total transmissions received: {len(json_idx)}")
```

---

## Key Timing Fields

Every Parquet row has these timing fields for understanding transmission latency:

| Field | Description |
|---|---|
| `t0_local_clock_iso_ms` | When the SMS simulation started, e.g. `2026-05-05T12:43:05.123-05:00` |
| `elapsed_ms_since_t0` | Milliseconds elapsed on the SMS machine since the sim started |
| `universe_time_ms` | The simulation's internal universe clock in milliseconds |
| `sms_send_time_ms` | When the dispatcher sent this packet (Unix ms) |
| `gsm_receive_time_ms` | When the GSM received this packet (Unix ms) |
| `sms_to_gsm_trip_ms` | The actual transmission trip time in milliseconds |
| `propagation_delay_ms` | The simulated speed-of-light delay the dispatcher applied |

The trip time calculation is intentionally left to external scripts. This pipeline records the raw timestamps and lets your analysis tools do the math.

---

## How the Dispatcher Decides What to Send

Not every object gets a message every frame. The dispatcher applies three gates in order:

**Line-of-sight check** — if the object is behind Earth with no visible ground station or TDRS relay, it is dropped that frame entirely and no message is sent.

**Throttle** — satellites publish at up to 40 Hz, asteroids at 10 Hz, debris at 5 Hz. Objects that were published too recently are skipped.

**Propagation delay** — the dispatcher computes the speed-of-light travel time from the object to the nearest visible ground station (or via TDRS relay to White Sands NM), then holds the message on a priority queue until that delay has elapsed before publishing.

Ground stations used for geometry: White Sands NM, Guam, Kiruna Sweden, Santiago Chile, Dongara Australia, North Pole Alaska, plus three TDRS geostationary relays at 41W, 171W, and 174E.

---

## Troubleshooting

| Problem | Check |
|---|---|
| Dispatcher shows `connection failed` | Mosquitto is not running on the Dell, or the Windows firewall is blocking port 1883 |
| GSM manager shows `received=0` after 30s | Dispatcher is not running or is pointed at the wrong broker IP |
| No Parquet files after 2 minutes | Processor not running, or GSM manager not writing JSON files — check both terminals |
| Mosquitto shows PUBLISH lines but no Sending lines | GSM manager is not subscribed — check its terminal for the subscribe confirmation |
| `nc -zv IP 1883` fails from either Mac | Re-run the PowerShell firewall command as Administrator on the Dell |
| Specific objects never appear in data | Those objects may be in continuous blackout — normal if they orbit behind Earth relative to all ground stations |
