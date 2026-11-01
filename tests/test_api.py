from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import api as api_module
from app.api import app
from app.heart_rate_store import HeartRateStore
from app.query import QueryService


def test_http_monitor_and_workbench_routes_use_persisted_stream(tmp_path, monkeypatch):
    database = tmp_path / "heart-rate.sqlite3"
    store = HeartRateStore(str(database))
    store.initialize()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    store.ingest(
        {
            "event_id": "hr-1",
            "user_id": "user-0001",
            "observed_at": now.isoformat(),
            "bpm": 72,
            "activity": "rest",
        }
    )
    service = QueryService(str(database))

    class Connected:
        def is_set(self):
            return True

    class Ingestor:
        connected = Connected()

    @asynccontextmanager
    async def test_lifespan(application):
        application.state.query_service = service
        application.state.heart_rate_store = store
        application.state.mqtt_ingestor = Ingestor()
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan
    try:
        with TestClient(app) as client:
            workbench = client.get("/")
            assert workbench.status_code == 200
            assert "Heart Rate" in workbench.text

            monitor_page = client.get("/monitor")
            assert monitor_page.status_code == 200
            assert "Live monitor" in monitor_page.text
            assert "not medical guidance" in monitor_page.text

            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/api/monitor/users").json() == {
                "users": ["user-0001"]
            }

            snapshot = client.get("/api/monitor")
            assert snapshot.status_code == 200
            assert snapshot.json()["latest"]["bpm"] == 72
            assert snapshot.json()["settings"] == {
                "low_bpm": 50,
                "high_bpm": 120,
                "alert_after_samples": 15,
            }

            updated = client.put(
                "/api/monitor/settings",
                json={
                    "user_id": "user-0001",
                    "low_bpm": 55,
                    "high_bpm": 125,
                    "alert_after_samples": 12,
                },
            )
            assert updated.status_code == 200
            assert updated.json()["high_bpm"] == 125
            invalid_settings = client.put(
                "/api/monitor/settings",
                json={
                    "user_id": "user-0001",
                    "low_bpm": 130,
                    "high_bpm": 120,
                    "alert_after_samples": 12,
                },
            )
            assert invalid_settings.status_code == 422

            response = client.get(
                "/metrics/heart_rate",
                params={
                    "user_id": "user-0001",
                    "start_date": now.date().isoformat(),
                    "end_date": now.date().isoformat(),
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
                "type": "TEXT",
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
            assert client.post(
                "/api/query", json={"sql": "DROP TABLE heart_rate"}
            ).status_code == 400

            summary = client.get(
                "/metrics/summary",
                params={
                    "user_id": "user-0001",
                    "start_date": now.date().isoformat(),
                    "end_date": now.date().isoformat(),
                },
            )
            assert summary.status_code == 200
            assert summary.json()["metrics"] == ["heart_rate"]
            assert summary.json()["results"][0]["heart_rate"]["average_bpm"] == 72.0

            def generated_sql(**kwargs):
                assert kwargs["question"] == "average resting heart rate"
                assert kwargs["reference_date"] == now.date()
                assert kwargs["model"] == "qwen2.5:3b"
                return (
                    "SELECT avg(bpm) AS average_bpm FROM heart_rate "
                    "WHERE activity = 'rest'"
                )

            monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
            monkeypatch.setattr(api_module, "generate_sql", generated_sql)
            nl_result = client.post(
                "/api/nl-query", json={"question": "average resting heart rate"}
            )
            assert nl_result.status_code == 200
            assert nl_result.json()["sql"].startswith("SELECT avg(bpm)")
            assert nl_result.json()["rows"] == [{"average_bpm": 72.0}]

            monkeypatch.setattr(
                api_module, "generate_sql", lambda **_kwargs: "DROP TABLE heart_rate"
            )
            rejected = client.post(
                "/api/nl-query", json={"question": "remove heart rate table"}
            )
            assert rejected.status_code == 502
            assert "read-only query guard" in rejected.json()["detail"]["message"]

            def unavailable_model(**_kwargs):
                raise api_module.ModelUnavailableError("Ollama is unavailable")

            monkeypatch.setattr(api_module, "generate_sql", unavailable_model)
            unavailable = client.post(
                "/api/nl-query", json={"question": "show recent heart rate"}
            )
            assert unavailable.status_code == 503
    finally:
        app.router.lifespan_context = original_lifespan
