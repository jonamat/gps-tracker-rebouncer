import socket
import time
import os
import traceback
import requests
import json
import threading
from dotenv import load_dotenv
from retrying import retry

load_dotenv()

# Configuration
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 2447))
HOST_REB = os.getenv("HOST_REB", "45.112.204.242")
HOST_REB_PORT = int(os.getenv("HOST_REB_PORT", 8090))
MAX_DISTANCE = float(os.getenv("MAX_DISTANCE", 0.001))  # ~100m
MAX_TIME_BETWEEN_UPDATES_MIN = float(os.getenv("MAX_TIME_BETWEEN_UPDATES_MIN", 120))
VM_URL = os.getenv("VM_URL", "http://192.168.4.3:8428")

last_location = None
last_update = time.time() - MAX_TIME_BETWEEN_UPDATES_MIN * 60

# Constants for encoding/decoding lat/lon
ENCODING_FACTOR = 10**6  # Reduce to 6 decimal places
LAT_OFFSET = 90
LON_OFFSET = 180
MAX_LAT = 180  # Latitude range (-90 to 90)
MAX_LON = 360  # Longitude range (-180 to 180)


def encode_latlon_math(lat, lon):
    """Encodes latitude and longitude into a single float64-safe number."""
    lat_enc = int((lat + LAT_OFFSET) * ENCODING_FACTOR)  # Scale and shift to positive
    lon_enc = int((lon + LON_OFFSET) * ENCODING_FACTOR)  # Scale and shift to positive
    return lat_enc * MAX_LON + lon_enc  # Keep within float64 safe range

def decode_latlon_math(encoded):
    """Decodes a float64-safe number back into latitude and longitude."""
    lat_enc = encoded // MAX_LON
    lon_enc = encoded % MAX_LON
    lat = (lat_enc / ENCODING_FACTOR) - LAT_OFFSET
    lon = (lon_enc / ENCODING_FACTOR) - LON_OFFSET
    return lat, lon


def start_server():
    """Start the GPS rebouncer server."""
    while True:
        try:
            server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((HOST, PORT))
            server_socket.listen(5)
            print(f"Listening on {HOST}:{PORT}...")

            while True:
                conn, addr = server_socket.accept()
                threading.Thread(target=handle_client_connection, args=(conn,)).start()
        
        except Exception as e:
            print(f"Server error: {e}")
            traceback.print_exc()
            time.sleep(5)



def handle_client_connection(conn):
    """Handle an incoming client connection."""
    try:
        data = conn.recv(1024)
        if not data:
            return
        decoded = data.decode("utf-8")
        print(f"Received: {decoded}")
        
        locations = parse_gps_data(decoded)
        forward_to_sinotrack(data)
        update_victoria_metrics(locations)
    
    except Exception as e:
        print(f"Error handling client connection: {e}")
        traceback.print_exc()
    
    finally:
        conn.close()



def parse_gps_data(decoded):
    """Parse incoming GPS data into structured format."""
    # *HQ,xxxxxxxxxx,V1,HHMMSS,A,4220.8148,N,01409.2804,E,000.00,xxx,DDMMYY,xxxxxxxx,xxx,xx,xxxxx,xxxxxxxx#
    # *HQ,xxxxxxxxxx,V1,221813,A,4220.8148,N,01600.8237,E,000.00,010,140224,FBFFFBFF,222,10,42092,19981601#

    records = decoded.split("#")
    locations = []

    for record in records:
        if not record:
            continue

        try:
            fields = record.split(",")
            lat_raw, lon_raw = fields[5], fields[7]
            hhmmss, ddmmyy = fields[3], fields[11]
            timestamp_seconds = time.mktime(time.strptime(f"{ddmmyy} {hhmmss}", "%d%m%y %H%M%S"))
            timestamp_seconds = int(timestamp_seconds)
            lat = round(float(lat_raw[:2]) + float(lat_raw[2:]) / 60, 5)
            lon = round(float(lon_raw[:3]) + float(lon_raw[3:]) / 60, 5)

            locations.append({"lat": lat, "lon": lon, "timestamp": timestamp_seconds})
        except Exception as e:
            print(f"Error parsing GPS data: {e}")
            traceback.print_exc()

    return locations



@retry(wait_fixed=5000, stop_max_attempt_number=3)
def forward_to_sinotrack(data):
    """Forward raw data to the Sinotrack service."""
    try:
        with socket.create_connection((HOST_REB, HOST_REB_PORT), timeout=5) as s:
            s.sendall(data)
    except Exception as e:
        print(f"Error forwarding to Sinotrack: {e}")
        traceback.print_exc()
        raise



def update_victoria_metrics(locations):
    """Send location updates to Victoria Metrics if needed."""
    global last_location, last_update

    for record in locations:
        lat, lon, timestamp_sec = record["lat"], record["lon"], record["timestamp"]
        timestamp_ms = timestamp_sec * 1000
        
        if last_location and abs(lat - last_location["lat"]) < MAX_DISTANCE and abs(lon - last_location["lon"]) < MAX_DISTANCE:
            if time.time() - last_update < MAX_TIME_BETWEEN_UPDATES_MIN * 60:
                print("Location unchanged, skipping update")
                continue
            else:
                print("Time exceeded, updating location")
        else:
            print("Location changed, updating")

        try:
            encoded_latlon = encode_latlon(lat, lon)

            payload = {
                "metric": {"__name__": "location/latlon"},
                "values": [encoded_latlon],
                "timestamps": [timestamp_ms]
            }
            
            # todo remove
            print(f"[DEBUG] Sending to Victoria Metrics: {json.dumps(payload)}")

            response = requests.post(f"{VM_URL}/api/v1/import", json=payload)
            response.raise_for_status()
            last_location = record
            last_update = time.time()
        except Exception as e:
            print(f"Error updating Victoria Metrics: {e}")
            traceback.print_exc()



if __name__ == "__main__":
    print("Starting GPS rebouncer server...")
    start_server()
