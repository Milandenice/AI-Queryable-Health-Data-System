import json
import math
import os
import random
import re
import signal
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

USER_ID = os.environ.get("SYNTHETIC_USER_ID", "user-0001")
SAMPLE_RATE_SECONDS = float(os.environ.get("SAMPLE_RATE_SECONDS", "1"))
MQTT_HOST = os.environ.get("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
DATA_SEED = int(os.environ.get("DATA_SEED", "2025"))


class HeartRateGenerator:
    def __init__(self, user_id=USER_ID, seed=DATA_SEED, start_time=None):
        self.user_id = user_id
        self.random = random.Random(seed)
        self.started_at = start_time or time.time()
        self.workout_until = 0.0
        self.next_workout_check = 0.0

    def next_reading(self, timestamp=None):
        now = time.time() if timestamp is None else timestamp
        elapsed = now - self.started_at
        day_phase = (elapsed % 86400) / 86400
        circadian = -4 * math.cos((day_phase - 0.125) * 2 * math.pi)
        if now >= self.next_workout_check:
            if now >= self.workout_until and self.random.random() < 0.002:
                self.workout_until = now + self.random.randint(30, 120)
            self.next_workout_check = now + 10

        if now < self.workout_until:
            activity = "workout"
            bpm = 68 + circadian + self.random.gauss(58, 8)
        else:
            active_phase = (elapsed % 86400) / 86400
            active = 0.25 < active_phase < 0.9 and self.random.random() < 0.25
            activity = "active" if active else "rest"
            bpm = 68 + circadian + self.random.gauss(12 if active else 0, 3.2)
        return {
            "event_id": str(uuid.uuid4()),
            "user_id": self.user_id,
            "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "bpm": max(40, min(200, round(bpm))),
            "activity": activity,
        }


def _wait_for_connection(client, is_stopped):
    while not client.is_connected() and not is_stopped():
        time.sleep(0.2)


def run():
    if not 0.1 <= SAMPLE_RATE_SECONDS <= 2:
        raise ValueError("SAMPLE_RATE_SECONDS must be between 0.1 and 2")
    if not re.fullmatch(r"user-[0-9]{4}", USER_ID):
        raise ValueError("SYNTHETIC_USER_ID must match user-NNNN")
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="synthetic-heart-rate-generator",
        clean_session=False,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    generator = HeartRateGenerator()
    stopped = False

    def stop(_signal, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopped:
            _wait_for_connection(client, lambda: stopped)
            if stopped:
                break
            reading = generator.next_reading()
            result = client.publish(
                f"health/heart_rate/{generator.user_id}",
                json.dumps(reading, separators=(",", ":")),
                qos=1,
            )
            result.wait_for_publish(timeout=5)
            time.sleep(SAMPLE_RATE_SECONDS)
    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    run()
