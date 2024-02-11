# GPS Rebouncer

The server listens for GPS data from a Sinotrak GPS tracker device, mine is a ST-901L 4G Mini GPS tracker, rebroadcasts it to a Sinotrack servers, and publishes the location data to an MQTT broker

## Usage

1. Clone the repository:

   ```bash
   git clone https://github.com/yourusername/gps-rebouncer.git
   ```

2. Navigate to the project directory:

   ```bash
   cd gps-rebouncer
   ```

3. Install the required packages:

   ```bash
   pip install -r requirements.txt
   ```

4. Set up your environment variables by creating a `.env` file in the project directory and adding the following variables (these are the default values)

   ```
   HOST=0.0.0.0
   PORT=2447
   HOST_REB=45.112.204.242
   HOST_REB_PORT=8089
   PUBLISH_TOPIC=dev/gps
   HEARTBEAT_TOPIC=sat/gps
   MAX_DISTANCE=0.001 # lat/long max distance, approximately 100 meters
   MAX_TIME_BETWEEN_UPDATES_MIN=60 # the max time between consecutive GPS updates for publishing if the distance threshold is not met
   BROKER=your_mqtt_broker_address
   ```

5. Run the script:

   ```bash
   python server.py
   ```

## Notes

- Before using the device, configure the server IP by sending an SMS to the device with the command `8040000 <my_server_ip> <port>`.
- The default Sinotrack server IP address is `45.112.204.242` and the default port is `8089`.
- The script has been tested only with the ST-901L 4G Mini GPS tracker.

## Configuration

- `HOST`: The host IP address for the server to listen on. Defaults to `0.0.0.0`.
- `PORT`: The port number for the server to listen on. Defaults to `2447`.
- `HOST_REB`: The IP address of the destination server to rebroadcast GPS data to. Defaults to `45.112.204.242`.
- `HOST_REB_PORT`: The port number of the destination server to rebroadcast GPS data to. Defaults to `8089`.
- `PUBLISH_TOPIC`: The MQTT topic to publish GPS location data to. Defaults to `dev/gps`.
- `HEARTBEAT_TOPIC`: The MQTT topic to publish heartbeat status messages to. Defaults to `sat/gps`.
- `MAX_DISTANCE`: The maximum distance threshold between consecutive GPS updates for publishing. Defaults to `0.001` (approximately 100 meters).
- `MAX_TIME_BETWEEN_UPDATES_MIN`: The maximum time threshold between consecutive GPS updates for publishing, in minutes. Defaults to `60` minutes.
- `BROKER`: The address of the MQTT broker to connect to.

