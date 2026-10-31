import json
import os
import re
from datetime import date, datetime, timezone
from io import BytesIO
from typing import Dict, List

import pyarrow as pa
import pyarrow.parquet as pq

from app.analytics import build_weekly_features
from app.storage import object_keys, read_json_object, s3_client, write_json_object

RAW_COMPLETION_KEY = "_SUCCESS.json"
CURATED_COMPLETION_KEY = "_SUCCESS.json"

METRIC_SCHEMAS = {
    "heart_rate": pa.schema(
        [
            ("event_id", pa.string()),
            ("user_id", pa.string()),
            ("observed_at", pa.timestamp("us", tz="UTC")),
            ("bpm", pa.int16()),
            ("activity", pa.string()),
        ]
    ),
    "sleep": pa.schema(
        [
            ("event_id", pa.string()),
            ("user_id", pa.string()),
            ("night_date", pa.date32()),
            ("start_at", pa.timestamp("us", tz="UTC")),
            ("end_at", pa.timestamp("us", tz="UTC")),
            ("stage", pa.string()),
            ("duration_minutes", pa.int16()),
        ]
    ),
    "steps": pa.schema(
        [
            ("event_id", pa.string()),
            ("user_id", pa.string()),
            ("bucket_start", pa.timestamp("us", tz="UTC")),
            ("bucket_end", pa.timestamp("us", tz="UTC")),
            ("steps", pa.int32()),
        ]
    ),
    "workouts": pa.schema(
        [
            ("event_id", pa.string()),
            ("user_id", pa.string()),
            ("start_at", pa.timestamp("us", tz="UTC")),
            ("end_at", pa.timestamp("us", tz="UTC")),
            ("activity_type", pa.string()),
            ("intensity", pa.string()),
            ("avg_heart_rate", pa.int16()),
        ]
    ),
    "weight": pa.schema(
        [
            ("event_id", pa.string()),
            ("user_id", pa.string()),
            ("measured_at", pa.timestamp("us", tz="UTC")),
            ("weight_kg", pa.float64()),
        ]
    ),
}
RECORD_FIELDS = {
    "heart_rate": {"event_id", "user_id", "observed_at", "bpm", "activity"},
    "sleep": {
        "event_id",
        "user_id",
        "night_date",
        "start_at",
        "end_at",
        "stage",
        "duration_minutes",
    },
    "steps": {
        "event_id",
        "user_id",
        "bucket_start",
        "bucket_end",
        "steps",
    },
    "workouts": {
        "event_id",
        "user_id",
        "start_at",
        "end_at",
        "activity_type",
        "intensity",
        "avg_heart_rate",
    },
    "weight": {"event_id", "user_id", "measured_at", "weight_kg"},
}

USER_ID = re.compile(r"user-[0-9]{4}\Z")
STAGES = {"awake", "light", "deep", "rem"}
ACTIVITIES = {"rest", "active", "workout"}
WORKOUT_TYPES = {"run", "cycling", "strength", "walking"}
INTENSITIES = {"moderate", "vigorous"}


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def validate_record(metric: str, record: object, partition_date: date) -> Dict:
    if metric not in METRIC_SCHEMAS:
        raise ValueError(f"Unknown metric dataset: {metric}")
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    missing = RECORD_FIELDS[metric] - record.keys()
    extra = record.keys() - RECORD_FIELDS[metric]
    if missing or extra:
        raise ValueError(
            f"record fields mismatch (missing={sorted(missing)}, extra={sorted(extra)})"
        )
    event_id = record.get("event_id")
    user_id = record.get("user_id")
    if not isinstance(event_id, str) or not event_id:
        raise ValueError("event_id must be a non-empty string")
    if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
        raise ValueError("user_id must match user-NNNN")

    normalized = dict(record)
    normalized["event_id"] = event_id
    normalized["user_id"] = user_id

    if metric == "heart_rate":
        observed_at = _timestamp(record.get("observed_at"), "observed_at")
        if observed_at.date() != partition_date:
            raise ValueError("observed_at does not match the event-date partition")
        if record.get("activity") not in ACTIVITIES:
            raise ValueError("activity is not a supported value")
        normalized.update(
            observed_at=observed_at,
            bpm=_integer(record.get("bpm"), "bpm", 35, 220),
        )
    elif metric == "sleep":
        night_date = date.fromisoformat(record.get("night_date", ""))
        start_at = _timestamp(record.get("start_at"), "start_at")
        end_at = _timestamp(record.get("end_at"), "end_at")
        if night_date != partition_date:
            raise ValueError("night_date does not match the event-date partition")
        if end_at <= start_at or (end_at - start_at).total_seconds() > 14 * 3600:
            raise ValueError("sleep stage interval is invalid")
        if record.get("stage") not in STAGES:
            raise ValueError("stage is not a supported value")
        if (end_at - start_at).total_seconds() != record.get("duration_minutes", 0) * 60:
            raise ValueError("duration_minutes must match the stage interval")
        normalized.update(
            night_date=night_date,
            start_at=start_at,
            end_at=end_at,
            duration_minutes=_integer(
                record.get("duration_minutes"), "duration_minutes", 1, 900
            ),
        )
    elif metric == "steps":
        bucket_start = _timestamp(record.get("bucket_start"), "bucket_start")
        bucket_end = _timestamp(record.get("bucket_end"), "bucket_end")
        if bucket_start.date() != partition_date:
            raise ValueError("bucket_start does not match the event-date partition")
        if (bucket_end - bucket_start).total_seconds() != 15 * 60:
            raise ValueError("step buckets must cover exactly 15 minutes")
        normalized.update(
            bucket_start=bucket_start,
            bucket_end=bucket_end,
            steps=_integer(record.get("steps"), "steps", 0, 100000),
        )
    elif metric == "workouts":
        start_at = _timestamp(record.get("start_at"), "start_at")
        end_at = _timestamp(record.get("end_at"), "end_at")
        if start_at.date() != partition_date:
            raise ValueError("start_at does not match the event-date partition")
        if end_at <= start_at or (end_at - start_at).total_seconds() > 8 * 3600:
            raise ValueError("workout interval is invalid")
        if record.get("activity_type") not in WORKOUT_TYPES:
            raise ValueError("activity_type is not a supported value")
        if record.get("intensity") not in INTENSITIES:
            raise ValueError("intensity is not a supported value")
        normalized.update(
            start_at=start_at,
            end_at=end_at,
            avg_heart_rate=_integer(
                record.get("avg_heart_rate"), "avg_heart_rate", 35, 220
            ),
        )
    elif metric == "weight":
        measured_at = _timestamp(record.get("measured_at"), "measured_at")
        value = record.get("weight_kg")
        if measured_at.date() != partition_date:
            raise ValueError("measured_at does not match the event-date partition")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("weight_kg must be numeric")
        if not 25 <= value <= 300:
            raise ValueError("weight_kg must be between 25 and 300 kg")
        normalized.update(measured_at=measured_at, weight_kg=float(value))

    return normalized


def _partition_date(key: str) -> date:
    match = re.search(r"/event_date=([0-9]{4}-[0-9]{2}-[0-9]{2})/", key)
    if not match:
        raise ValueError(f"Object key has no valid event_date partition: {key}")
    return date.fromisoformat(match.group(1))


def transform_object(client, raw_bucket: str, curated_bucket: str, key: str) -> int:
    parts = key.split("/")
    if len(parts) != 4 or parts[0] != "raw" or not parts[1] in METRIC_SCHEMAS:
        raise ValueError(f"Unexpected raw object key: {key}")
    metric = parts[1]
    partition_date = _partition_date(key)
    response = client.get_object(Bucket=raw_bucket, Key=key)
    body = response["Body"].read().decode("utf-8")
    records: List[Dict] = []
    event_ids = set()
    for line_number, line in enumerate(body.splitlines(), start=1):
        try:
            record = validate_record(metric, json.loads(line), partition_date)
            if record["event_id"] in event_ids:
                raise ValueError(f"duplicate event_id: {record['event_id']}")
            event_ids.add(record["event_id"])
            records.append(record)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid record at {key}:{line_number}: {error}") from error
    if not records:
        raise ValueError(f"Raw object contains no records: {key}")

    table = pa.Table.from_pylist(records, schema=METRIC_SCHEMAS[metric])
    output = BytesIO()
    pq.write_table(table, output, compression="zstd")
    output_key = (
        f"curated/{metric}/event_date={partition_date.isoformat()}/part-00000.parquet"
    )
    client.put_object(
        Bucket=curated_bucket,
        Key=output_key,
        Body=output.getvalue(),
        ContentType="application/vnd.apache.parquet",
    )
    return len(records)


def run() -> None:
    client = s3_client()
    raw_bucket = os.environ.get("RAW_BUCKET", "health-raw")
    curated_bucket = os.environ.get("CURATED_BUCKET", "health-curated")
    computed_bucket = os.environ.get("COMPUTED_BUCKET", "health-computed")
    raw_manifest = read_json_object(client, raw_bucket, RAW_COMPLETION_KEY)
    if raw_manifest is None:
        raise RuntimeError(
            f"Raw dataset is incomplete: {raw_bucket}/{RAW_COMPLETION_KEY} is missing"
        )
    existing_manifest = read_json_object(
        client, curated_bucket, CURATED_COMPLETION_KEY
    )
    if existing_manifest is not None:
        if existing_manifest.get("source") != raw_manifest:
            raise RuntimeError(
                "Existing curated data does not match the raw dataset. "
                "Reset the MinIO data volume to regenerate the dataset."
            )
        print("Curated dataset is already complete; reusing existing objects.", flush=True)
    else:
        keys = sorted(object_keys(client, raw_bucket, "raw/"))
        if not keys:
            raise RuntimeError(f"No raw data objects found in bucket {raw_bucket}")
        metrics_seen = set()
        total = 0
        for key in keys:
            count = transform_object(client, raw_bucket, curated_bucket, key)
            metrics_seen.add(key.split("/")[1])
            total += count
            print(f"Curated {count} records from {key}", flush=True)
        missing = set(METRIC_SCHEMAS) - metrics_seen
        if missing:
            raise RuntimeError(f"Raw input is missing metric datasets: {sorted(missing)}")
        existing_manifest = {
            "source": raw_manifest,
            "raw_objects": len(keys),
            "records": total,
        }
        write_json_object(
            client,
            curated_bucket,
            CURATED_COMPLETION_KEY,
            existing_manifest,
        )
        print(f"ETL completed: {total} records across {len(keys)} objects", flush=True)

    feature_rows = build_weekly_features(client, curated_bucket, computed_bucket)
    print(f"Computed {feature_rows} user-week feature rows", flush=True)


if __name__ == "__main__":
    run()
