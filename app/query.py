from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence

import duckdb
import sqlglot
from sqlglot import exp

from app.etl import METRIC_SCHEMAS

METRICS = {"heart_rate", "sleep", "steps", "workouts", "weight"}
MAX_QUERY_LENGTH = 20000
MAX_QUERY_ROWS = 1000
ALLOWED_FUNCTIONS = {
    "ABS",
    "AVG",
    "COALESCE",
    "COUNT",
    "CURRENT_DATE",
    "CURRENT_TIMESTAMP",
    "DATE_DIFF",
    "DATE_PART",
    "DATE_TRUNC",
    "EXTRACT",
    "GREATEST",
    "LEAST",
    "LOWER",
    "MAX",
    "MIN",
    "NULLIF",
    "ROUND",
    "STDDEV",
    "SUM",
    "TIMESTAMP_TRUNC",
    "UPPER",
}
SUMMARY_QUERIES = {
    "heart_rate": """
        SELECT event_date AS day, round(avg(bpm), 1) AS average_bpm,
               min(bpm) AS min_bpm, max(bpm) AS max_bpm, count(*) AS observations
        FROM {source}
        WHERE user_id = ? AND event_date BETWEEN ? AND ?
        GROUP BY event_date
    """,
    "steps": """
        SELECT event_date AS day, sum(steps) AS total_steps, count(*) AS buckets
        FROM {source}
        WHERE user_id = ? AND event_date BETWEEN ? AND ?
        GROUP BY event_date
    """,
    "sleep": """
        SELECT night_date AS day,
               sum(duration_minutes) FILTER (WHERE stage != 'awake') AS sleep_minutes,
               sum(duration_minutes) FILTER (WHERE stage = 'awake') AS awake_minutes
        FROM {source}
        WHERE user_id = ? AND night_date BETWEEN ? AND ?
        GROUP BY night_date
    """,
    "workouts": """
        SELECT event_date AS day, count(*) AS workout_count,
               round(sum(date_diff('second', start_at, end_at)) / 60.0, 1)
                   AS workout_minutes,
               round(avg(avg_heart_rate), 1) AS average_workout_bpm
        FROM {source}
        WHERE user_id = ? AND event_date BETWEEN ? AND ?
        GROUP BY event_date
    """,
    "weight": """
        SELECT event_date AS day, arg_max(weight_kg, measured_at) AS weight_kg,
               count(*) AS weigh_ins
        FROM {source}
        WHERE user_id = ? AND event_date BETWEEN ? AND ?
        GROUP BY event_date
    """,
}


def _source(data_dir: Path, metric: str) -> str:
    pattern = (data_dir / metric / "event_date=*" / "*.parquet").as_posix()
    return f"read_parquet('{pattern}', hive_partitioning=true)"


def _validate_dates(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValueError("end_date must be on or after start_date")
    if (end_date - start_date).days > 365:
        raise ValueError("Date range cannot exceed 366 days")


def _validate_select(sql: str) -> str:
    if not sql or not sql.strip():
        raise ValueError("Query cannot be empty")
    if len(sql) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query cannot exceed {MAX_QUERY_LENGTH} characters")
    try:
        statements = sqlglot.parse(sql, read="duckdb")
    except sqlglot.errors.ParseError as error:
        raise ValueError(f"Invalid SQL: {error}") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only one read-only SELECT statement is allowed")

    expression = statements[0]
    if any(
        isinstance(node, (exp.DML, exp.DDL, exp.Command))
        for node in expression.walk()
    ):
        raise ValueError("Data-changing or administrative statements are not allowed")
    cte_names = {
        cte.alias_or_name.lower()
        for cte in expression.find_all(exp.CTE)
    }
    for table in expression.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise ValueError("Table functions and external data sources are not allowed")
        if table.db or table.catalog:
            raise ValueError("Database-qualified table names are not allowed")
        table_name = table.name.lower()
        if table_name not in METRICS and table_name not in cte_names:
            raise ValueError(
                f"Unknown table '{table.name}'. Available tables: {', '.join(sorted(METRICS))}"
            )

    for function in expression.find_all(exp.Func):
        if isinstance(function, (exp.And, exp.Or)):
            continue
        if function.sql_name().upper() not in ALLOWED_FUNCTIONS:
            name = function.name or function.sql_name()
            raise ValueError(f"Function '{name}' is not allowed in queries")

    return expression.sql(dialect="duckdb")


class QueryService:
    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir).resolve()
        if not self.data_dir.is_dir():
            raise RuntimeError(f"Curated data cache is missing: {self.data_dir}")

    def catalog(self) -> List[Dict]:
        tables = []
        for metric in sorted(METRICS):
            files = sorted(
                (self.data_dir / metric).glob("event_date=*/*.parquet")
            )
            if not files:
                continue
            schema = METRIC_SCHEMAS[metric]
            columns = [
                {"name": field.name, "type": str(field.type)}
                for field in schema
            ]
            columns.append({"name": "event_date", "type": "DATE"})
            tables.append(
                {
                    "name": metric,
                    "columns": columns,
                    "file_count": len(files),
                }
            )
        return tables

    def table_schema(self, table_name: str) -> List[Dict]:
        table = table_name.lower()
        if table not in METRICS:
            raise ValueError(f"Unknown table '{table_name}'")
        match = next(
            (item for item in self.catalog() if item["name"] == table),
            None,
        )
        if match is None:
            raise FileNotFoundError(f"No data is available for table '{table_name}'")
        return match["columns"]

    def preview(self, table_name: str, limit: int = 20) -> Dict:
        table = table_name.lower()
        if table not in METRICS:
            raise ValueError(f"Unknown table '{table_name}'")
        rows = self.run_sql(f"SELECT * FROM {table} LIMIT {limit}")
        return rows

    def run_sql(self, sql: str) -> Dict:
        statement = _validate_select(sql)
        with duckdb.connect(":memory:") as connection:
            for metric in METRICS:
                files = list(
                    (self.data_dir / metric).glob("event_date=*/*.parquet")
                )
                if not files:
                    continue
                parquet_glob = (self.data_dir / metric / "event_date=*/*.parquet").as_posix()
                connection.execute(
                    f"CREATE VIEW {metric} AS "
                    f"SELECT * FROM read_parquet('{parquet_glob}', "
                    "hive_partitioning=true)"
                )
            cursor = connection.execute(
                f"SELECT * FROM ({statement}) AS _query_result LIMIT ?",
                [MAX_QUERY_ROWS + 1],
            )
            columns = [column[0] for column in cursor.description]
            rows = cursor.fetchall()
            truncated = len(rows) > MAX_QUERY_ROWS
            rows = rows[:MAX_QUERY_ROWS]
            return {
                "columns": columns,
                "rows": [dict(zip(columns, row)) for row in rows],
                "row_count": len(rows),
                "truncated": truncated,
            }

    def records(
        self,
        metric: str,
        user_id: str,
        start_date: date,
        end_date: date,
        limit: int,
    ) -> List[Dict]:
        if metric not in METRICS:
            raise ValueError(f"Unsupported metric: {metric}")
        _validate_dates(start_date, end_date)
        fields = {
            "heart_rate": "event_id, user_id, observed_at, bpm, activity",
            "sleep": "event_id, user_id, night_date, start_at, end_at, stage, duration_minutes",
            "steps": "event_id, user_id, bucket_start, bucket_end, steps",
            "workouts": "event_id, user_id, start_at, end_at, activity_type, intensity, avg_heart_rate",
            "weight": "event_id, user_id, measured_at, weight_kg",
        }[metric]
        date_field = {
            "heart_rate": "event_date",
            "sleep": "night_date",
            "steps": "event_date",
            "workouts": "event_date",
            "weight": "event_date",
        }[metric]
        sql = (
            f"SELECT {fields} FROM {_source(self.data_dir, metric)} "
            f"WHERE user_id = ? AND {date_field} BETWEEN ? AND ? "
            f"ORDER BY {date_field} LIMIT ?"
        )
        with duckdb.connect(":memory:") as connection:
            cursor = connection.execute(
                sql, [user_id, start_date, end_date, limit]
            )
            columns = [column[0] for column in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def summary(
        self,
        user_id: str,
        start_date: date,
        end_date: date,
        metrics: Sequence[str],
    ) -> List[Dict]:
        _validate_dates(start_date, end_date)
        if not metrics or any(metric not in METRICS for metric in metrics):
            raise ValueError("metrics must contain supported metric names")
        by_day: Dict[date, Dict] = {}
        with duckdb.connect(":memory:") as connection:
            for metric in metrics:
                sql = SUMMARY_QUERIES[metric].format(source=_source(self.data_dir, metric))
                cursor = connection.execute(
                    sql, [user_id, start_date, end_date]
                )
                columns = [column[0] for column in cursor.description]
                for row in cursor.fetchall():
                    values = dict(zip(columns, row))
                    day = values.pop("day")
                    by_day.setdefault(day, {"date": day.isoformat()})[metric] = values
        return [by_day[day] for day in sorted(by_day)]
