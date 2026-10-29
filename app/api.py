import os
import re
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.nl_query import (
    ModelResponseError,
    ModelUnavailableError,
    generate_sql,
)
from app.query import METRICS, QueryService
from app.storage import download_curated, s3_client

USER_ID = re.compile(r"user-[0-9]{4}\Z")
STATIC_DIR = Path(__file__).parent / "static"


class SQLQuery(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


class NaturalLanguageQuery(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


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
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def workbench():
    return FileResponse(STATIC_DIR / "index.html")


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


@app.get("/api/catalog")
def catalog(request: Request):
    return {"tables": _service(request).catalog()}


@app.get("/api/catalog/{table_name}/schema")
def table_schema(table_name: str, request: Request):
    try:
        return {
            "table": table_name,
            "columns": _service(request).table_schema(table_name),
        }
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/api/catalog/{table_name}/preview")
def table_preview(
    table_name: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
):
    try:
        result = _service(request).preview(table_name, limit)
        return {
            "table": table_name,
            **result,
        }
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except duckdb.Error as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/query")
def execute_query(body: SQLQuery, request: Request):
    try:
        result = _service(request).run_sql(body.sql)
        return {
            "sql": body.sql,
            **result,
        }
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except duckdb.Error as error:
        raise HTTPException(status_code=400, detail=f"Query failed: {error}") from error


@app.post("/api/nl-query")
def execute_natural_language_query(body: NaturalLanguageQuery, request: Request):
    query_service = _service(request)
    catalog = query_service.catalog()
    if not catalog:
        raise HTTPException(status_code=503, detail="No queryable tables are loaded")
    reference_date = max(
        date.fromisoformat(partition.name.removeprefix("event_date="))
        for table in METRICS
        for partition in (query_service.data_dir / table).glob("event_date=*")
        if partition.is_dir()
    )
    model = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
    try:
        sql = generate_sql(
            question=body.question,
            tables=catalog,
            reference_date=reference_date,
            endpoint=os.environ.get("OLLAMA_URL", "http://ollama:11434"),
            model=model,
        )
    except ModelUnavailableError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except ModelResponseError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    try:
        result = query_service.run_sql(sql)
    except ValueError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": (
                    "Generated SQL was rejected by the read-only query guard: "
                    f"{error}"
                ),
                "sql": sql,
            },
        ) from error
    except duckdb.Error as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": f"Generated SQL could not be executed: {error}",
                "sql": sql,
            },
        ) from error
    return {
        "question": body.question,
        "model": model,
        "sql": sql,
        **result,
    }


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
