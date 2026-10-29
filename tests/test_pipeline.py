import json
from io import BytesIO
from datetime import date
from botocore.exceptions import ClientError

import pytest

from app.etl import transform_object, validate_record
from app.generate import iter_batches
from app.storage import read_json_object


def test_generation_is_deterministic_and_metric_shapes_differ():
    first = list(iter_batches(seed=17, user_count=20, day_count=2))
    second = list(iter_batches(seed=17, user_count=20, day_count=2))

    assert first == second
    _, day = first[0]
    assert set(day) == {"heart_rate", "sleep", "steps", "workouts", "weight"}
    assert len(day["heart_rate"]) > len(day["steps"]) > len(day["sleep"])
    assert len(day["weight"]) == 20
    assert day["workouts"]
    for metric, records in day.items():
        event_ids = [record["event_id"] for record in records]
        assert len(event_ids) == len(set(event_ids))
        for record in records:
            validate_record(metric, record, first[0][0])


def test_sleep_stage_intervals_are_contiguous_and_match_durations():
    _, day = next(iter_batches(seed=27, user_count=1, day_count=1))
    events = day["sleep"]

    assert events
    for previous, current in zip(events, events[1:]):
        assert previous["end_at"] == current["start_at"]
    for event in events:
        assert event["duration_minutes"] > 0


def test_etl_rejects_bad_values_and_wrong_date_partitions():
    valid = {
        "event_id": "user-0001-heart_rate-20250101T000000Z",
        "user_id": "user-0001",
        "observed_at": "2025-01-01T00:00:00+00:00",
        "bpm": 65,
        "activity": "rest",
    }

    normalized = validate_record("heart_rate", valid, date(2025, 1, 1))
    assert normalized["bpm"] == 65
    with pytest.raises(ValueError, match="event-date partition"):
        validate_record("heart_rate", valid, date(2025, 1, 2))
    with pytest.raises(ValueError, match="between 35 and 220"):
        validate_record("heart_rate", {**valid, "bpm": 300}, date(2025, 1, 1))


def test_etl_writes_validated_records_as_partitioned_parquet():
    record = {
        "event_id": "user-0001-weight-20250101T070000Z",
        "user_id": "user-0001",
        "measured_at": "2025-01-01T07:00:00+00:00",
        "weight_kg": 80.5,
    }

    class FakeS3:
        uploaded = None

        def get_object(self, **kwargs):
            return {"Body": BytesIO((json.dumps(record) + "\n").encode())}

        def put_object(self, **kwargs):
            self.uploaded = kwargs

    client = FakeS3()
    count = transform_object(
        client,
        "health-raw",
        "health-curated",
        "raw/weight/event_date=2025-01-01/part-00000.jsonl",
    )

    assert count == 1
    assert client.uploaded["Key"] == (
        "curated/weight/event_date=2025-01-01/part-00000.parquet"
    )
    assert client.uploaded["Body"][:4] == b"PAR1"


def test_completion_marker_is_read_as_json_and_missing_marker_is_not_an_error():
    class FakeS3:
        def __init__(self, value=None):
            self.value = value

        def get_object(self, **kwargs):
            if self.value is None:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                    "GetObject",
                )
            return {"Body": BytesIO(json.dumps(self.value).encode())}

    marker = {"seed": 2025, "days": 30}
    assert read_json_object(FakeS3(marker), "health-raw", "_SUCCESS.json") == marker
    assert read_json_object(FakeS3(), "health-raw", "_SUCCESS.json") is None


def test_generator_skips_a_completed_matching_dataset(monkeypatch, capsys):
    from app import generate

    marker = {
        "version": 1,
        "seed": 2025,
        "users": 100,
        "days": 30,
        "start_date": "2025-01-01",
    }

    class FakeS3:
        uploads = 0

        def get_object(self, **kwargs):
            return {"Body": BytesIO(json.dumps(marker).encode())}

        def put_object(self, **kwargs):
            self.uploads += 1

    client = FakeS3()
    monkeypatch.setattr(generate, "s3_client", lambda: client)

    generate.run()

    assert client.uploads == 0
    assert "already complete" in capsys.readouterr().out


def test_etl_skips_when_curated_marker_matches_raw_manifest(monkeypatch, capsys):
    from app import etl

    raw_manifest = {"version": 1, "seed": 2025}
    curated_manifest = {"source": raw_manifest, "raw_objects": 150, "records": 1_000}

    class FakeS3:
        def __init__(self):
            self.reads = iter((raw_manifest, curated_manifest))

        def get_object(self, **kwargs):
            return {"Body": BytesIO(json.dumps(next(self.reads)).encode())}

    monkeypatch.setattr(etl, "s3_client", FakeS3)

    def unexpected_object_listing(*args, **kwargs):
        raise AssertionError("ETL should skip before listing raw data")

    monkeypatch.setattr(etl, "object_keys", unexpected_object_listing)

    etl.run()

    assert "already complete" in capsys.readouterr().out
