from collections import defaultdict
from datetime import date, timedelta
from io import BytesIO
from typing import Dict, List, Tuple

import pyarrow as pa
import pyarrow.parquet as pq

from app.storage import object_keys, read_json_object, write_json_object

WEEKLY_FEATURES_KEY = "weekly_features"
COMPUTED_COMPLETION_KEY = "_SUCCESS.json"
WEEKLY_FEATURE_SCHEMA = pa.schema(
    [
        ("user_id", pa.string()),
        ("week_start", pa.date32()),
        ("avg_resting_bpm", pa.float64()),
        ("avg_daily_steps", pa.float64()),
        ("avg_sleep_hours", pa.float64()),
        ("workout_count", pa.int16()),
        ("workout_minutes", pa.float64()),
        ("weight_kg", pa.float64()),
        ("weight_change_kg", pa.float64()),
        ("weight_change_days", pa.float64()),
        ("weight_change_kg_per_day", pa.float64()),
    ]
)


def _week_start(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _feature_bucket(features: Dict, user_id: str, week_start: date) -> Dict:
    key = (user_id, week_start)
    if key not in features:
        features[key] = {
            "user_id": user_id,
            "week_start": week_start,
            "resting_hr_total": 0,
            "resting_hr_count": 0,
            "steps_total": 0,
            "step_days": set(),
            "sleep_minutes": 0,
            "sleep_nights": set(),
            "workout_count": 0,
            "workout_seconds": 0,
        }
    return features[key]


def build_weekly_features(client, curated_bucket: str, computed_bucket: str) -> int:
    curated_manifest = read_json_object(
        client, curated_bucket, COMPUTED_COMPLETION_KEY
    )
    if curated_manifest is None:
        raise RuntimeError(
            f"Curated data is incomplete: {curated_bucket}/_SUCCESS.json is missing"
        )

    completion = read_json_object(client, computed_bucket, COMPUTED_COMPLETION_KEY)
    expected_completion = {"source": curated_manifest, "feature_version": 1}
    if (
        isinstance(completion, dict)
        and completion.get("source") == curated_manifest
        and completion.get("feature_version") == 1
    ):
        print("Computed dataset is already complete; reusing existing objects.", flush=True)
        return int(completion.get("feature_rows", 0))

    features: Dict[Tuple[str, date], Dict] = {}
    weights = defaultdict(list)
    parquet_keys = [
        key
        for key in object_keys(client, curated_bucket, "curated/")
        if key.endswith(".parquet")
    ]
    if not parquet_keys:
        raise RuntimeError(f"No curated Parquet objects found in {curated_bucket}")

    for key in parquet_keys:
        metric = key.split("/")[1]
        response = client.get_object(Bucket=curated_bucket, Key=key)
        table = pq.read_table(BytesIO(response["Body"].read()))
        for record in table.to_pylist():
            user_id = record["user_id"]
            if metric == "heart_rate" and record["activity"] == "rest":
                observed = record["observed_at"]
                bucket = _feature_bucket(features, user_id, _week_start(observed.date()))
                bucket["resting_hr_total"] += record["bpm"]
                bucket["resting_hr_count"] += 1
            elif metric == "steps":
                observed = record["bucket_start"]
                bucket = _feature_bucket(features, user_id, _week_start(observed.date()))
                bucket["steps_total"] += record["steps"]
                bucket["step_days"].add(observed.date())
            elif metric == "sleep" and record["stage"] != "awake":
                night = record["night_date"]
                bucket = _feature_bucket(features, user_id, _week_start(night))
                bucket["sleep_minutes"] += record["duration_minutes"]
                bucket["sleep_nights"].add(night)
            elif metric == "workouts":
                start = record["start_at"]
                bucket = _feature_bucket(features, user_id, _week_start(start.date()))
                bucket["workout_count"] += 1
                bucket["workout_seconds"] += (
                    record["end_at"] - start
                ).total_seconds()
            elif metric == "weight":
                measured = record["measured_at"]
                weights[user_id].append(
                    (measured, record["weight_kg"], _week_start(measured.date()))
                )

    for user_id, measurements in weights.items():
        measurements.sort(key=lambda item: item[0])
        previous = None
        for measured_at, weight_kg, week_start in measurements:
            bucket = _feature_bucket(features, user_id, week_start)
            elapsed_days = (
                (measured_at - previous[0]).total_seconds() / 86400
                if previous is not None
                else None
            )
            bucket["weight_kg"] = weight_kg
            if elapsed_days is not None and elapsed_days > 0:
                change = weight_kg - previous[1]
                bucket["weight_change_kg"] = change
                bucket["weight_change_days"] = elapsed_days
                bucket["weight_change_kg_per_day"] = change / elapsed_days
            previous = (measured_at, weight_kg)

    by_week = defaultdict(list)
    for (user_id, week_start), values in sorted(features.items()):
        by_week[week_start].append(
            {
                "user_id": user_id,
                "week_start": week_start,
                "avg_resting_bpm": (
                    values["resting_hr_total"] / values["resting_hr_count"]
                    if values["resting_hr_count"]
                    else None
                ),
                "avg_daily_steps": (
                    values["steps_total"] / len(values["step_days"])
                    if values["step_days"]
                    else None
                ),
                "avg_sleep_hours": (
                    values["sleep_minutes"] / len(values["sleep_nights"]) / 60
                    if values["sleep_nights"]
                    else None
                ),
                "workout_count": values["workout_count"],
                "workout_minutes": values["workout_seconds"] / 60,
                "weight_kg": values.get("weight_kg"),
                "weight_change_kg": values.get("weight_change_kg"),
                "weight_change_days": values.get("weight_change_days"),
                "weight_change_kg_per_day": values.get("weight_change_kg_per_day"),
            }
        )

    feature_rows = 0
    for week_start, rows in sorted(by_week.items()):
        output = BytesIO()
        pq.write_table(
            pa.Table.from_pylist(rows, schema=WEEKLY_FEATURE_SCHEMA),
            output,
            compression="zstd",
        )
        key = (
            f"{WEEKLY_FEATURES_KEY}/event_week={week_start.isoformat()}/"
            "part-00000.parquet"
        )
        client.put_object(
            Bucket=computed_bucket,
            Key=key,
            Body=output.getvalue(),
            ContentType="application/vnd.apache.parquet",
        )
        feature_rows += len(rows)

    if not feature_rows:
        raise RuntimeError("No weekly features could be computed from curated data")
    write_json_object(
        client,
        computed_bucket,
        COMPUTED_COMPLETION_KEY,
        {
            **expected_completion,
            "feature_rows": feature_rows,
            "weeks": len(by_week),
            "curated_objects": len(parquet_keys),
        },
    )
    return feature_rows
