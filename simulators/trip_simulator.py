import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from pydantic_settings import BaseSettings

class SimulatorSettings(BaseSettings):
    MQTT_BROKER_HOST: str = "localhost"
    MQTT_BROKER_PORT: int = 1883
    MQTT_USER: str
    MQTT_PASSWORD: str

    class Config:
        env_file_encoding = 'utf-8'
        extra = 'ignore'
        _env_file = "karto.env"
        if not Path(_env_file).exists():
            if Path(".env").exists():
                _env_file = ".env"
            else:
                print("ERROR: Could not find 'karto.env' or '.env' file.", file=sys.stderr)
                sys.exit(1)
        env_file = _env_file

TRIP_PATH = [
    (37.8024, -122.4058), (37.8045, -122.4077), (37.8066, -122.4093),
    (37.8085, -122.4100), (37.8090, -122.4105)
]

def parse_args():
    parser = argparse.ArgumentParser(description="Karto Trip Simulator")
    parser.add_argument("--vehicle-id", default="SIM-VEHICLE-1", help="The vehicle ID to simulate")
    parser.add_argument("--user-id", default="trip_simulator", help="The user ID for the MQTT topic")
    return parser.parse_args()

def publish_metric(client: mqtt.Client, user_id: str, vehicle_id: str, metric: str, payload: str):
    topic = f"ovms/{user_id}/{vehicle_id}/metric/{metric.replace('.', '/')}"
    client.publish(topic, payload, qos=1)
    print(f"  -> Published Topic: {topic}, Payload: {payload}")
    time.sleep(0.05)

def on_connect(client, userdata, flags, rc, properties=None):
    """Callback for when the client connects to the broker."""
    if rc == 0:
        print("Successfully connected to MQTT broker.")
    else:
        print(f"Failed to connect, return code {rc}\n", file=sys.stderr)
        if rc in [1, 2, 3, 4, 5]:
             print("This is a critical error, please check broker settings and credentials. Exiting.", file=sys.stderr)
             sys.exit(1)

def simulate_trip(client: mqtt.Client, user_id: str, vehicle_id: str, path: list):
    print(f"\n--- Starting Trip Simulation for Vehicle: {vehicle_id.upper()} ---")
    try:
        print("\n Sending 'Engine On' signal...")
        publish_metric(client, user_id, vehicle_id, "v.b.soc", str(95))
        publish_metric(client, user_id, vehicle_id, "v.e.on", "1")
        print("    Waiting 3 seconds for trip to initialize...")
        time.sleep(3)

        print("\n Driving along the path...")
        for i, (lat, lon) in enumerate(path):
            print(f"\n    Publishing GPS point {i+1}/{len(path)} at ({lat}, {lon})...")
            
            speed = 25.0 + (i * 2)
            now_dt = datetime.now(timezone.utc)
            utc_time_str = now_dt.strftime('%Y-%m-%d %H:%M:%S') + " UTC"
            soc = 90.5 - (i * 1.5)
            altitude = 105.7 + (i * 5.5)
            
            publish_metric(client, user_id, vehicle_id, "v.p.latitude", str(lat))
            publish_metric(client, user_id, vehicle_id, "v.p.longitude", str(lon))
            publish_metric(client, user_id, vehicle_id, "v.p.speed", str(speed))
            publish_metric(client, user_id, vehicle_id, "v.p.altitude", f"{altitude:.1f}")
            publish_metric(client, user_id, vehicle_id, "m.time.utc", utc_time_str)
            publish_metric(client, user_id, vehicle_id, "v.b.soc", str(soc))
            
            if i < len(path) - 1:
                print("    Waiting 5 seconds before next point...")
                time.sleep(5)

        print("\n Sending 'Engine Off' signal...")
        publish_metric(client, user_id, vehicle_id, "v.e.on", "0")
        print("    Trip has ended. Waiting for grace period processing...")

    except KeyboardInterrupt:
        print("\nSimulation interrupted by user.")
    finally:
        print("\n Sending final 'Engine Off' signal to be safe...")
        publish_metric(client, user_id, vehicle_id, "v.e.on", "0")
        print("    Waiting 1 second for final message to be sent...")
        time.sleep(1)
        print("\n--- Simulation Complete ---")

if __name__ == "__main__":
    args = parse_args()
    try:
        settings = SimulatorSettings()
    except Exception as e:
        print(f"ERROR: Could not load settings. {e}", file=sys.stderr)
        sys.exit(1)

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"karto_simulator_{args.vehicle_id}")
    
    client.username_pw_set(settings.MQTT_USER, settings.MQTT_PASSWORD)
    client.on_connect = on_connect

    try:
        print(f"Connecting to MQTT broker at {settings.MQTT_BROKER_HOST}:{settings.MQTT_BROKER_PORT}...")
        client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, 60)
        client.loop_start()

        time.sleep(2)

        simulate_trip(client, args.user_id, args.vehicle_id, TRIP_PATH)
    except Exception as e:
        print(f"An error occurred during simulation: {e}", file=sys.stderr)
    finally:
        print("Disconnecting MQTT client.")
        client.loop_stop()
        client.disconnect()