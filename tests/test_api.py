from contextlib import asynccontextmanager
from datetime import date, datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from app.api import app
from app.etl import METRIC_SCHEMAS
from app.query import QueryService


def test_http_query_routes_validate_and_return_results(tmp_path):
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

    @asynccontextmanager
    async def test_lifespan(application):
        application.state.query_service = service
        yield

    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = test_lifespan
    try:
        with TestClient(app) as client:
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

            invalid = client.get(
                "/metrics/unknown",
                params={
                    "user_id": "user-0001",
                    "start_date": day.isoformat(),
                    "end_date": day.isoformat(),
                },
            )
            assert invalid.status_code == 404
    finally:
        app.router.lifespan_context = original_lifespan
