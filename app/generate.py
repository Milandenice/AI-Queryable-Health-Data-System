import json
import math
import os
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterator, List, Tuple

from app.storage import read_json_object, s3_client, write_json_object

START_DATE = date(2025, 1, 1)
DAY_COUNT = 30
USER_COUNT = 100
METRICS = ("heart_rate", "sleep", "steps", "workouts", "weight")
RAW_COMPLETION_KEY = "_SUCCESS.json"


@dataclass
class UserProfile:
    user_id: str
    resting_bpm: float
    activity_factor: float
    weight_kg: float
    weight_trend: float
    weigh_interval: int
    next_weigh_day: int


def make_profiles(rng: random.Random, user_count: int) -> List[UserProfile]:
    profiles = []
    for index in range(user_count):
        activity_factor = rng.uniform(0.65, 1.5)
        if activity_factor > 1.15:
            weight_direction = rng.choice((-1, -1, 0))
        elif activity_factor < 0.85:
            weight_direction = rng.choice((0, 1, 1))
        else:
            weight_direction = rng.choice((-1, 0, 1))
        profiles.append(
            UserProfile(
                user_id=f"user-{index + 1:04d}",
                resting_bpm=rng.uniform(55, 78),
                activity_factor=activity_factor,
                weight_kg=rng.uniform(58, 112),
                weight_trend=weight_direction * rng.uniform(0.006, 0.02),
                weigh_interval=rng.randint(1, 7),
                next_weigh_day=0,
            )
        )
    return profiles


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


def _event_id(user_id: str, metric: str, timestamp: datetime, suffix: str = "") -> str:
    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return f"{user_id}-{metric}-{stamp}{suffix}"


def _sleep_events(profile: UserProfile, night_date: date, rng: random.Random) -> List[Dict]:
    start = datetime.combine(
        night_date - timedelta(days=1),
        time(rng.randint(21, 23), rng.choice((0, 15, 30, 45))),
        tzinfo=timezone.utc,
    )
    remaining = rng.randint(390, 510)
    cycle = 0
    events = []
    while remaining > 0:
        pattern = [
            ("light", rng.randint(25, 45)),
            ("deep", rng.randint(20, 45) if cycle < 2 else rng.randint(5, 20)),
            ("light", rng.randint(15, 30)),
            ("rem", rng.randint(15, 35) if cycle < 2 else rng.randint(25, 45)),
        ]
        if cycle > 0 and rng.random() < 0.25:
            pattern.insert(0, ("awake", rng.randint(2, 8)))
        for stage, requested in pattern:
            duration = min(requested, remaining)
            if duration <= 0:
                break
            end = start + timedelta(minutes=duration)
            events.append(
                {
                    "event_id": _event_id(
                        profile.user_id, "sleep", start, f"-{len(events):02d}"
                    ),
                    "user_id": profile.user_id,
                    "night_date": night_date.isoformat(),
                    "start_at": start.isoformat(),
                    "end_at": end.isoformat(),
                    "stage": stage,
                    "duration_minutes": duration,
                }
            )
            start = end
            remaining -= duration
            if remaining == 0:
                break
        cycle += 1
    return events


def _generate_day(
    day: date, day_index: int, profiles: List[UserProfile], rng: random.Random
) -> Dict[str, List[Dict]]:
    output = {metric: [] for metric in METRICS}
    for profile in profiles:
        workouts: List[Tuple[datetime, datetime, str, str]] = []
        if rng.random() < 0.12:
            start = _at(day, rng.randint(7, 20), rng.choice((0, 15, 30, 45)))
            duration = rng.randint(25, 75)
            workout_end = start + timedelta(minutes=duration)
            kind = rng.choice(("run", "cycling", "strength", "walking"))
            intensity = rng.choice(("moderate", "vigorous"))
            workouts.append((start, workout_end, kind, intensity))
            output["workouts"].append(
                {
                    "event_id": _event_id(profile.user_id, "workout", start),
                    "user_id": profile.user_id,
                    "start_at": start.isoformat(),
                    "end_at": workout_end.isoformat(),
                    "activity_type": kind,
                    "intensity": intensity,
                    "avg_heart_rate": int(
                        profile.resting_bpm + rng.uniform(45, 85)
                    ),
                }
            )

        nonwear = None
        if rng.random() < 0.16:
            gap_start = _at(day, rng.randint(10, 17), rng.choice((0, 15, 30, 45)))
            nonwear = (gap_start, gap_start + timedelta(minutes=rng.randint(45, 180)))

        for minute_of_day in range(0, 24 * 60, 5):
            observed = _at(day, minute_of_day // 60, minute_of_day % 60)
            if nonwear and nonwear[0] <= observed < nonwear[1]:
                continue
            active_workout = next(
                (workout for workout in workouts if workout[0] <= observed < workout[1]),
                None,
            )
            if active_workout:
                continue
            circadian = -5 * math.cos((minute_of_day - 180) * 2 * math.pi / 1440)
            bpm = profile.resting_bpm + circadian + rng.gauss(0, 3.2)
            activity = "rest" if minute_of_day < 7 * 60 or minute_of_day >= 22 * 60 else "active"
            for workout_start, workout_end, _, _ in workouts:
                recovery_end = workout_end + timedelta(minutes=25)
                if workout_end <= observed < recovery_end:
                    recovery = (recovery_end - observed).total_seconds() / 60
                    bpm += 18 * recovery / 25
            output["heart_rate"].append(
                {
                    "event_id": _event_id(profile.user_id, "heart_rate", observed),
                    "user_id": profile.user_id,
                    "observed_at": observed.isoformat(),
                    "bpm": max(40, min(200, int(round(bpm)))),
                    "activity": activity,
                }
            )

        for workout_start, workout_end, _, intensity in workouts:
            observed = workout_start
            while observed < workout_end:
                if not nonwear or not nonwear[0] <= observed < nonwear[1]:
                    progress = (observed - workout_start).total_seconds() / max(
                        1, (workout_end - workout_start).total_seconds()
                    )
                    bpm = profile.resting_bpm + 45 + 15 * math.sin(progress * math.pi)
                    bpm += rng.gauss(0, 5)
                    output["heart_rate"].append(
                        {
                            "event_id": _event_id(profile.user_id, "heart_rate", observed),
                            "user_id": profile.user_id,
                            "observed_at": observed.isoformat(),
                            "bpm": max(75, min(200, int(round(bpm)))),
                            "activity": "workout",
                        }
                    )
                observed += timedelta(minutes=1)

        for bucket_minute in range(7 * 60, 23 * 60, 15):
            bucket_start = _at(day, bucket_minute // 60, bucket_minute % 60)
            if nonwear and nonwear[0] <= bucket_start < nonwear[1]:
                continue
            hour = bucket_minute / 60
            day_curve = max(0.25, math.sin((hour - 6) * math.pi / 17))
            expected_steps = 240 * profile.activity_factor * day_curve
            steps = max(0, int(rng.gauss(expected_steps, expected_steps * 0.65)))
            for workout_start, workout_end, kind, _ in workouts:
                if workout_start < bucket_start + timedelta(minutes=15) and workout_end > bucket_start:
                    steps += 450 if kind in ("run", "walking") else 180
            output["steps"].append(
                {
                    "event_id": _event_id(profile.user_id, "steps", bucket_start),
                    "user_id": profile.user_id,
                    "bucket_start": bucket_start.isoformat(),
                    "bucket_end": (bucket_start + timedelta(minutes=15)).isoformat(),
                    "steps": steps,
                }
            )

        output["sleep"].extend(_sleep_events(profile, day, rng))

        if day_index >= profile.next_weigh_day:
            measured_at = _at(day, rng.randint(6, 9), rng.choice((0, 15, 30, 45)))
            noise = rng.gauss(0, 0.12)
            weight = profile.weight_kg + profile.weight_trend * day_index + noise
            output["weight"].append(
                {
                    "event_id": _event_id(profile.user_id, "weight", measured_at),
                    "user_id": profile.user_id,
                    "measured_at": measured_at.isoformat(),
                    "weight_kg": round(weight, 2),
                }
            )
            profile.next_weigh_day = day_index + profile.weigh_interval
            profile.weigh_interval = rng.randint(1, 7)
    return output


def iter_batches(
    seed: int = 2025,
    user_count: int = USER_COUNT,
    day_count: int = DAY_COUNT,
    start_date: date = START_DATE,
) -> Iterator[Tuple[date, Dict[str, List[Dict]]]]:
    rng = random.Random(seed)
    profiles = make_profiles(rng, user_count)
    for day_index in range(day_count):
        current_day = start_date + timedelta(days=day_index)
        yield current_day, _generate_day(current_day, day_index, profiles, rng)


def run() -> None:
    client = s3_client()
    bucket = os.environ.get("S3_BUCKET", "health-raw")
    seed = int(os.environ.get("DATA_SEED", "2025"))
    manifest = {
        "version": 1,
        "seed": seed,
        "users": USER_COUNT,
        "days": DAY_COUNT,
        "start_date": START_DATE.isoformat(),
    }
    existing_manifest = read_json_object(client, bucket, RAW_COMPLETION_KEY)
    if existing_manifest is not None:
        if existing_manifest != manifest:
            raise RuntimeError(
                "Existing raw data was generated with a different configuration. "
                "Run 'docker compose down -v' to reset the dataset before changing it."
            )
        print("Raw dataset is already complete; reusing existing objects.", flush=True)
        return

    for day, batch in iter_batches(seed=seed):
        for metric, records in batch.items():
            if not records:
                continue
            key = f"raw/{metric}/event_date={day.isoformat()}/part-00000.jsonl"
            body = "".join(
                json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
                for record in records
            ).encode("utf-8")
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/x-ndjson",
            )
        print(f"Generated and uploaded data for {day.isoformat()}", flush=True)
    write_json_object(client, bucket, RAW_COMPLETION_KEY, manifest)
    print("Raw dataset generation completed.", flush=True)


if __name__ == "__main__":
    run()
