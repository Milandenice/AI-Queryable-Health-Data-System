from collections import defaultdict
from datetime import timedelta
import math
from typing import Dict, List, Optional, Sequence, Tuple

from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import GroupKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODEL_FEATURES = (
    "avg_daily_steps",
    "avg_sleep_hours",
    "workout_minutes",
    "avg_resting_bpm",
)
FEATURE_LABELS = {
    "avg_daily_steps": "steps",
    "avg_sleep_hours": "sleep duration",
    "workout_minutes": "workout minutes",
    "avg_resting_bpm": "resting heart rate",
}
TARGET = "weight_change_kg_per_day"
MIN_MODEL_SAMPLES = 10
MIN_MODEL_USERS = 3


def _finite(value) -> bool:
    return value is not None and math.isfinite(float(value))


def _weekly_pairs(rows: Sequence[Dict]) -> List[Tuple[Dict, Dict]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["user_id"]].append(row)

    pairs = []
    for user_rows in grouped.values():
        user_rows.sort(key=lambda row: row["week_start"])
        for previous, following in zip(user_rows, user_rows[1:]):
            if following["week_start"] - previous["week_start"] == timedelta(days=7):
                pairs.append((previous, following))
    return pairs


def _complete_samples(rows: Sequence[Dict]):
    samples = []
    for previous, following in _weekly_pairs(rows):
        target = following.get(TARGET)
        values = [previous.get(feature) for feature in MODEL_FEATURES]
        if _finite(target) and all(_finite(value) for value in values):
            samples.append(
                (values, float(target), previous["user_id"], previous["week_start"])
            )
    return samples


def _correlation(left: Sequence[float], right: Sequence[float]) -> Optional[float]:
    if len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    left_sum = sum(value * value for value in left_delta)
    right_sum = sum(value * value for value in right_delta)
    denominator = math.sqrt(left_sum * right_sum)
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(left_delta, right_delta)) / denominator


def associations(rows: Sequence[Dict]) -> List[Dict]:
    samples = _complete_samples(rows)
    results = []
    for index, feature in enumerate(MODEL_FEATURES):
        values = [sample[0][index] for sample in samples]
        targets = [sample[1] for sample in samples]
        results.append(
            {
                "feature": feature,
                "label": FEATURE_LABELS[feature],
                "correlation": _correlation(values, targets),
                "sample_count": len(values),
            }
        )
    return results


def personal_analysis(rows: Sequence[Dict], user_id: str) -> Dict:
    user_rows = [row for row in rows if row["user_id"] == user_id]
    values = associations(user_rows)
    usable = [
        item
        for item in values
        if item["sample_count"] >= 3 and item["correlation"] is not None
    ]
    if not usable:
        guidance = (
            "There are not enough consecutive, complete weeks to identify a "
            "personal pattern. Keep tracking before drawing conclusions."
        )
    else:
        strongest = max(usable, key=lambda item: abs(item["correlation"]))
        direction = (
            "higher" if strongest["correlation"] > 0 else "lower"
        )
        guidance = (
            f"In this synthetic profile, higher {strongest['label']} was associated "
            f"with a {direction} next-week weight-change rate "
            f"(r={strongest['correlation']:.2f}, n={strongest['sample_count']} "
            "weekly pairs). This is exploratory, not evidence of cause; keep "
            "tracking rather than changing health behavior based on this pattern."
        )
    return {
        "associations": values,
        "guidance": guidance,
        "paired_weeks": max((item["sample_count"] for item in values), default=0),
    }


def _new_model():
    return make_pipeline(StandardScaler(), Ridge(alpha=1.0))


def _model_readiness(samples) -> Optional[str]:
    users = {sample[2] for sample in samples}
    if len(samples) < MIN_MODEL_SAMPLES:
        return f"At least {MIN_MODEL_SAMPLES} complete weekly examples are needed."
    if len(users) < MIN_MODEL_USERS:
        return f"At least {MIN_MODEL_USERS} distinct profiles are needed."
    return None


def evaluate_model(rows: Sequence[Dict]) -> Dict:
    samples = _complete_samples(rows)
    reason = _model_readiness(samples)
    if reason:
        return {"status": "insufficient_data", "reason": reason}

    features = [sample[0] for sample in samples]
    targets = [sample[1] for sample in samples]
    groups = [sample[2] for sample in samples]
    split_count = min(5, len(set(groups)))
    predictions = cross_val_predict(
        _new_model(),
        features,
        targets,
        groups=groups,
        cv=GroupKFold(n_splits=split_count),
    )
    return {
        "status": "ready",
        "algorithm": "Ridge regression",
        "target": "next-week weight-change rate (kg/day)",
        "validation": "grouped cross-validation; each profile is held out from its fold",
        "sample_count": len(samples),
        "profile_count": len(set(groups)),
        "mae_kg_per_day": float(mean_absolute_error(targets, predictions)),
        "r2": float(r2_score(targets, predictions)),
    }


def predict_user(rows: Sequence[Dict], user_id: str) -> Dict:
    user_rows = sorted(
        (row for row in rows if row["user_id"] == user_id),
        key=lambda row: row["week_start"],
    )
    if not user_rows:
        return {"status": "insufficient_data", "reason": "No weekly history is available."}

    latest = user_rows[-1]
    values = [latest.get(feature) for feature in MODEL_FEATURES]
    if not all(_finite(value) for value in values):
        return {
            "status": "insufficient_data",
            "reason": "The latest week is missing one or more model features.",
        }

    samples = [
        sample for sample in _complete_samples(rows) if sample[2] != user_id
    ]
    reason = _model_readiness(samples)
    if reason:
        return {"status": "insufficient_data", "reason": reason}

    model = _new_model()
    model.fit([sample[0] for sample in samples], [sample[1] for sample in samples])
    predicted_rate = float(model.predict([values])[0])
    return {
        "status": "ready",
        "algorithm": "Ridge regression",
        "target": "next-week weight-change rate (kg/day)",
        "source_week": latest["week_start"].isoformat(),
        "predicted_kg_per_day": predicted_rate,
        "predicted_7_day_change_kg": predicted_rate * 7,
        "training_sample_count": len(samples),
        "training_profile_count": len({sample[2] for sample in samples}),
        "excluded_profile": user_id,
    }
