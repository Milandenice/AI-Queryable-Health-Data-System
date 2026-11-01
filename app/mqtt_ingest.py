import json
import logging
import os
import threading

import paho.mqtt.client as mqtt

logger = logging.getLogger(__name__)


class MQTTIngestor:
    def __init__(self, store, host=None, port=None):
        self.store = store
        self.host = host or os.environ.get("MQTT_HOST", "mqtt")
        self.port = port or int(os.environ.get("MQTT_PORT", "1883"))
        self.connected = threading.Event()
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="health-api-ingestor",
            clean_session=False,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self):
        self.client.connect_async(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def stop(self):
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            logger.error("MQTT connection failed: %s", reason_code)
            self.connected.clear()
            return
        client.subscribe("health/heart_rate/+", qos=1)
        self.connected.set()
        logger.info("Connected to MQTT and subscribed to heart-rate stream")

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        self.connected.clear()
        if reason_code.is_failure:
            logger.warning("MQTT disconnected unexpectedly: %s", reason_code)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload)
            topic_parts = message.topic.split("/")
            if (
                len(topic_parts) != 3
                or topic_parts[:2] != ["health", "heart_rate"]
                or not isinstance(payload, dict)
                or payload.get("user_id") != topic_parts[2]
            ):
                raise ValueError("topic and heart-rate payload profile do not match")
            self.store.ingest(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            logger.exception("Rejected invalid heart-rate message on %s", message.topic)
        except Exception:
            logger.exception("Failed to persist heart-rate message on %s", message.topic)
