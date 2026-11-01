import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

USER_ID = re.compile(r"user-[0-9]{4}\Z")
ACTIVITIES = {"rest", "active", "workout"}
DEFAULT_USER_ID = "user-0001"
DEFAULT_LOW_BPM = 50
DEFAULT_HIGH_BPM = 120
DEFAULT_ALERT_AFTER_SAMPLES = 15
RECENT_WINDOW_SECONDS = 300
BASELINE_WINDOW_SECONDS = 7 * 24 * 60 * 60


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return parsed.astimezone(timezone.utc)


def _connect(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5
        )
    else:
        connection = sqlite3.connect(path, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _consecutive_out_of_range(rows, direction, low_bpm, high_bpm) -> int:
    count = 0
    for row in rows:
        bpm = row["bpm"]
        if (direction == "high" and bpm > high_bpm) or (
            direction == "low" and bpm < low_bpm
        ):
            count += 1
        else:
            break
    return count


class HeartRateStore:
    def __init__(
        self,
        database_path: str,
        low_bpm: int = DEFAULT_LOW_BPM,
        high_bpm: int = DEFAULT_HIGH_BPM,
        alert_after_samples: int = DEFAULT_ALERT_AFTER_SAMPLES,
        default_user_id: str = DEFAULT_USER_ID,
    ):
        self.database_path = Path(database_path).resolve()
        self.low_bpm = low_bpm
        self.high_bpm = high_bpm
        self.alert_after_samples = alert_after_samples
        self.default_user_id = default_user_id
        self.validate_user_id(default_user_id)
        if not 35 <= self.low_bpm <= 219 or not 36 <= self.high_bpm <= 220:
            raise ValueError("BPM thresholds must be between 35 and 220")
        if not self.low_bpm < self.high_bpm:
            raise ValueError("low_bpm must be lower than high_bpm")
        if not 1 <= self.alert_after_samples <= 300:
            raise ValueError("alert_after_samples must be between 1 and 300")

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.database_path) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS heart_rate (
                    event_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    observed_at_ms INTEGER NOT NULL,
                    event_date TEXT NOT NULL,
                    bpm INTEGER NOT NULL CHECK (bpm BETWEEN 35 AND 220),
                    activity TEXT NOT NULL
                        CHECK (activity IN ('rest', 'active', 'workout'))
                );
                CREATE INDEX IF NOT EXISTS heart_rate_user_time
                    ON heart_rate(user_id, observed_at_ms);
                CREATE TABLE IF NOT EXISTS resting_hr_minute (
                    user_id TEXT NOT NULL,
                    minute_ms INTEGER NOT NULL,
                    bpm_total INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    PRIMARY KEY (user_id, minute_ms)
                );
                CREATE INDEX IF NOT EXISTS resting_hr_minute_user_time
                    ON resting_hr_minute(user_id, minute_ms);
                CREATE TABLE IF NOT EXISTS alarm_settings (
                    user_id TEXT PRIMARY KEY,
                    low_bpm INTEGER NOT NULL CHECK (low_bpm BETWEEN 35 AND 219),
                    high_bpm INTEGER NOT NULL CHECK (high_bpm BETWEEN 36 AND 220),
                    alert_after_samples INTEGER NOT NULL
                        CHECK (alert_after_samples BETWEEN 1 AND 300),
                    CHECK (low_bpm < high_bpm)
                );
                CREATE TABLE IF NOT EXISTS alarm_state (
                    user_id TEXT PRIMARY KEY,
                    direction TEXT,
                    outside_since_ms INTEGER,
                    last_observed_ms INTEGER,
                    consecutive_samples INTEGER NOT NULL DEFAULT 0
                        CHECK (consecutive_samples >= 0),
                    triggered INTEGER NOT NULL DEFAULT 0
                        CHECK (triggered IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS alarm_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    direction TEXT NOT NULL CHECK (direction IN ('low', 'high')),
                    event_type TEXT NOT NULL
                        CHECK (event_type IN ('triggered', 'resolved')),
                    observed_at TEXT NOT NULL,
                    bpm INTEGER NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alarm_events_user_time
                    ON alarm_events(user_id, id DESC);
                """
            )
            settings_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(alarm_settings)")
            }
            if "alert_after_samples" not in settings_columns:
                connection.execute("ALTER TABLE alarm_settings RENAME TO alarm_settings_legacy")
                connection.execute(
                    """
                    CREATE TABLE alarm_settings (
                        user_id TEXT PRIMARY KEY,
                        low_bpm INTEGER NOT NULL CHECK (low_bpm BETWEEN 35 AND 219),
                        high_bpm INTEGER NOT NULL CHECK (high_bpm BETWEEN 36 AND 220),
                        alert_after_samples INTEGER NOT NULL
                            CHECK (alert_after_samples BETWEEN 1 AND 300),
                        CHECK (low_bpm < high_bpm)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO alarm_settings
                        (user_id, low_bpm, high_bpm, alert_after_samples)
                    SELECT user_id, low_bpm, high_bpm, ?
                    FROM alarm_settings_legacy
                    """,
                    (self.alert_after_samples,),
                )
                connection.execute("DROP TABLE alarm_settings_legacy")
            state_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(alarm_state)")
            }
            if "consecutive_samples" not in state_columns:
                connection.execute(
                    """
                    ALTER TABLE alarm_state ADD COLUMN consecutive_samples
                    INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_samples >= 0)
                    """
                )
                connection.execute(
                    """
                    UPDATE alarm_state
                    SET direction = NULL, outside_since_ms = NULL,
                        last_observed_ms = NULL, consecutive_samples = 0,
                        triggered = 0
                    """
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO alarm_settings
                    (user_id, low_bpm, high_bpm, alert_after_samples)
                VALUES (?, ?, ?, ?)
                """,
                (
                    self.default_user_id,
                    self.low_bpm,
                    self.high_bpm,
                    self.alert_after_samples,
                ),
            )

    @staticmethod
    def validate_user_id(user_id: str) -> None:
        if not isinstance(user_id, str) or not USER_ID.fullmatch(user_id):
            raise ValueError("user_id must match user-NNNN")

    def users(self):
        with _connect(self.database_path, read_only=True) as connection:
            rows = connection.execute(
                """
                SELECT user_id FROM alarm_settings
                ORDER BY CASE WHEN user_id = ? THEN 0 ELSE 1 END, user_id
                """,
                (self.default_user_id,),
            ).fetchall()
        return [row["user_id"] for row in rows]

    def ingest(self, payload: Dict) -> bool:
        required = {"event_id", "user_id", "observed_at", "bpm", "activity"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise ValueError("reading must contain exactly the required heart-rate fields")
        event_id = payload["event_id"]
        user_id = payload["user_id"]
        bpm = payload["bpm"]
        activity = payload["activity"]
        if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
            raise ValueError("event_id must be a non-empty string of at most 128 characters")
        self.validate_user_id(user_id)
        if isinstance(bpm, bool) or not isinstance(bpm, int) or not 35 <= bpm <= 220:
            raise ValueError("bpm must be an integer between 35 and 220")
        if not isinstance(activity, str) or activity not in ACTIVITIES:
            raise ValueError(f"activity must be one of {sorted(ACTIVITIES)}")
        if not isinstance(payload["observed_at"], str):
            raise ValueError("observed_at must be a timezone-aware ISO timestamp")
        observed_at = _utc_datetime(payload["observed_at"])
        observed_at_ms = int(observed_at.timestamp() * 1000)
        with _connect(self.database_path) as connection:
            settings = connection.execute(
                """
                SELECT low_bpm, high_bpm, alert_after_samples
                FROM alarm_settings WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if settings is None:
                connection.execute(
                    """
                    INSERT INTO alarm_settings
                        (user_id, low_bpm, high_bpm, alert_after_samples)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        self.low_bpm,
                        self.high_bpm,
                        self.alert_after_samples,
                    ),
                )
                settings = {
                    "low_bpm": self.low_bpm,
                    "high_bpm": self.high_bpm,
                    "alert_after_samples": self.alert_after_samples,
                }
            direction = (
                "low"
                if bpm < settings["low_bpm"]
                else "high"
                if bpm > settings["high_bpm"]
                else None
            )
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO heart_rate
                    (event_id, user_id, observed_at, observed_at_ms, event_date, bpm, activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    user_id,
                    observed_at.isoformat(),
                    observed_at_ms,
                    observed_at.date().isoformat(),
                    bpm,
                    activity,
                ),
            ).rowcount
            if not inserted:
                return False
            if activity == "rest":
                minute_ms = observed_at_ms // 60000 * 60000
                connection.execute(
                    """
                    INSERT INTO resting_hr_minute(user_id, minute_ms, bpm_total, sample_count)
                    VALUES (?, ?, ?, 1)
                    ON CONFLICT(user_id, minute_ms) DO UPDATE SET
                        bpm_total = bpm_total + excluded.bpm_total,
                        sample_count = sample_count + 1
                    """,
                    (user_id, minute_ms, bpm),
                )

            recent_alarm_readings = connection.execute(
                """
                SELECT bpm FROM heart_rate
                WHERE user_id = ?
                ORDER BY observed_at_ms DESC, rowid DESC
                LIMIT ?
                """,
                (user_id, settings["alert_after_samples"]),
            ).fetchall()
            consecutive_samples = _consecutive_out_of_range(
                recent_alarm_readings,
                direction,
                settings["low_bpm"],
                settings["high_bpm"],
            )
            state = connection.execute(
                "SELECT direction, outside_since_ms, last_observed_ms, "
                "consecutive_samples, triggered "
                "FROM alarm_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if state is None:
                state = {
                    "direction": None,
                    "outside_since_ms": None,
                    "last_observed_ms": None,
                    "consecutive_samples": 0,
                    "triggered": 0,
                }
            if (
                state["last_observed_ms"] is not None
                and observed_at_ms <= state["last_observed_ms"]
            ):
                return True
            if direction is None:
                if state["triggered"]:
                    self._write_alarm_event(
                        connection,
                        user_id,
                        state["direction"],
                        "resolved",
                        observed_at.isoformat(),
                        bpm,
                    )
                next_state = (None, None, None, 0, 0)
            elif state["direction"] != direction:
                if state["triggered"]:
                    self._write_alarm_event(
                        connection,
                        user_id,
                        state["direction"],
                        "resolved",
                        observed_at.isoformat(),
                        bpm,
                        resolution="threshold direction changed",
                    )
                next_state = (
                    direction,
                    observed_at_ms,
                    observed_at_ms,
                    consecutive_samples,
                    0,
                )
            else:
                outside_since_ms = state["outside_since_ms"]
                triggered = state["triggered"]
                if (
                    not triggered
                    and consecutive_samples >= settings["alert_after_samples"]
                ):
                    self._write_alarm_event(
                        connection,
                        user_id,
                        direction,
                        "triggered",
                        observed_at.isoformat(),
                        bpm,
                    )
                    triggered = 1
                next_state = (
                    direction,
                    outside_since_ms,
                    observed_at_ms,
                    consecutive_samples,
                    triggered,
                )

            connection.execute(
                """
                INSERT INTO alarm_state
                    (user_id, direction, outside_since_ms, last_observed_ms,
                     consecutive_samples, triggered)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    direction=excluded.direction,
                    outside_since_ms=excluded.outside_since_ms,
                    last_observed_ms=excluded.last_observed_ms,
                    consecutive_samples=excluded.consecutive_samples,
                    triggered=excluded.triggered
                """,
                (user_id, *next_state),
            )
        return True

    @staticmethod
    def _write_alarm_event(
        connection,
        user_id,
        direction,
        event_type,
        observed_at,
        bpm,
        resolution="returned to the configured range",
    ):
        state = "too low" if direction == "low" else "too high"
        detail = resolution if event_type == "resolved" else (
            "remained outside the configured range"
        )
        connection.execute(
            """
            INSERT INTO alarm_events
                (user_id, direction, event_type, observed_at, bpm, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                direction,
                event_type,
                observed_at,
                bpm,
                f"Heart rate {state} and {detail} ({bpm} bpm).",
            ),
        )

    def settings(self, user_id: str | None = None) -> Dict:
        user_id = user_id or self.default_user_id
        self.validate_user_id(user_id)
        with _connect(self.database_path, read_only=True) as connection:
            row = connection.execute(
                """
                SELECT low_bpm, high_bpm, alert_after_samples
                FROM alarm_settings WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"No monitor settings exist for {user_id}")
        return dict(row)

    def update_settings(
        self, user_id: str, low_bpm: int, high_bpm: int, alert_after_samples: int
    ) -> Dict:
        self.validate_user_id(user_id)
        if not 35 <= low_bpm <= 219 or not 36 <= high_bpm <= 220:
            raise ValueError("BPM thresholds must be between 35 and 220")
        if low_bpm >= high_bpm:
            raise ValueError("low_bpm must be lower than high_bpm")
        if not 1 <= alert_after_samples <= 300:
            raise ValueError("alert_after_samples must be between 1 and 300")
        now = datetime.now(timezone.utc).isoformat()
        with _connect(self.database_path) as connection:
            state = connection.execute(
                "SELECT direction, triggered FROM alarm_state WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if state and state["triggered"]:
                latest = connection.execute(
                    "SELECT bpm FROM heart_rate WHERE user_id = ? "
                    "ORDER BY observed_at_ms DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                self._write_alarm_event(
                    connection,
                    user_id,
                    state["direction"],
                    "resolved",
                    now,
                    latest["bpm"] if latest else low_bpm,
                    resolution="alert settings changed",
                )
            connection.execute(
                """
                INSERT INTO alarm_settings
                    (user_id, low_bpm, high_bpm, alert_after_samples)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    low_bpm=excluded.low_bpm,
                    high_bpm=excluded.high_bpm,
                    alert_after_samples=excluded.alert_after_samples
                """,
                (user_id, low_bpm, high_bpm, alert_after_samples),
            )
            connection.execute(
                "DELETE FROM alarm_state WHERE user_id = ?", (user_id,)
            )
        return self.settings(user_id)

    def snapshot(self, user_id: str | None = None) -> Dict:
        user_id = user_id or self.default_user_id
        self.validate_user_id(user_id)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with _connect(self.database_path, read_only=True) as connection:
            readings = connection.execute(
                """
                SELECT event_id, user_id, observed_at, bpm, activity
                FROM heart_rate
                WHERE user_id = ? AND observed_at_ms >= ?
                ORDER BY observed_at_ms DESC, rowid DESC LIMIT ?
                """,
                (user_id, now_ms - RECENT_WINDOW_SECONDS * 1000, RECENT_WINDOW_SECONDS),
            ).fetchall()
            latest = connection.execute(
                """
                SELECT event_id, user_id, observed_at, bpm, activity
                FROM heart_rate WHERE user_id = ?
                ORDER BY observed_at_ms DESC, rowid DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            settings = connection.execute(
                """
                SELECT low_bpm, high_bpm, alert_after_samples
                FROM alarm_settings WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            alarm_readings = connection.execute(
                """
                SELECT bpm FROM heart_rate
                WHERE user_id = ?
                ORDER BY observed_at_ms DESC, rowid DESC
                LIMIT ?
                """,
                (user_id, settings["alert_after_samples"]),
            ).fetchall()
            state = connection.execute(
                """
                SELECT direction, consecutive_samples, triggered
                FROM alarm_state WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            events = connection.execute(
                """
                SELECT direction, event_type, observed_at, bpm, message
                FROM alarm_events WHERE user_id = ?
                ORDER BY id DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
            minute_values = connection.execute(
                """
                SELECT 1.0 * bpm_total / sample_count AS bpm
                FROM resting_hr_minute
                WHERE user_id = ? AND minute_ms >= ?
                ORDER BY minute_ms
                """,
                (user_id, now_ms - BASELINE_WINDOW_SECONDS * 1000),
            ).fetchall()
        baseline_values = sorted(float(row["bpm"]) for row in minute_values)
        baseline = None
        if baseline_values:
            baseline = {
                "sample_minutes": len(baseline_values),
                "average_bpm": sum(baseline_values) / len(baseline_values),
                "median_bpm": _percentile(baseline_values, 0.5),
                "typical_low_bpm": _percentile(baseline_values, 0.05),
                "typical_high_bpm": _percentile(baseline_values, 0.95),
                "window_days": 7,
            }
        alarm = None
        if state and state["direction"]:
            consecutive_samples = _consecutive_out_of_range(
                alarm_readings,
                state["direction"],
                settings["low_bpm"],
                settings["high_bpm"],
            )
            alert_after_samples = settings["alert_after_samples"]
            alarm = {
                "direction": state["direction"],
                "triggered": bool(state["triggered"]),
                "consecutive_samples": consecutive_samples,
                "samples_until_trigger": max(
                    0, alert_after_samples - consecutive_samples
                ),
            }
        return {
            "user_id": user_id,
            "latest": dict(latest) if latest else None,
            "recent": [dict(row) for row in reversed(readings)],
            "settings": dict(settings),
            "alarm": alarm,
            "alarm_events": [dict(row) for row in events],
            "personal_baseline": baseline,
        }

    def reference_date(self):
        with _connect(self.database_path, read_only=True) as connection:
            row = connection.execute(
                "SELECT max(event_date) AS event_date FROM heart_rate"
            ).fetchone()
        return row["event_date"]


def _percentile(values, fraction):
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)
