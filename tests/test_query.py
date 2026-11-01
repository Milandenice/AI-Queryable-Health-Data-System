from datetime import date, datetime, timedelta, timezone

import pytest

from app.heart_rate_store import HeartRateStore
from app.query import QueryService

UTC = timezone.utc


def _reading(event_id, observed_at, bpm, activity="rest"):
    return {
        "event_id": event_id,
        "user_id": "user-0001",
        "observed_at": observed_at.isoformat(),
        "bpm": bpm,
        "activity": activity,
    }


def test_sqlite_queries_read_and_summarize_saved_heart_rate(tmp_path):
    database = tmp_path / "heart-rate.sqlite3"
    store = HeartRateStore(str(database))
    store.initialize()
    day = datetime(2025, 1, 1, tzinfo=UTC)
    store.ingest(_reading("hr-1", day, 72))
    store.ingest(_reading("hr-2", day + timedelta(minutes=5), 84, "active"))
    service = QueryService(str(database))

    records = service.records("heart_rate", "user-0001", date(2025, 1, 1), date(2025, 1, 1), 10)
    assert [row["bpm"] for row in records] == [84, 72]

    summary = service.summary(
        "user-0001", date(2025, 1, 1), date(2025, 1, 1), ["heart_rate"]
    )
    assert summary == [
        {
            "date": "2025-01-01",
            "heart_rate": {
                "average_bpm": 78.0,
                "min_bpm": 72,
                "max_bpm": 84,
                "observations": 2,
            },
        }
    ]

    result = service.run_sql(
        "SELECT user_id, round(avg(bpm), 1) AS average_bpm "
        "FROM heart_rate WHERE activity = 'rest' GROUP BY user_id"
    )
    assert result["columns"] == ["user_id", "average_bpm"]
    assert result["rows"] == [{"user_id": "user-0001", "average_bpm": 72.0}]
    catalog = service.catalog()
    assert [table["name"] for table in catalog] == ["heart_rate"]
    assert catalog[0]["record_count"] == 2
    assert service.table_schema("heart_rate")[0] == {
        "name": "event_id",
        "type": "TEXT",
    }
    assert service.preview("heart_rate", limit=1)["row_count"] == 1


def test_sql_query_only_allows_single_selects_over_heart_rate(tmp_path):
    database = tmp_path / "heart-rate.sqlite3"
    HeartRateStore(str(database)).initialize()
    service = QueryService(str(database))

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


def test_read_only_query_supports_boolean_filters(tmp_path):
    database = tmp_path / "heart-rate.sqlite3"
    store = HeartRateStore(str(database))
    store.initialize()
    store.ingest(
        _reading("hr-1", datetime(2025, 1, 1, tzinfo=UTC), 72, "rest")
    )
    service = QueryService(str(database))

    result = service.run_sql(
        "SELECT bpm FROM heart_rate "
        "WHERE activity = 'rest' AND bpm > 70 OR user_id = 'nobody'"
    )
    assert result["rows"] == [{"bpm": 72}]
