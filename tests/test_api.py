from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from app import api as api_module
from app.analytics import WEEKLY_FEATURE_SCHEMA
from app.api import app
from app.etl import METRIC_SCHEMAS
from app.query import QueryService


def test_http_query_routes_validate_and_return_results(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
    day = date(2025, 1, 1)
    target = tmp_path / "heart_rate" / "event_date=2025-01-01" / "part-00000.parquet"
    target.parent.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "event_id": "hr-1",
                    "user_id": "user-0001",
                    "observed_at": datetime(2025, 1, 1, 8, tzinfo=timezone.utc),
                    "bpm": 72,
                    "activity": "rest",
                }
            ],
            schema=METRIC_SCHEMAS["heart_rate"],
        ),
        target,
    )
    service = QueryService(str(tmp_path))
    features = tmp_path / "computed" / "weekly_features" / "event_week=2024-12-30"
    features.mkdir(parents=True)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "user_id": "user-0001",
                    "week_start": date(2024, 12, 30),
                    "avg_resting_bpm": 72.0,
                    "avg_daily_steps": 5000.0,
                    "avg_sleep_hours": 7.0,
                    "workout_count": 1,
                    "workout_minutes": 30.0,
                    "weight_kg": 80.5,
                    "weight_change_kg": None,
                    "weight_change_days": None,
                    "weight_change_kg_per_day": None,
                }
            ],
            schema=WEEKLY_FEATURE_SCHEMA,
        ),
        features / "part-00000.parquet",
    )

    @asynccontextmanager
    async def test_lifespan(application):
        application.state.query_service = service
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan
    try:
        with TestClient(app) as client:
            workbench = client.get("/")
            assert workbench.status_code == 200
            assert "Health Data Query Workbench" in workbench.text
            insights_page = client.get("/insights")
            assert insights_page.status_code == 200
            assert "Welcome back" in insights_page.text
            assert 'id="weekly-rows"' not in insights_page.text

            insight_users = client.get("/api/insights/users")
            assert insight_users.status_code == 200
            assert insight_users.json() == {"users": ["user-0001"]}

            insights = client.get("/api/insights", params={"user_id": "user-0001"})
            assert insights.status_code == 200
            assert insights.json()["weekly"][0]["avg_daily_steps"] == 5000.0
            assert insights.json()["weight_prediction"]["status"] == "insufficient_data"
            assert insights.json()["personal_analysis"]["associations"]
            assert set(insights.json()) == {
                "user_id",
                "weekly",
                "personal_analysis",
                "weight_prediction",
            }

            cohort = client.get("/api/insights/cohort")
            assert cohort.status_code == 200
            assert len(cohort.json()["associations"]) == 4
            assert cohort.json()["model_evaluation"]["status"] == "insufficient_data"

            response = client.get(
                "/metrics/heart_rate",
                params={
                    "user_id": "user-0001",
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                    "limit": 10,
                },
            )
            assert response.status_code == 200
            assert response.json()["results"][0]["bpm"] == 72

            catalog = client.get("/api/catalog")
            assert catalog.status_code == 200
            assert [table["name"] for table in catalog.json()["tables"]] == [
                "heart_rate"
            ]

            schema = client.get("/api/catalog/heart_rate/schema")
            assert schema.status_code == 200
            assert schema.json()["columns"][0] == {
                "name": "event_id",
                "type": "string",
            }

            preview = client.get("/api/catalog/heart_rate/preview")
            assert preview.status_code == 200
            assert preview.json()["rows"][0]["bpm"] == 72

            sql_result = client.post(
                "/api/query",
                json={
                    "sql": "SELECT user_id, avg(bpm) AS average_bpm "
                    "FROM heart_rate GROUP BY user_id"
                },
            )
            assert sql_result.status_code == 200
            assert sql_result.json()["rows"][0]["average_bpm"] == 72.0

            summary = client.get(
                "/metrics/summary",
                params={
                    "user_id": "user-0001",
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                    "metrics": "heart_rate",
                },
            )
            assert summary.status_code == 200
            assert summary.json()["grain"] == "day"
            assert summary.json()["results"][0]["heart_rate"]["average_bpm"] == 72.0

            invalid = client.get(
                "/metrics/unknown",
                params={
                    "user_id": "user-0001",
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                },
            )
            assert invalid.status_code == 404

            invalid_range = client.get(
                "/metrics/heart_rate",
                params={
                    "user_id": "user-0001",
                    "start_date": "2025-01-02",
                    "end_date": "2025-01-01",
                },
            )
            assert invalid_range.status_code == 422

            invalid_user = client.get(
                "/metrics/heart_rate",
                params={
                    "user_id": "not-a-user",
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                },
            )
            assert invalid_user.status_code == 422

            forbidden_query = client.post(
                "/api/query", json={"sql": "DROP TABLE heart_rate"}
            )
            assert forbidden_query.status_code == 400

            def generated_sql(**kwargs):
                assert kwargs["question"] == "average resting heart rate"
                assert kwargs["reference_date"] == day
                assert kwargs["model"] == "qwen2.5:3b"
                return (
                    "SELECT avg(bpm) AS average_bpm FROM heart_rate "
                    "WHERE activity = 'rest'"
                )

            monkeypatch.setattr(api_module, "generate_sql", generated_sql)
            nl_result = client.post(
                "/api/nl-query", json={"question": "average resting heart rate"}
            )
            assert nl_result.status_code == 200
            assert nl_result.json()["sql"].startswith("SELECT avg(bpm)")
            assert nl_result.json()["rows"] == [{"average_bpm": 72.0}]
            assert nl_result.json()["model"] == "qwen2.5:3b"

            monkeypatch.setattr(
                api_module, "generate_sql", lambda **kwargs: "DROP TABLE heart_rate"
            )
            rejected_nl_sql = client.post(
                "/api/nl-query", json={"question": "remove the heart rate table"}
            )
            assert rejected_nl_sql.status_code == 502
            assert (
                "read-only query guard"
                in rejected_nl_sql.json()["detail"]["message"]
            )
            assert rejected_nl_sql.json()["detail"]["sql"] == (
                "DROP TABLE heart_rate"
            )

            def unavailable_model(**_kwargs):
                raise api_module.ModelUnavailableError("Ollama is unavailable")

            monkeypatch.setattr(api_module, "generate_sql", unavailable_model)
            unavailable = client.post(
                "/api/nl-query", json={"question": "show my recent steps"}
            )
            assert unavailable.status_code == 503
            assert unavailable.json()["detail"] == "Ollama is unavailable"
    finally:
        app.router.lifespan_context = original_lifespan
