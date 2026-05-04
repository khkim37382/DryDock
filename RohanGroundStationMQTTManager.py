# ground station manager (GSM)
import paho.mqtt.client as mqtt
import os
import json
import time
import threading
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

## Wipe before hand so that no manual cleaning
def clear_directories():
    for directory in [SAVE_DIR, OUTPUT_DIR]:  # GSM uses these two
        for filename in os.listdir(directory):
            if filename.endswith(".json"):
                filepath = os.path.join(directory, filename)
                os.remove(filepath)
                print(f"[startup] cleared: {filepath}")
    print("[startup] all directories cleared, starting fresh")

BROKER_IP = ""  # fill in
TOPIC_TELEMETRY = "satellite/telemetry"
TOPIC_THRUSTER = "satellite/thruster"

SAVE_DIR = "received_transmissions"
OUTPUT_DIR = "ready_to_send_transmissions"
MAX_AGE_SECONDS = 180

os.makedirs(SAVE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

clear_directories()

if not BROKER_IP:
    raise ValueError("Set BROKER_IP before running")


# ─── MQTT CALLBACKS ───────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("Connected to broker")
        client.subscribe(TOPIC_TELEMETRY)
    else:
        print(f"Connection failed: {rc}")


def on_message(client, userdata, msg):
    gsm_receive_time_ns = time.time_ns()

    try:
        payload = json.loads(msg.payload.decode("utf-8"))

        if msg.topic == TOPIC_TELEMETRY:

            # calculate SMS→GSM trip time
            sms_send_time_ns = payload.get("sms_send_time_ns")
            if sms_send_time_ns:
                sms_to_gsm_trip_ns = gsm_receive_time_ns - sms_send_time_ns
            else:
                sms_to_gsm_trip_ns = None
                print("Warning: no sms_send_time_ns in payload")

            # append GSM timing fields only, no roundtrip calc here
            payload["gsm_receive_time_ns"] = gsm_receive_time_ns
            payload["sms_to_gsm_trip_ns"] = sms_to_gsm_trip_ns

            # save for other GSM script to parse
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{SAVE_DIR}/telemetry_{timestamp}.json"
            with open(filename, "w") as f:
                json.dump(payload, f)
            print(f"[{timestamp}] telemetry saved | sms→gsm: {sms_to_gsm_trip_ns}ns")

    except json.JSONDecodeError:
        print(f"Invalid JSON received: {msg.payload}")
    except Exception as e:
        print(f"Error in on_message: {e}")


# ─── CLEANUP THREAD ───────────────────────────────────────────────────────────

def cleanup_old_files():
    while True:
        cutoff = time.time() - MAX_AGE_SECONDS
        for filename in os.listdir(SAVE_DIR):
            filepath = os.path.join(SAVE_DIR, filename)
            try:
                if os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
                    print(f"[cleanup] deleted: {filename}")
            except Exception as e:
                print(f"[cleanup] error on {filename}: {e}")
        time.sleep(30)


# ─── WATCHDOG ─────────────────────────────────────────────────────────────────

class OutputDirHandler(FileSystemEventHandler):
    def __init__(self, mqtt_client):
        self.client = mqtt_client
        self.processing = False

    def on_modified(self, event):
        if event.is_directory:
            return
        if event.src_path.endswith(".json") and not self.processing:
            self.processing = True
            self.send_all_pending()
            self.processing = False

    def send_all_pending(self):
        files = sorted(os.listdir(OUTPUT_DIR))
        for filename in files:
            if not filename.endswith(".json"):
                continue
            filepath = os.path.join(OUTPUT_DIR, filename)
            try:
                time.sleep(0.1)
                with open(filepath, "r") as f:
                    payload = json.load(f)

                # only append GSM send time, SMS handles the rest
                payload["gsm_send_time_ns"] = time.time_ns()

                self.client.publish(TOPIC_THRUSTER, json.dumps(payload))
                timestamp = datetime.now().strftime("%H%M%S_%f")
                print(f"[{timestamp}] thrust vector sent: {filename}")

                os.remove(filepath)
                print(f"[{timestamp}] deleted after send: {filename}")

            except json.JSONDecodeError:
                print(f"Invalid JSON, skipping and deleting: {filename}")
                os.remove(filepath)
            except Exception as e:
                print(f"Error on {filename}: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message
client.connect(BROKER_IP)
client.loop_start()

observer = Observer()
observer.schedule(OutputDirHandler(client), OUTPUT_DIR, recursive=False)
observer.start()
print(f"Watching {OUTPUT_DIR} for thrust vectors to send...")

cleanup_thread = threading.Thread(target=cleanup_old_files, daemon=True)
cleanup_thread.start()
print(f"Cleanup thread started, telemetry deleted after {MAX_AGE_SECONDS}s")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    observer.stop()
    client.loop_stop()
    client.disconnect()
    print("Disconnected cleanly")

observer.join()