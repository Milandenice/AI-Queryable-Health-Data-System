import json
from datetime import date, datetime, timezone
from io import BytesIO

import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError
import pytest

from app.analytics import build_weekly_features
from app.etl import METRIC_SCHEMAS


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, Bucket, Prefix):
        return [
            {
                "Contents": [
                    {"Key": key}
                    for key in self.client.objects[Bucket]
                    if key.startswith(Prefix)
                ]
            }
        ]


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, _name):
        return FakePaginator(self)

    def get_object(self, Bucket, Key):
        try:
            return {"Body": BytesIO(self.objects[Bucket][Key])}
        except KeyError as error:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            ) from error

    def put_object(self, Bucket, Key, Body, **_kwargs):
        self.objects.setdefault(Bucket, {})[Key] = Body


def _encode_parquet(metric, records):
    output = BytesIO()
    pq.write_table(
        pa.Table.from_pylist(records, schema=METRIC_SCHEMAS[metric]),
        output,
    )
    return output.getvalue()


def test_computed_weekly_features_aggregate_metrics_and_weight_change():
    utc = timezone.utc
    records = {
        "_SUCCESS.json": json.dumps(
            {"source": {"version": 2}, "raw_objects": 5, "records": 8}
        ).encode(),
        "curated/heart_rate/event_date=2025-01-06/part-00000.parquet":
            _encode_parquet(
                "heart_rate",
                [
                    {
                        "event_id": "hr-1",
                        "user_id": "user-0001",
                        "observed_at": datetime(2025, 1, 6, 8, tzinfo=utc),
                        "bpm": 60,
                        "activity": "rest",
                    },
                    {
                        "event_id": "hr-2",
                        "user_id": "user-0001",
                        "observed_at": datetime(2025, 1, 6, 9, tzinfo=utc),
                        "bpm": 70,
                        "activity": "rest",
                    },
                ],
            ),
        "curated/steps/event_date=2025-01-06/part-00000.parquet":
            _encode_parquet(
                "steps",
                [
                    {
                        "event_id": "steps-1",
                        "user_id": "user-0001",
                        "bucket_start": datetime(2025, 1, 6, 8, tzinfo=utc),
                        "bucket_end": datetime(2025, 1, 6, 8, 15, tzinfo=utc),
                        "steps": 1000,
                    },
                    {
                        "event_id": "steps-2",
                        "user_id": "user-0001",
                        "bucket_start": datetime(2025, 1, 7, 8, tzinfo=utc),
                        "bucket_end": datetime(2025, 1, 7, 8, 15, tzinfo=utc),
                        "steps": 3000,
                    },
                ],
            ),
        "curated/sleep/event_date=2025-01-06/part-00000.parquet":
            _encode_parquet(
                "sleep",
                [
                    {
                        "event_id": "sleep-1",
                        "user_id": "user-0001",
                        "night_date": date(2025, 1, 6),
                        "start_at": datetime(2025, 1, 5, 22, tzinfo=utc),
                        "end_at": datetime(2025, 1, 6, 5, tzinfo=utc),
                        "stage": "deep",
                        "duration_minutes": 420,
                    },
                    {
                        "event_id": "sleep-2",
                        "user_id": "user-0001",
                        "night_date": date(2025, 1, 6),
                        "start_at": datetime(2025, 1, 6, 5, tzinfo=utc),
                        "end_at": datetime(2025, 1, 6, 5, 30, tzinfo=utc),
                        "stage": "awake",
                        "duration_minutes": 30,
                    },
                ],
            ),
        "curated/workouts/event_date=2025-01-06/part-00000.parquet":
            _encode_parquet(
                "workouts",
                [
                    {
                        "event_id": "workout-1",
                        "user_id": "user-0001",
                        "start_at": datetime(2025, 1, 6, 12, tzinfo=utc),
                        "end_at": datetime(2025, 1, 6, 12, 30, tzinfo=utc),
                        "activity_type": "run",
                        "intensity": "moderate",
                        "avg_heart_rate": 140,
                    }
                ],
            ),
        "curated/weight/event_date=2025-01-06/part-00000.parquet":
            _encode_parquet(
                "weight",
                [
                    {
                        "event_id": "weight-1",
                        "user_id": "user-0001",
                        "measured_at": datetime(2025, 1, 6, 7, tzinfo=utc),
                        "weight_kg": 80.0,
                    }
                ],
            ),
        "curated/weight/event_date=2025-01-10/part-00000.parquet":
            _encode_parquet(
                "weight",
                [
                    {
                        "event_id": "weight-2",
                        "user_id": "user-0001",
                        "measured_at": datetime(2025, 1, 10, 7, tzinfo=utc),
                        "weight_kg": 79.6,
                    }
                ],
            ),
    }
    client = FakeS3({"health-curated": records, "health-computed": {}})

    feature_rows = build_weekly_features(
        client, "health-curated", "health-computed"
    )

    assert feature_rows == 1
    feature_key = (
        "weekly_features/event_week=2025-01-06/part-00000.parquet"
    )
    table = pq.read_table(
        BytesIO(client.objects["health-computed"][feature_key])
    )
    row = table.to_pylist()[0]
    assert row["avg_resting_bpm"] == 65
    assert row["avg_daily_steps"] == 2000
    assert row["avg_sleep_hours"] == 7
    assert row["workout_count"] == 1
    assert row["workout_minutes"] == 30
    assert row["weight_kg"] == 79.6
    assert row["weight_change_kg"] == pytest.approx(-0.4)
    assert row["weight_change_days"] == pytest.approx(4)
    assert row["weight_change_kg_per_day"] == pytest.approx(-0.1)
    assert build_weekly_features(client, "health-curated", "health-computed") == 1
