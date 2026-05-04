# ============================================================
# satellite_manager.py  —  v2 (reference, not in active pipeline)
# ============================================================
#
# *** CURRENT STATUS: NOT IN THE ACTIVE PIPELINE ***
#
# We agreed to focus on one-way GSM ingest only for now.
# The active pipeline is:
#   satellite_orbit.py → ready_to_send_telemetry/ → dispatcher.cpp
#     → MQTT (per-object topics) → ground_station_manager.py
#     → received_transmissions/ → ground_station_processor.cpp
#     → training_data/ (Parquet)
#
# This file is preserved for FUTURE bidirectional work, when you
# want the GSM to send thrust commands BACK to the satellite side.
# When that happens, this script runs on the satellite side to:
#   - Subscribe to satellite/thruster
#   - Calculate gsm_to_sms_trip_ns from gsm_send_time_ns
#   - Cache last_gsm_to_sms_trip_ns for next outbound telemetry
#   - Save received commands to received_commands/ for a burn
#     execution script to pick up
#   - Watch ready_to_send_telemetry_sms/ for telemetry produced
#     by another satellite-side script and publish it
#
# To activate this file later:
#   1. Set BROKER_IP
#   2. Run python satellite_manager.py
#   3. Make sure ground_station_manager.py's outbound watchdog
#      is producing thrust-vector JSONs in ready_to_send_transmissions/
#
# ROUNDTRIP / TIMING DESIGN
# ─────────────────────────
# - Reads gsm_send_time_ns from incoming thrust commands
# - Computes gsm_to_sms_trip_ns = sms_receive - gsm_send
# - Caches as last_gsm_to_sms_trip_ns (a single float)
# - Appends both last_gsm_to_sms_trip_ns AND new sms_send_time_ns
#   to every outbound telemetry packet
# ============================================================

import paho.mqtt.client as mqtt
import os
import json
import time
import threading
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

BROKER_IP        = ""  # fill in
TOPIC_TELEMETRY  = "satellite/telemetry"
TOPIC_THRUSTER   = "satellite/thruster"

COMMANDS_DIR     = "received_commands"
OUTPUT_DIR       = "ready_to_send_telemetry_sms"
MAX_AGE_SECONDS  = 180   # 3 min retention on commands

os.makedirs(COMMANDS_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR,   exist_ok=True)

if not BROKER_IP:
    raise ValueError("Set BROKER_IP before running")

# Shared cache: last observed GSM→SMS trip time
last_gsm_to_sms_trip_ns = None


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to broker")
        client.subscribe(TOPIC_THRUSTER)
    else:
        print(f"Connection failed: {rc}")


def on_message(client, userdata, msg):
    global last_gsm_to_sms_trip_ns
    sms_receive_time_ns = time.time_ns()

    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        print(f"Invalid JSON: {msg.payload}")
        return

    if msg.topic == TOPIC_THRUSTER:
        gsm_send_time_ns = payload.get("gsm_send_time_ns")
        if gsm_send_time_ns:
            gsm_to_sms_trip_ns = sms_receive_time_ns - gsm_send_time_ns
            last_gsm_to_sms_trip_ns = gsm_to_sms_trip_ns
            payload["sms_receive_time_ns"] = sms_receive_time_ns
            payload["gsm_to_sms_trip_ns"]  = gsm_to_sms_trip_ns
            payload["last_gsm_to_sms_observed_at_ns"] = sms_receive_time_ns
            print(f"command received | gsm→sms: {gsm_to_sms_trip_ns}ns")

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"{COMMANDS_DIR}/command_{timestamp}.json"
        with open(filename, "w") as f:
            json.dump(payload, f)


def cleanup_old_files():
    while True:
        cutoff = time.time() - MAX_AGE_SECONDS
        for filename in os.listdir(COMMANDS_DIR):
            filepath = os.path.join(COMMANDS_DIR, filename)
            try:
                if os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
            except FileNotFoundError:
                pass
        time.sleep(30)


class OutputDirHandler(FileSystemEventHandler):
    def __init__(self, mqtt_client):
        self.client = mqtt_client
        self.processing = False

    def on_modified(self, event):
        if event.is_directory: return
        if event.src_path.endswith(".json") and not self.processing:
            self.processing = True
            self.send_all_pending()
            self.processing = False

    def send_all_pending(self):
        for filename in sorted(os.listdir(OUTPUT_DIR)):
            if not filename.endswith(".json"): continue
            filepath = os.path.join(OUTPUT_DIR, filename)
            try:
                time.sleep(0.1)
                with open(filepath, "r") as f:
                    payload = json.load(f)

                payload["sms_send_time_ns"]         = time.time_ns()
                payload["last_gsm_to_sms_trip_ns"]  = last_gsm_to_sms_trip_ns

                self.client.publish(TOPIC_TELEMETRY, json.dumps(payload))
                os.remove(filepath)
            except json.JSONDecodeError:
                os.remove(filepath)
            except Exception as e:
                print(f"Error on {filename}: {e}")


def main():
    client = mqtt.Client()
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER_IP)
    client.loop_start()

    observer = Observer()
    observer.schedule(OutputDirHandler(client), OUTPUT_DIR, recursive=False)
    observer.start()

    threading.Thread(target=cleanup_old_files, daemon=True).start()

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        client.loop_stop()
        client.disconnect()
        observer.join()


if __name__ == "__main__":
    main()
