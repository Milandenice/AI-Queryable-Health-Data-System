from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
import json

import pytest

from app import generate as generate_module
from app.generate import HeartRateGenerator
from app.heart_rate_store import HeartRateStore
from app.mqtt_ingest import MQTTIngestor


UTC = timezone.utc


def _reading(event_id, observed_at, bpm=80, activity="rest", user_id="user-0001"):
    return {
        "event_id": event_id,
        "user_id": user_id,
        "observed_at": observed_at.isoformat(),
        "bpm": bpm,
        "activity": activity,
    }


def test_synthetic_stream_is_reproducible_except_for_unique_event_ids():
    first = HeartRateGenerator("user-0001", seed=17, start_time=1000)
    second = HeartRateGenerator("user-0001", seed=17, start_time=1000)
    first_readings = [first.next_reading(1000 + index) for index in range(10)]
    second_readings = [second.next_reading(1000 + index) for index in range(10)]

    assert [
        {key: value for key, value in reading.items() if key != "event_id"}
        for reading in first_readings
    ] == [
        {key: value for key, value in reading.items() if key != "event_id"}
        for reading in second_readings
    ]
    assert len({reading["event_id"] for reading in first_readings}) == 10
    assert all(40 <= reading["bpm"] <= 200 for reading in first_readings)
    assert {reading["activity"] for reading in first_readings} <= {
        "rest",
        "active",
        "workout",
    }


def test_generator_uses_current_time_for_live_readings(monkeypatch):
    generator = HeartRateGenerator("user-0001", seed=17, start_time=1000)
    current_time = 1001.0
    monkeypatch.setattr(generate_module.time, "time", lambda: current_time)

    first = generator.next_reading()
    current_time = 1002.0
    second = generator.next_reading()

    assert first["observed_at"] == datetime.fromtimestamp(1001, UTC).isoformat()
    assert second["observed_at"] == datetime.fromtimestamp(1002, UTC).isoformat()


def test_stream_persists_readings_and_triggers_after_consecutive_crossing(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"), alert_after_samples=3)
    store.initialize()
    store.update_settings("user-0001", 50, 100, 3)
    start = datetime.now(UTC).replace(microsecond=0)

    assert store.ingest(_reading("high-0", start, bpm=101))
    assert store.snapshot()["alarm"]["triggered"] is False
    assert store.ingest(_reading("high-1", start + timedelta(seconds=1), bpm=110))
    assert store.snapshot()["alarm_events"] == []
    assert store.ingest(_reading("high-2", start + timedelta(seconds=2), bpm=115))
    triggered = store.snapshot()
    assert triggered["alarm"]["triggered"] is True
    assert triggered["alarm_events"][0]["event_type"] == "triggered"

    assert store.ingest(_reading("normal", start + timedelta(seconds=3), bpm=90))
    recovered = store.snapshot()
    assert recovered["alarm"] is None
    assert [event["event_type"] for event in recovered["alarm_events"]] == [
        "resolved",
        "triggered",
    ]


def test_alarm_uses_consecutive_samples_even_across_stream_gaps(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"), alert_after_samples=3)
    store.initialize()
    store.update_settings("user-0001", 50, 100, 3)
    start = datetime.now(UTC).replace(microsecond=0)

    store.ingest(_reading("gap-0", start, bpm=101))
    store.ingest(_reading("gap-1", start + timedelta(seconds=10), bpm=110))
    assert store.snapshot()["alarm"]["triggered"] is False
    store.ingest(_reading("gap-2", start + timedelta(seconds=20), bpm=111))
    assert store.snapshot()["alarm"]["triggered"] is True


def test_in_range_reading_resets_consecutive_alarm_count(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"), alert_after_samples=3)
    store.initialize()
    store.update_settings("user-0001", 50, 100, 3)
    start = datetime.now(UTC).replace(microsecond=0)

    store.ingest(_reading("high-1", start, bpm=110))
    store.ingest(_reading("high-2", start + timedelta(seconds=1), bpm=110))
    store.ingest(_reading("normal", start + timedelta(seconds=2), bpm=100))
    store.ingest(_reading("high-3", start + timedelta(seconds=3), bpm=110))
    store.ingest(_reading("high-4", start + timedelta(seconds=4), bpm=110))
    assert store.snapshot()["alarm"]["consecutive_samples"] == 2
    store.ingest(_reading("high-5", start + timedelta(seconds=5), bpm=110))

    assert store.snapshot()["alarm"]["triggered"] is True


def test_alarm_triggers_on_fifteenth_consecutive_sample(tmp_path):
    store = HeartRateStore(
        str(tmp_path / "heart-rate.sqlite3"),
        alert_after_samples=15,
    )
    store.initialize()
    store.update_settings("user-0001", 50, 100, 15)
    start = datetime.now(UTC).replace(microsecond=0)

    for index in range(14):
        store.ingest(_reading(f"high-{index}", start + timedelta(seconds=index), bpm=110))
    assert store.snapshot()["alarm"]["triggered"] is False
    assert store.snapshot()["alarm"]["consecutive_samples"] == 14
    store.ingest(_reading("high-14", start + timedelta(seconds=14), bpm=110))

    snapshot = store.snapshot()
    assert snapshot["alarm"]["triggered"] is True
    assert snapshot["alarm"]["consecutive_samples"] == 15
    assert snapshot["alarm_events"][0]["event_type"] == "triggered"


def test_low_alarm_settings_validation_and_duplicate_event_handling(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"), alert_after_samples=1)
    store.initialize()
    store.update_settings("user-0001", 60, 120, 1)
    start = datetime.now(UTC).replace(microsecond=0)
    first = _reading("low-0", start, bpm=55)

    assert store.ingest(first) is True
    assert store.ingest(first) is False
    store.ingest(_reading("low-1", start + timedelta(seconds=1), bpm=55))
    assert store.snapshot()["alarm"]["direction"] == "low"
    assert store.snapshot()["alarm"]["triggered"] is True
    with pytest.raises(ValueError, match="lower than"):
        store.update_settings("user-0001", 130, 120, 10)
    with pytest.raises(ValueError, match="between 35 and 220"):
        store.ingest(_reading("invalid", start, bpm=300))


def test_existing_settings_migrate_to_consecutive_sample_threshold(tmp_path):
    database = tmp_path / "heart-rate.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE alarm_settings (
                user_id TEXT PRIMARY KEY,
                low_bpm INTEGER NOT NULL,
                high_bpm INTEGER NOT NULL,
                alert_after_seconds INTEGER NOT NULL
            );
            INSERT INTO alarm_settings VALUES ('user-0001', 55, 125, 10);
            CREATE TABLE alarm_state (
                user_id TEXT PRIMARY KEY,
                direction TEXT,
                outside_since_ms INTEGER,
                last_observed_ms INTEGER,
                triggered INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO alarm_state
                VALUES ('user-0001', 'high', 1, 2, 1);
            """
        )

    store = HeartRateStore(str(database))
    store.initialize()

    assert store.settings() == {
        "low_bpm": 55,
        "high_bpm": 125,
        "alert_after_samples": 15,
    }
    assert store.snapshot()["alarm"] is None


def test_resting_baseline_is_derived_from_saved_samples(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"))
    store.initialize()
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    for index, bpm in enumerate((60, 62, 64)):
        store.ingest(
            _reading(
                f"rest-{index}",
                start + timedelta(minutes=index),
                bpm=bpm,
            )
        )

    baseline = store.snapshot()["personal_baseline"]
    assert baseline["sample_minutes"] == 3
    assert baseline["average_bpm"] == 62
    assert baseline["median_bpm"] == 62


def test_configured_profile_is_the_default_monitor_profile(tmp_path):
    store = HeartRateStore(
        str(tmp_path / "heart-rate.sqlite3"),
        default_user_id="user-0007",
    )
    store.initialize()

    assert store.users() == ["user-0007"]
    assert store.snapshot()["user_id"] == "user-0007"


def test_mqtt_ingestor_checks_topic_matches_payload_profile(tmp_path):
    store = HeartRateStore(str(tmp_path / "heart-rate.sqlite3"))
    store.initialize()
    ingestor = MQTTIngestor(store)
    observed_at = datetime.now(UTC).replace(microsecond=0)
    payload = _reading("mqtt-1", observed_at)

    ingestor._on_message(
        None,
        None,
        SimpleNamespace(
            topic="health/heart_rate/user-0001",
            payload=json.dumps(payload).encode(),
        ),
    )
    ingestor._on_message(
        None,
        None,
        SimpleNamespace(
            topic="health/heart_rate/user-0002",
            payload=json.dumps({**payload, "event_id": "mqtt-2"}).encode(),
        ),
    )

    assert store.snapshot()["latest"]["event_id"] == "mqtt-1"
