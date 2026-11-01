import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.heart_rate_store import DEFAULT_USER_ID, HeartRateStore
from app.mqtt_ingest import MQTTIngestor
from app.nl_query import (
    ModelResponseError,
    ModelUnavailableError,
    generate_sql,
)
from app.query import METRICS, QueryService

USER_ID = re.compile(r"user-[0-9]{4}\Z")
STATIC_DIR = Path(__file__).parent / "static"


class SQLQuery(BaseModel):
    sql: str = Field(min_length=1, max_length=20000)


class NaturalLanguageQuery(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


class AlarmSettings(BaseModel):
    user_id: str = Field(default=DEFAULT_USER_ID, pattern=r"^user-[0-9]{4}$")
    low_bpm: int = Field(ge=35, le=219)
    high_bpm: int = Field(ge=36, le=220)
    alert_after_samples: int = Field(ge=1, le=300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = HeartRateStore(
        os.environ.get("HEART_RATE_DB", "/data/heart-rate.sqlite3"),
        low_bpm=int(os.environ.get("ALARM_LOW_BPM", "50")),
        high_bpm=int(os.environ.get("ALARM_HIGH_BPM", "120")),
        alert_after_samples=int(os.environ.get("ALARM_AFTER_SAMPLES", "15")),
        default_user_id=os.environ.get("SYNTHETIC_USER_ID", DEFAULT_USER_ID),
    )
    store.initialize()
    ingestor = MQTTIngestor(store)
    ingestor.start()
    app.state.heart_rate_store = store
    app.state.mqtt_ingestor = ingestor
    app.state.query_service = QueryService(str(store.database_path))
    print(f"Heart-rate history stored at {store.database_path}", flush=True)
    try:
        yield
    finally:
        ingestor.stop()


app = FastAPI(
    title="AI-Queryable Health Data System",
    description="A synthetic live heart-rate monitor with read-only history queries.",
    version="2.0.0",
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
        raise HTTPException(status_code=503, detail="Heart-rate history is not loaded")
    return service


def _store(request: Request) -> HeartRateStore:
    store = getattr(request.app.state, "heart_rate_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Heart-rate storage is not ready")
    return store


@app.get("/health")
def health(request: Request) -> dict:
    ingestor = getattr(request.app.state, "mqtt_ingestor", None)
    if ingestor is None or not ingestor.connected.is_set():
        raise HTTPException(status_code=503, detail="Heart-rate stream is not connected")
    return {"status": "ok"}


@app.get("/api/catalog")
def catalog(request: Request):
    return {"tables": _service(request).catalog()}


@app.get("/monitor", include_in_schema=False)
def monitor_page():
    return FileResponse(STATIC_DIR / "monitor.html")


@app.get("/api/monitor/users")
def monitor_users(request: Request):
    return {"users": _store(request).users()}


@app.get("/api/monitor")
def monitor_snapshot(
    request: Request,
    user_id: str | None = Query(default=None, pattern=r"^user-[0-9]{4}$"),
):
    try:
        store = _store(request)
        return store.snapshot(user_id or store.default_user_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.put("/api/monitor/settings")
def update_monitor_settings(body: AlarmSettings, request: Request):
    try:
        return _store(request).update_settings(
            body.user_id,
            body.low_bpm,
            body.high_bpm,
            body.alert_after_samples,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/monitor/stream")
async def monitor_stream(
    request: Request,
    user_id: str | None = Query(default=None, pattern=r"^user-[0-9]{4}$"),
):
    store = _store(request)
    user_id = user_id or store.default_user_id
    try:
        store.settings(user_id)
    except FileNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error

    async def updates():
        last_event_id = None
        while True:
            if await request.is_disconnected():
                break
            snapshot = store.snapshot(user_id)
            latest = snapshot["latest"]
            if latest and latest["event_id"] != last_event_id:
                last_event_id = latest["event_id"]
                yield f"data: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        updates(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
    if table_name.lower() not in METRICS:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table_name}'")
    try:
        result = _service(request).preview(table_name, limit)
        return {
            "table": table_name,
            **result,
        }
    except ValueError as error:
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


@app.post("/api/nl-query")
def execute_natural_language_query(body: NaturalLanguageQuery, request: Request):
    query_service = _service(request)
    catalog = query_service.catalog()
    if not catalog:
        raise HTTPException(status_code=503, detail="No queryable tables are loaded")
    reference_date_string = _store(request).reference_date()
    reference_date = (
        date.fromisoformat(reference_date_string)
        if reference_date_string
        else datetime.now(timezone.utc).date()
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
    metrics: str = "heart_rate",
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
