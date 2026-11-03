import json
from datetime import date, timedelta
from typing import Dict, List
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ModelUnavailableError(RuntimeError):
    pass


class ModelResponseError(RuntimeError):
    pass


def _prompt(question: str, tables: List[Dict], reference_date: date) -> str:
    schema = "\n".join(
        f"- {table['name']}: "
        + ", ".join(
            f"{column['name']} {column['type']}"
            for column in table["columns"]
        )
        for table in tables
    )
    ten_day_start = reference_date - timedelta(days=9)
    return f"""Translate the user's question into exactly one DuckDB SQL SELECT query.
Return only a JSON object with one string property named "sql".

Rules:
- Use DuckDB SQL syntax only; do not use MySQL or PostgreSQL-specific syntax.
- Query only these tables and columns:
{schema}
- Do not use table functions, external files, system catalogs, DDL, DML, or multiple statements.
- Use only ordinary SQL aggregate, date, and string functions.
- Date partition columns such as event_date and night_date are already DATE values. Group and filter on them directly; do not wrap them in DATE(...).
- Do not use DATE_SUB or other MySQL date functions. For date ranges, use DuckDB DATE literals and BETWEEN, for example:
  event_date BETWEEN DATE '{ten_day_start.isoformat()}' AND DATE '{reference_date.isoformat()}'
- The example above is the inclusive last 10 dataset dates. For other relative periods, calculate the inclusive range from the dataset's latest date; never use CURRENT_DATE because the dataset may be historical.
- Keep results useful and bounded; add LIMIT 100 unless the query is an aggregate.
- User IDs have the form user-0001. Do not invent a user ID if none is specified.
- The synthetic dataset's latest date is {reference_date.isoformat()}. Interpret relative periods such as "last week" relative to that date. If a month is given without a year, use the year of the dataset's latest date.
- For resting heart rate, filter heart_rate.activity = 'rest'.
- For disrupted sleep, summarize awake-stage minutes per user from sleep records.
- Never infer causation from these synthetic data.

User question:
{question}
"""


def generate_sql(
    question: str,
    tables: List[Dict],
    reference_date: date,
    endpoint: str,
    model: str,
) -> str:
    request_body = {
        "model": model,
        "prompt": _prompt(question, tables, reference_date),
        "format": "json",
        "stream": False,
        "options": {"temperature": 0},
    }
    request = Request(
        f"{endpoint.rstrip('/')}/api/generate",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        raise ModelUnavailableError(
            f"Local language model returned HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise ModelUnavailableError(
            "Could not reach the local language model. Check that the Ollama "
            "service is running and the model has finished downloading."
        ) from error
    except json.JSONDecodeError as error:
        raise ModelResponseError("Local language model returned invalid JSON") from error

    if not isinstance(payload, dict):
        raise ModelResponseError("Local language model returned an invalid response")
    response_text = payload.get("response")
    if not isinstance(response_text, str):
        raise ModelResponseError("Local language model response has no text")
    try:
        generated = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise ModelResponseError(
            "Local language model did not return the required JSON SQL response"
        ) from error
    sql = generated.get("sql") if isinstance(generated, dict) else None
    if not isinstance(sql, str) or not sql.strip():
        raise ModelResponseError(
            "Local language model response did not contain a SQL query"
        )
    return sql.strip()
