from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.analytics import WEEKLY_FEATURE_SCHEMA
from app.etl import METRIC_SCHEMAS
from app.query import QueryService

DAY = date(2025, 1, 1)
UTC = timezone.utc


def _write_metric(root, metric, record):
    target = root / metric / "event_date=2025-01-01" / "part-00000.parquet"
    target.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist([record], schema=METRIC_SCHEMAS[metric]), target
    )


def test_duckdb_reads_records_and_aligns_daily_metric_summaries(tmp_path):
    _write_metric(
        tmp_path,
        "heart_rate",
        {
            "event_id": "hr-1",
            "user_id": "user-0001",
            "observed_at": datetime(2025, 1, 1, 8, tzinfo=UTC),
            "bpm": 72,
            "activity": "rest",
        },
    )
    _write_metric(
        tmp_path,
        "sleep",
        {
            "event_id": "sleep-1",
            "user_id": "user-0001",
            "night_date": DAY,
            "start_at": datetime(2025, 1, 1, 0, tzinfo=UTC),
            "end_at": datetime(2025, 1, 1, 7, tzinfo=UTC),
            "stage": "deep",
            "duration_minutes": 420,
        },
    )
    _write_metric(
        tmp_path,
        "steps",
        {
            "event_id": "steps-1",
            "user_id": "user-0001",
            "bucket_start": datetime(2025, 1, 1, 8, tzinfo=UTC),
            "bucket_end": datetime(2025, 1, 1, 8, 15, tzinfo=UTC),
            "steps": 500,
        },
    )
    _write_metric(
        tmp_path,
        "workouts",
        {
            "event_id": "workout-1",
            "user_id": "user-0001",
            "start_at": datetime(2025, 1, 1, 9, tzinfo=UTC),
            "end_at": datetime(2025, 1, 1, 10, tzinfo=UTC),
            "activity_type": "run",
            "intensity": "moderate",
            "avg_heart_rate": 145,
        },
    )
    _write_metric(
        tmp_path,
        "weight",
        {
            "event_id": "weight-1",
            "user_id": "user-0001",
            "measured_at": datetime(2025, 1, 1, 7, tzinfo=UTC),
            "weight_kg": 80.5,
        },
    )

    service = QueryService(str(tmp_path))
    records = service.records("heart_rate", "user-0001", DAY, DAY, 10)
    assert records[0]["bpm"] == 72

    summary = service.summary(
        "user-0001", DAY, DAY, ["heart_rate", "sleep", "steps", "workouts", "weight"]
    )
    assert len(summary) == 1
    assert summary[0]["heart_rate"]["average_bpm"] == 72.0
    assert summary[0]["sleep"]["sleep_minutes"] == 420
    assert summary[0]["steps"]["total_steps"] == 500
    assert summary[0]["workouts"]["workout_minutes"] == 60.0
    assert summary[0]["weight"]["weight_kg"] == 80.5

    sql_result = service.run_sql(
        "SELECT user_id, round(avg(bpm), 1) AS average_bpm "
        "FROM heart_rate WHERE user_id = 'user-0001' GROUP BY user_id"
    )
    assert sql_result["columns"] == ["user_id", "average_bpm"]
    assert sql_result["rows"] == [{"user_id": "user-0001", "average_bpm": 72.0}]

    filtered_result = service.run_sql(
        "SELECT bpm FROM heart_rate "
        "WHERE activity = 'rest' AND bpm > 70 OR user_id = 'nobody'"
    )
    assert filtered_result["rows"] == [{"bpm": 72}]

    catalog = {table["name"]: table for table in service.catalog()}
    assert set(catalog) == {"heart_rate", "sleep", "steps", "workouts", "weight"}
    assert catalog["heart_rate"]["file_count"] == 1
    assert service.table_schema("weight")[-1] == {
        "name": "event_date",
        "type": "DATE",
    }


def test_sql_query_only_allows_single_selects_over_known_tables(tmp_path):
    import pytest

    from app.query import QueryService

    service = QueryService(str(tmp_path))
    for sql, message in [
        ("DELETE FROM heart_rate", "read-only SELECT"),
        ("SELECT * FROM users", "Unknown table"),
        ("SELECT * FROM read_parquet('/etc/passwd')", "external data sources"),
        ("SELECT read_text('/etc/passwd')", "is not allowed"),
        ("SELECT 1; DROP TABLE heart_rate", "read-only SELECT"),
        (
            "WITH removed AS (DELETE FROM heart_rate RETURNING *) SELECT * FROM removed",
            "Data-changing",
        ),
    ]:
        with pytest.raises(ValueError, match=message):
            service.run_sql(sql)


def test_query_service_returns_only_selected_user_weekly_metrics(tmp_path):
    features_dir = (
        tmp_path / "computed" / "weekly_features" / "event_week=2025-01-06"
    )
    features_dir.mkdir(parents=True)
    records = []
    for index in range(40):
        records.append(
            {
                "user_id": f"user-{index + 1:04d}",
                "week_start": date(2025, 1, 6),
                "avg_resting_bpm": 80 - index,
                "avg_daily_steps": 1000 + index * 100,
                "avg_sleep_hours": 6 + index / 10,
                "workout_count": index % 5,
                "workout_minutes": index * 10,
                "weight_kg": 80.0,
                "weight_change_kg": index / 100,
                "weight_change_days": 7,
                "weight_change_kg_per_day": index / 700,
            }
        )
    pq.write_table(
        pa.Table.from_pylist(records, schema=WEEKLY_FEATURE_SCHEMA),
        features_dir / "part-00000.parquet",
    )

    result = QueryService(str(tmp_path)).insights("user-0001")

    assert result["user_id"] == "user-0001"
    assert set(result) == {
        "user_id",
        "weekly",
        "personal_analysis",
        "weight_prediction",
    }
    assert len(result["weekly"]) == 1
    assert result["weekly"][0]["avg_daily_steps"] == 1000
    assert result["weight_prediction"]["status"] == "insufficient_data"
    assert QueryService(str(tmp_path)).insight_users() == [
        f"user-{index + 1:04d}" for index in range(40)
    ]
