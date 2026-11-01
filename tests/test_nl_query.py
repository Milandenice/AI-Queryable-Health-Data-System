import json
from datetime import date
from urllib.error import URLError

import pytest

from app.nl_query import (
    ModelResponseError,
    ModelUnavailableError,
    generate_sql,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_generate_sql_uses_local_model_and_dataset_date(monkeypatch):
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        observed["body"] = json.loads(request.data)
        return FakeResponse({"response": '{"sql":"SELECT count(*) FROM heart_rate"}'})

    monkeypatch.setattr("app.nl_query.urlopen", fake_urlopen)
    sql = generate_sql(
        question="How many heart-rate records are there?",
        tables=[
            {
                "name": "heart_rate",
                "columns": [{"name": "user_id", "type": "string"}],
            }
        ],
        reference_date=date(2025, 1, 30),
        endpoint="http://ollama:11434/",
        model="qwen2.5:3b",
    )

    assert sql == "SELECT count(*) FROM heart_rate"
    assert observed["url"] == "http://ollama:11434/api/generate"
    assert observed["timeout"] == 180
    assert observed["body"]["model"] == "qwen2.5:3b"
    assert observed["body"]["stream"] is False
    assert observed["body"]["format"] == "json"
    assert "2025-01-30" in observed["body"]["prompt"]
    assert "user_id string" in observed["body"]["prompt"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"response": "not JSON"},
        {"response": '{"query":"SELECT 1"}'},
        [],
    ],
)
def test_generate_sql_rejects_malformed_model_responses(monkeypatch, payload):
    monkeypatch.setattr(
        "app.nl_query.urlopen", lambda *_args, **_kwargs: FakeResponse(payload)
    )
    with pytest.raises(ModelResponseError):
        generate_sql("show heart rate", [], date(2025, 1, 30), "http://ollama", "model")


def test_generate_sql_reports_local_model_unavailable(monkeypatch):
    def fail_request(*_args, **_kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr("app.nl_query.urlopen", fail_request)
    with pytest.raises(ModelUnavailableError, match="Could not reach"):
        generate_sql("show heart rate", [], date(2025, 1, 30), "http://ollama", "model")
