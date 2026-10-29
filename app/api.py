import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request

from app.query import METRICS, QueryService
from app.storage import download_curated, s3_client

USER_ID = re.compile(r"user-[0-9]{4}\Z")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache_dir = tempfile.mkdtemp(prefix="health-data-curated-")
    bucket = os.environ.get("CURATED_BUCKET", "health-curated")
    count = download_curated(s3_client(), bucket, cache_dir)
    app.state.query_service = QueryService(cache_dir)
    print(f"Loaded {count} curated objects from {bucket}", flush=True)
    yield


app = FastAPI(
    title="AI-Queryable Health Data System",
    description="Read-only queries over synthetic health metrics.",
    version="1.0.0",
    lifespan=lifespan,
)


def _validate_user_id(user_id: str) -> None:
    if not USER_ID.fullmatch(user_id):
        raise HTTPException(status_code=422, detail="user_id must match user-NNNN")


def _service(request: Request) -> QueryService:
    service = getattr(request.app.state, "query_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Curated data is not loaded")
    return service


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics/summary")
def summary(
    request: Request,
    user_id: str,
    start_date: date,
    end_date: date,
    metrics: str = "heart_rate,sleep,steps,workouts,weight",
):
    _validate_user_id(user_id)
    selected = [metric.strip() for metric in metrics.split(",") if metric.strip()]
    if not selected or any(metric not in METRICS for metric in selected):
        raise HTTPException(
            status_code=422,
            detail=f"metrics must be comma-separated values from {sorted(METRICS)}",
        )
    try:
        return {
            "user_id": user_id,
            "start_date": start_date,
            "end_date": end_date,
            "metrics": selected,
            "grain": "day",
            "results": _service(request).summary(
                user_id, start_date, end_date, selected
            ),
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/metrics/{metric}")
def records(
    metric: str,
    request: Request,
    user_id: str,
    start_date: date,
    end_date: date,
    limit: int = Query(default=100, ge=1, le=1000),
):
    _validate_user_id(user_id)
    if metric not in METRICS:
        raise HTTPException(
            status_code=404, detail=f"Unsupported metric: {metric}"
        )
    try:
        return {
            "metric": metric,
            "user_id": user_id,
            "start_date": start_date,
            "end_date": end_date,
            "limit": limit,
            "results": _service(request).records(
                metric, user_id, start_date, end_date, limit
            ),
        }
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
