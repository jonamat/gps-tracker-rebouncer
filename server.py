import socket
import threading
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
import time
import os
import json

load_dotenv()

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 2447))
HOST_REB = os.getenv("HOST_REB", "45.112.204.242")
HOST_REB_PORT = int(os.getenv("HOST_REB_PORT", 8090))
PUBLISH_TOPIC = os.getenv("PUBLISH_TOPIC", "dev/gps")
HEARTBEAT_TOPIC = os.getenv("HEARTBEAT_TOPIC", "sat/gps")
MAX_DISTANCE = float(os.getenv("MAX_DISTANCE", 0.001)) # ~ 100m
MAX_TIME_BETWEEN_UPDATES_MIN = float(os.getenv("MAX_TIME_BETWEEN_UPDATES_MIN", 60))

input_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
input_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

global_client: mqtt.Client = None
last_status = "ALIVE"
last_location = None
last_timestamp = 0
last_update = time.time() - MAX_TIME_BETWEEN_UPDATES_MIN * 60

print("Starting GPS rebouncer server...")


def mqtt_thread_fn():
    global global_client
    client = mqtt.Client()
    client.connect(os.getenv("BROKER"), 1883, 60)
    global_client = client
    client.loop_forever()


mqtt_thread = threading.Thread(target=mqtt_thread_fn)
mqtt_thread.start()

while global_client is None:
    pass

print("MQTT client connected")


def heartbeat_loop_fn():
    while True:
        global_client.publish(HEARTBEAT_TOPIC, last_status)
        time.sleep(1)


heartbeat_thread = threading.Thread(target=heartbeat_loop_fn)
heartbeat_thread.start()


def handle_client_connection(conn):
    global last_status
    global last_location
    global last_update
    global last_timestamp
    
    # receive data from the GPS tracker
    try:
        data = conn.recv(1024)
        if not data:
            return
        decoded = data.decode("utf-8")
        print(f"Received: {decoded}")
        
        # *HQ,xxxxxxxxxx,V1,HHMMSS,A,4220.8148,N,01409.2804,E,000.00,xxx,DDMMYY,xxxxxxxx,xxx,xx,xxxxx,xxxxxxxx#
        # *HQ,xxxxxxxxxx,V1,221813,A,4220.8148,N,01600.8237,E,000.00,010,140224,FBFFFBFF,222,10,42092,19981601#
        locations: list[str] = decoded.split("#")

        for location in locations:
            if len(location) == 0:
                continue

            locationRaw: list[str] = location.split(",")[5:8]
            # to 60 degree and float 5 digits max
            lat = round(float(locationRaw[0][:2]) + float(locationRaw[0][2:]) / 60, 5)
            lon = round(float(locationRaw[2][:3]) + float(locationRaw[2][3:]) / 60, 5)
            location = json.dumps({
                "lat": lat,
                "lon": lon
            })
            hhmmss = location.split(",")[3]
            ddmmyy = location.split(",")[11]
            timestamp = time.mktime(time.strptime(f"{ddmmyy} {hhmmss}", "%d%m%y %H%M%S"))
            
    except Exception as e:
        print(f"Error during data receiving: {e}")
        last_status = "ERROR"
        return

    err = False
    # send data to sinotrack
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as output_socket:
            output_socket.settimeout(5)
            output_socket.connect((HOST_REB, HOST_REB_PORT))
            output_socket.sendall(data)
    except Exception as e:
        print(f"Error during data sending: {e}")
        err = True

    is_close_to_last = last_location is not None and abs(lat - last_location["lat"]) < MAX_DISTANCE and abs(lon - last_location["lon"]) < MAX_DISTANCE
    time_exceeded = time.time() - last_update > MAX_TIME_BETWEEN_UPDATES_MIN * 60 
    has_old_timestamp = timestamp < last_timestamp

    if is_close_to_last and not time_exceeded:
        print(f"Location is close to the last one, skip update")
        return

    if has_old_timestamp:
        print(f"Timestamp is older than the last one, skip update")
        return
        
    if is_close_to_last and time_exceeded:
        print(f"Location is close to the last one, but time exceeded, updating")

    if not is_close_to_last:
        print(f"Location is changed, updating")
    
    pub = global_client.publish(PUBLISH_TOPIC, location)
    if pub.is_published():
        last_location = json.loads(location)
        last_update = time.time()
        last_timestamp = timestamp
    else:
        print(f"Error during publishing to MQTT: {pub.rc}")
        err = True
        return

    if err:
        last_status = "ERROR"
    else:
        last_status = "ALIVE"


while True:
    try:
        input_socket.bind((HOST, PORT))
        input_socket.listen(1)
        print(f"Listening on {HOST}:{PORT}...")

        while True:
            conn, addr = input_socket.accept()
            try:
                handle_client_connection(conn)
            except Exception as e:
                print(f"Error during handle_client_connection: {e}")
                last_status = "ERROR"
            time.sleep(1)
            conn.close()

    except Exception as e:
        print(f"Error during input socket binding: {e}")
        last_status = "ERROR"
        
    finally:
        conn.close()
        input_socket.close()
