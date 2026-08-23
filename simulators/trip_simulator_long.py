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

# A longer trip from Berlin to Hamburg, Germany
TRIP_PATH = [
    (52.5163, 13.3777), # Berlin (Brandenburg Gate)
    (52.5358, 13.2006), # Spandau
    (52.6075, 12.8951), # Nauen
    (52.7384, 12.5806), # Friesack
    (52.9452, 12.4975), # Kyritz
    (53.0760, 11.8604), # Perleberg
    (53.3243, 11.4913), # Ludwigslust
    (53.4358, 11.1852), # Hagenow
    (53.3756, 10.7249), # Boizenburg/Elbe
    (53.3725, 10.5589), # Lauenburg/Elbe
    (53.4379, 10.3708), # Geesthacht
    (53.4877, 10.2118), # Bergedorf
    (53.5511, 9.9937)  # Hamburg (City Hall)
]

def parse_args():
    parser = argparse.ArgumentParser(description="Karto Trip Simulator")
    parser.add_argument("--vehicle-id", default="SIM-DE-1", help="The vehicle ID to simulate")
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
        publish_metric(client, user_id, vehicle_id, "v.b.soc", str(98))
        publish_metric(client, user_id, vehicle_id, "v.e.on", "1")
        print("    Waiting 3 seconds for trip to initialize...")
        time.sleep(3)

        print("\n Driving along the path...")
        for i, (lat, lon) in enumerate(path):
            print(f"\n    Publishing GPS point {i+1}/{len(path)} at ({lat}, {lon})...")
            
            speed = 50.0 + (i * 5) if i < (len(path) - 2) else 40.0
            if speed > 130.0:
                speed = 130.0

            now_dt = datetime.now(timezone.utc)
            utc_time_str = now_dt.strftime('%Y-%m-%d %H:%M:%S') + " UTC"
            soc = 98.0 - (i * 2.5)
            altitude = 34.0 + (i * 1.2)
            
            publish_metric(client, user_id, vehicle_id, "v.p.latitude", str(lat))
            publish_metric(client, user_id, vehicle_id, "v.p.longitude", str(lon))
            publish_metric(client, user_id, vehicle_id, "v.p.speed", f"{speed:.1f}")
            publish_metric(client, user_id, vehicle_id, "v.p.altitude", f"{altitude:.1f}")
            publish_metric(client, user_id, vehicle_id, "m.time.utc", utc_time_str)
            publish_metric(client, user_id, vehicle_id, "v.b.soc", f"{soc:.2f}")
            
            if i < len(path) - 1:
                wait_time = 3
                print(f"    Waiting {wait_time} seconds before next point...")
                time.sleep(wait_time)

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