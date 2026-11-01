import sqlite3
from datetime import date
from pathlib import Path
from typing import Dict, List, Sequence

import sqlglot
from sqlglot import exp

from app.heart_rate_store import HeartRateStore

METRICS = {"heart_rate"}
MAX_QUERY_LENGTH = 20000
MAX_QUERY_ROWS = 1000
ALLOWED_FUNCTIONS = {
    "ABS",
    "AVG",
    "CAST",
    "COALESCE",
    "COUNT",
    "DATE",
    "DATETIME",
    "LOWER",
    "MAX",
    "MIN",
    "ROUND",
    "STRFTIME",
    "SUM",
    "TIME",
    "UPPER",
}


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
        statements = sqlglot.parse(sql, read="sqlite")
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
    cte_names = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    for table in expression.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise ValueError("Table functions and external data sources are not allowed")
        if table.db or table.catalog:
            raise ValueError("Database-qualified table names are not allowed")
        table_name = table.name.lower()
        if table_name not in METRICS and table_name not in cte_names:
            raise ValueError(
                f"Unknown table '{table.name}'. Available tables: heart_rate"
            )

    for function in expression.find_all(exp.Func):
        if isinstance(function, (exp.And, exp.Or)):
            continue
        if function.sql_name().upper() not in ALLOWED_FUNCTIONS:
            name = function.name or function.sql_name()
            raise ValueError(f"Function '{name}' is not allowed in queries")

    return expression.sql(dialect="sqlite")


class QueryService:
    def __init__(self, database_path: str):
        self.database_path = Path(database_path).resolve()
        if not self.database_path.is_file():
            raise RuntimeError(f"Heart-rate database is missing: {self.database_path}")

    def catalog(self) -> List[Dict]:
        with sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro", uri=True
        ) as connection:
            columns = connection.execute("PRAGMA table_info(heart_rate)").fetchall()
            count = connection.execute("SELECT count(*) FROM heart_rate").fetchone()[0]
        if not columns:
            return []
        return [
            {
                "name": "heart_rate",
                "columns": [
                    {"name": row[1], "type": row[2]}
                    for row in columns
                ],
                "record_count": count,
            }
        ]

    def table_schema(self, table_name: str) -> List[Dict]:
        table = table_name.lower()
        if table not in METRICS:
            raise ValueError(f"Unknown table '{table_name}'")
        catalog = self.catalog()
        if not catalog:
            raise FileNotFoundError(f"No data is available for table '{table_name}'")
        return catalog[0]["columns"]

    def preview(self, table_name: str, limit: int = 20) -> Dict:
        table = table_name.lower()
        if table not in METRICS:
            raise ValueError(f"Unknown table '{table_name}'")
        if not 1 <= limit <= 100:
            raise ValueError("Preview limit must be between 1 and 100")
        return self.run_sql(f"SELECT * FROM {table} LIMIT {limit}")

    def run_sql(self, sql: str) -> Dict:
        statement = _validate_select(sql)
        try:
            with sqlite3.connect(
                f"file:{self.database_path.as_posix()}?mode=ro", uri=True
            ) as connection:
                connection.row_factory = sqlite3.Row
                cursor = connection.execute(
                    f"SELECT * FROM ({statement}) AS _query_result LIMIT ?",
                    (MAX_QUERY_ROWS + 1,),
                )
                rows = cursor.fetchall()
                columns = [item[0] for item in cursor.description]
        except sqlite3.Error as error:
            raise ValueError(f"Query failed: {error}") from error
        truncated = len(rows) > MAX_QUERY_ROWS
        rows = rows[:MAX_QUERY_ROWS]
        return {
            "columns": columns,
            "rows": [dict(row) for row in rows],
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
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        HeartRateStore.validate_user_id(user_id)
        with sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT event_id, user_id, observed_at, bpm, activity
                FROM heart_rate
                WHERE user_id = ? AND event_date BETWEEN ? AND ?
                ORDER BY observed_at_ms DESC LIMIT ?
                """,
                (user_id, start_date.isoformat(), end_date.isoformat(), limit),
            ).fetchall()
        return [dict(row) for row in rows]

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
        HeartRateStore.validate_user_id(user_id)
        with sqlite3.connect(
            f"file:{self.database_path.as_posix()}?mode=ro", uri=True
        ) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT event_date AS date, round(avg(bpm), 1) AS average_bpm,
                       min(bpm) AS min_bpm, max(bpm) AS max_bpm,
                       count(*) AS observations
                FROM heart_rate
                WHERE user_id = ? AND event_date BETWEEN ? AND ?
                GROUP BY event_date
                ORDER BY event_date
                """,
                (user_id, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        return [
            {
                "date": row["date"],
                "heart_rate": {
                    "average_bpm": row["average_bpm"],
                    "min_bpm": row["min_bpm"],
                    "max_bpm": row["max_bpm"],
                    "observations": row["observations"],
                },
            }
            for row in rows
        ]
