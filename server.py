import socket
import threading
import time
import os
import traceback
import json
import hashlib
from collections import deque

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
import requests

load_dotenv()

# Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 2447))
HOST_REB = os.getenv("HOST_REB", "45.112.204.242")
HOST_REB_PORT = int(os.getenv("HOST_REB_PORT", 8090))
PUBLISH_TOPIC = os.getenv("PUBLISH_TOPIC", "dev/gps")
EVENT_TOPIC = os.getenv("EVENT_TOPIC", "dev/gps/events")
HEARTBEAT_TOPIC = os.getenv("HEARTBEAT_TOPIC", "sat/gps")
MAX_DISTANCE = float(os.getenv("MAX_DISTANCE", 0.001))  # ~100m
MAX_TIME_BETWEEN_UPDATES_MIN = float(os.getenv("MAX_TIME_BETWEEN_UPDATES_MIN", 60))
# H02 V1 text packets require an ACK or the tracker will replay its blind-spot
# buffer indefinitely. Format confirmed from Traccar H02ProtocolDecoder.java:
#   *HQ,<imei>,V4,V1,<yyyyMMddHHmmss UTC>#
# Set SEND_ACK=false only for debugging (disabling causes retransmission storms).
SEND_ACK = os.getenv("SEND_ACK", "true").lower() == "true"
VM_URL = os.getenv("VM_URL", "http://192.168.4.3:8428")

# Per-IMEI state, keyed by IMEI string
device_states: dict[str, dict] = {}
device_states_lock = threading.Lock()
global_status = "ALIVE"

print("Starting GPS rebouncer server...")

# --- MQTT ---

global_client: mqtt.Client = None


def mqtt_thread_fn():
    global global_client
    broker = os.getenv("BROKER")
    print(f"[MQTT] Connecting to broker: {broker!r}")
    client = mqtt.Client()

    def on_connect(_c, _userdata, _flags, rc):
        if rc == 0:
            print(f"[MQTT] Connected (rc=0)")
        else:
            print(f"[MQTT] Connection failed (rc={rc})")

    def on_disconnect(_c, _userdata, rc):
        print(f"[MQTT] Disconnected (rc={rc})")

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect(broker, 1883, 60)
    global_client = client
    client.loop_forever()


threading.Thread(target=mqtt_thread_fn, daemon=True).start()

while global_client is None:
    time.sleep(0.05)

print("[MQTT] Client object ready")


def heartbeat_loop_fn():
    while True:
        global_client.publish(HEARTBEAT_TOPIC, global_status)
        time.sleep(1)


threading.Thread(target=heartbeat_loop_fn, daemon=True).start()


def restart_timer():
    time.sleep(24 * 60 * 60)
    os._exit(0)


threading.Thread(target=restart_timer, daemon=True).start()


# --- H02 protocol parser ---

def parse_h02(raw: str) -> dict | None:
    """
    Parse a single *HQ,...# H02 packet.

    Format:
      *HQ,<imei>,<type>,<HHMMSS>,<A|V>,<lat>,<N|S>,<lon>,<E|W>,
          <speed>,<course>,<DDMMYY>,<status_hex>,<mcc>,<mnc>,<lac>,<cid>#

    Returns None for any unparseable input (wrong prefix, too few fields,
    bad numbers). GPS invalid (A/V flag = V) is not an error — lat/lon will
    be None, which callers must handle.
    """
    raw = raw.strip().rstrip("#")
    if not raw.startswith("*HQ,"):
        return None
    parts = raw.split(",")
    if len(parts) < 12:
        return None
    try:
        imei = parts[1]
        pkt_type = parts[2]   # V1, V6, etc.
        time_str = parts[3]   # HHMMSS
        gps_valid = parts[4]  # A = valid fix, V = invalid / no fix
        lat_raw = parts[5]    # ddmm.mmmm
        lat_dir = parts[6]    # N or S
        lon_raw = parts[7]    # dddmm.mmmm
        lon_dir = parts[8]    # E or W
        speed = parts[9]
        course = parts[10]
        date_str = parts[11]  # DDMMYY
        status_hex = parts[12] if len(parts) > 12 else None
        mcc = parts[13] if len(parts) > 13 else None
        mnc = parts[14] if len(parts) > 14 else None
        lac = parts[15] if len(parts) > 15 else None
        cid = parts[16] if len(parts) > 16 else None

        lat = lon = None
        if gps_valid == "A":
            # NMEA format: ddmm.mmmm — first 2 digits are degrees, rest are minutes
            lat = round(float(lat_raw[:2]) + float(lat_raw[2:]) / 60, 6)
            if lat_dir == "S":
                lat = -lat
            # Longitude: first 3 digits are degrees
            lon = round(float(lon_raw[:3]) + float(lon_raw[3:]) / 60, 6)
            if lon_dir == "W":
                lon = -lon

        tracker_ts = None
        try:
            tracker_ts = time.mktime(time.strptime(f"{date_str} {time_str}", "%d%m%y %H%M%S"))
        except ValueError:
            # Stale / garbage date seen on boot/reconnect (e.g. "090925"). Not fatal.
            pass

        return {
            "imei": imei,
            "type": pkt_type,
            "gps_valid": gps_valid,
            "lat": lat,
            "lon": lon,
            "speed": speed,
            "course": course,
            "status_hex": status_hex,
            "mcc": mcc,
            "mnc": mnc,
            "lac": lac,
            "cid": cid,
            "time_str": time_str,
            "date_str": date_str,
            "tracker_ts": tracker_ts,
        }
    except Exception:
        return None


def interpret_status(status_hex: str) -> dict:
    """
    Empirical interpretation of the H02 status bitmask.
    Convention is active-low: bit = 0 means the condition is active.
    These bit assignments are derived from observed patterns, NOT official docs.
    """
    try:
        val = int(status_hex, 16)
    except (ValueError, TypeError):
        return {}
    return {
        "moving": not bool(val & 0x04000000),      # bit 26 clear → moving / wakeup
        "low_battery": not bool(val & 0x00000800),  # bit 11 clear → low battery
        "alarm": not bool(val & 0x00000040),         # bit 6 clear  → alarm pending
    }


# --- lat/lon encoding for Victoria Metrics ---
# Encodes lat/lon into a single float64-safe integer (~10m precision).

_ENCODING_FACTOR = 10 ** 4
_LAT_OFFSET = 90
_LON_OFFSET = 180
_MULTIPLIER = 10 ** 4 * 360


def encode_latlon(lat: float, lon: float) -> int:
    lat_enc = int((lat + _LAT_OFFSET) * _ENCODING_FACTOR)
    lon_enc = int((lon + _LON_OFFSET) * _ENCODING_FACTOR)
    return lat_enc * _MULTIPLIER + lon_enc


# Queue for VM writes that failed, retried by a background thread.
_failed_vm_queue: list[dict] = []


def write_location_to_vm(lat: float, lon: float, timestamp_ms: int, imei: str, status_hex: str | None) -> None:
    payload = {
        "metric": {"__name__": "location/latlon", "imei": imei, "status": status_hex or ""},
        "values": [encode_latlon(lat, lon)],
        "timestamps": [timestamp_ms],
    }
    print(f"[VM] POST {VM_URL}/api/v1/import payload={payload}")
    try:
        r = requests.post(f"{VM_URL}/api/v1/import", json=payload, timeout=5)
        r.raise_for_status()
        print(f"[VM] Write OK (status={r.status_code})")
    except Exception as e:
        print(f"[VM] Write failed: {e} — queuing for retry")
        _failed_vm_queue.append(payload)


def _vm_retry_loop() -> None:
    while True:
        time.sleep(10)
        if not _failed_vm_queue:
            continue
        print(f"[VM] Retrying {len(_failed_vm_queue)} failed write(s)...")
        payload = _failed_vm_queue.pop(0)
        try:
            r = requests.post(f"{VM_URL}/api/v1/import", json=payload, timeout=5)
            r.raise_for_status()
            print(f"[VM] Retry OK")
        except Exception as e:
            print(f"[VM] Retry failed: {e} — re-queuing")
            _failed_vm_queue.append(payload)


# Known empirical state transitions (from → to) → human label.
# Used to enrich event payloads; does not gate behaviour.
_TRANSITIONS: dict[tuple[str | None, str], str] = {
    ("FFFFFBFF", "FBFFFBFF"): "movement_wakeup",
    ("FBFFFBFF", "FFFFFBFF"): "stopped",
    ("FFFFFBFF", "FBF7FBBF"): "low_battery_alarm",
    ("FBFFFBFF", "FBF7FBBF"): "low_battery_alarm",
    ("FBF7FBBF", "FFFFFBFF"): "normal_restored",
    ("FBF7FBFF", "FFFFFBFF"): "normal_restored",
    (None, "FFFFFBFF"):       "boot_normal",
    (None, "FBFFFBFF"):       "boot_moving",
}


def send_v1_ack(conn: socket.socket, imei: str) -> None:
    """
    ACK for H02 V1 text packet. Without this the tracker never drains its
    blind-spot replay buffer and retransmits the same packets indefinitely.
    Format from Traccar H02ProtocolDecoder.java: *HQ,<imei>,V4,V1,<UTC>#
    """
    now_utc = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    ack = f"*HQ,{imei},V4,V1,{now_utc}#"
    try:
        conn.sendall(ack.encode())
        print(f"[{imei}] ACK sent: {ack}")
    except Exception as e:
        print(f"[{imei}] ACK send failed: {e}")


def get_device_state(imei: str) -> dict:
    with device_states_lock:
        if imei not in device_states:
            device_states[imei] = {
                "last_status_hex": None,
                "last_location": None,       # {"lat": float, "lon": float}
                "last_tracker_ts": None,     # tracker's own timestamp (unix float)
                "last_update": time.time() - MAX_TIME_BETWEEN_UPDATES_MIN * 60,
                # Rolling window of raw-packet MD5 hashes for replay detection.
                # maxlen caps memory; oldest entries age out automatically.
                "seen_hashes": deque(maxlen=500),
            }
        return device_states[imei]


# --- connection handler ---

def handle_client_connection(conn: socket.socket):
    global global_status

    try:
        try:
            data = conn.recv(1024)
            if not data:
                return
            decoded = data.decode("utf-8")
            print(f"Received: {decoded!r}")
        except Exception as e:
            print(f"Error receiving data: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            global_status = "ERROR"
            return

        # Send ACK immediately after receiving, before full processing.
        # The tracker will not drain its blind-spot replay buffer until it gets
        # *HQ,<imei>,V4,V1,<utc># back. Quick-extract IMEI for this purpose.
        if SEND_ACK:
            first_raw = next((p.strip() for p in decoded.split("#") if p.strip().startswith("*HQ,")), None)
            if first_raw:
                quick_parts = first_raw.split(",")
                if len(quick_parts) >= 2:
                    send_v1_ack(conn, quick_parts[1])

        # Forward raw bytes to Sinotrack upstream unchanged
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as out:
                out.settimeout(5)
                out.connect((HOST_REB, HOST_REB_PORT))
                out.sendall(data)
        except Exception as e:
            print(f"Error forwarding to Sinotrack: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)

        for raw_pkt in decoded.split("#"):
            raw_pkt = raw_pkt.strip()
            if not raw_pkt:
                continue

            pkt = parse_h02(raw_pkt)
            if pkt is None:
                print(f"Unparseable packet: {raw_pkt!r}")
                continue

            imei = pkt["imei"]
            state = get_device_state(imei)

            # Replay detection: same raw packet hash seen before → tracker retry queue.
            # We still forwarded to Sinotrack above, but we don't re-emit MQTT events.
            pkt_hash = hashlib.md5(raw_pkt.encode()).hexdigest()
            if pkt_hash in state["seen_hashes"]:
                print(f"[{imei}] Replay detected, skipping MQTT")
                continue
            state["seen_hashes"].append(pkt_hash)

            status_hex = pkt["status_hex"]
            prev_status = state["last_status_hex"]

            # Publish status/alarm events regardless of GPS position change.
            # Same coordinates + different status = important event (movement, battery, etc.)
            if status_hex and status_hex != prev_status:
                transition = _TRANSITIONS.get((prev_status, status_hex))
                interpreted = interpret_status(status_hex)
                if transition is None:
                    print(
                        f"[{imei}] UNKNOWN STATUS TRANSITION: {prev_status} → {status_hex}"
                        f" | interpreted={interpreted}"
                        f" | raw_packet={raw_pkt!r}"
                    )
                else:
                    print(f"[{imei}] Status: {prev_status} → {status_hex} ({transition}) {interpreted}")
                event_payload = {
                    "imei": imei,
                    "type": pkt["type"],
                    "from_status": prev_status,
                    "to_status": status_hex,
                    "transition": transition,
                    "gps_valid": pkt["gps_valid"],
                    "lat": pkt["lat"],
                    "lon": pkt["lon"],
                    "tracker_ts": pkt["tracker_ts"],
                    "interpreted": interpreted,
                }
                global_client.publish(EVENT_TOPIC, json.dumps(event_payload))
                state["last_status_hex"] = status_hex

            # V6 type or A/V flag = V means no GPS fix (boot, LBS-only, reconnect).
            # Log it but do not publish a location — coordinates are unreliable.
            if pkt["gps_valid"] != "A" or pkt["lat"] is None:
                print(f"[{imei}] No GPS fix (type={pkt['type']} valid={pkt['gps_valid']}), skipping location update")
                continue

            lat, lon = pkt["lat"], pkt["lon"]
            tracker_ts = pkt["tracker_ts"]

            print(f"[{imei}] GPS fix: lat={lat} lon={lon} ts={pkt['date_str']} {pkt['time_str']} tracker_ts={tracker_ts}")

            # Discard out-of-order timestamps (stale packet from before last known fix)
            if tracker_ts and state["last_tracker_ts"] and tracker_ts < state["last_tracker_ts"]:
                print(f"[{imei}] Stale timestamp: got {tracker_ts} but last was {state['last_tracker_ts']}, skipping")
                continue

            last_loc = state["last_location"]
            if last_loc is not None:
                dlat = abs(lat - last_loc["lat"])
                dlon = abs(lon - last_loc["lon"])
                is_near = dlat < MAX_DISTANCE and dlon < MAX_DISTANCE
                print(f"[{imei}] Distance check: dlat={dlat:.6f} dlon={dlon:.6f} threshold={MAX_DISTANCE} is_near={is_near}")
            else:
                is_near = False
                print(f"[{imei}] No previous location, will publish")

            elapsed = time.time() - state["last_update"]
            time_exceeded = elapsed > MAX_TIME_BETWEEN_UPDATES_MIN * 60
            print(f"[{imei}] Time since last update: {elapsed:.0f}s (limit={MAX_TIME_BETWEEN_UPDATES_MIN * 60:.0f}s) time_exceeded={time_exceeded}")

            if is_near and not time_exceeded:
                print(f"[{imei}] Location unchanged and time not exceeded, skipping update")
                continue

            timestamp_ms = int(tracker_ts * 1000) if tracker_ts else int(time.time() * 1000)
            write_location_to_vm(lat, lon, timestamp_ms, imei, status_hex)
            state["last_location"] = {"lat": lat, "lon": lon}
            state["last_update"] = time.time()
            state["last_tracker_ts"] = tracker_ts
            global_status = "ALIVE"
            print(f"[{imei}] Location written: {lat},{lon} status={status_hex} ts={timestamp_ms}")

    finally:
        time.sleep(1)  # keep connection alive briefly for ACK delivery
        conn.close()


# --- main ---

def start_server():
    while True:
        try:
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((HOST, PORT))
            server_socket.listen(5)
            print(f"Listening on {HOST}:{PORT}...")

            while True:
                conn, _ = server_socket.accept()
                threading.Thread(target=handle_client_connection, args=(conn,), daemon=True).start()

        except Exception as e:
            print(f"Server error: {e}")
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=_vm_retry_loop, daemon=True).start()
    start_server()
