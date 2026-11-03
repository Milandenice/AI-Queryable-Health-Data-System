# AI-Queryable Health Data System

A local, end-to-end system that generates synthetic heart-rate, sleep, steps, workout, and weight data; lands and transforms it in object storage; and serves analytical queries over HTTP.

> **Status:** The data pipeline, API, local SQL workbench, and local natural-language query path are implemented. Automated Python tests cover generation, ETL, API routes, and query execution.

## Goals and scope for take home

- One-command local startup with Docker Compose and no cloud account or paid API.
- Metric-specific synthetic data and schemas that preserve irregular sampling and missing observations.
- Separate generator, ETL, object-storage, and read-only query responsibilities.
- A local SQL workbench, validated HTTP query API (Bonus 1), and local natural-language query interface (Bonus 2).

## Reasoning for architectural choices

My perceived outcome of this take home was the following: A standalone app that is capable of computing the relation between heart-rate, sleep, steps and workout on one side and weight on the other. This app could be used to inform and help users how their habits influence their weight and what they can change in order to achieve their weight goals

## Architecture

```mermaid
flowchart LR
    U[Python generator<br/>metric-specific generators] -->|JSONL objects| R[(MinIO raw bucket)]
    R -->|batch read| E[Python ETL job]
    E -->|validated Parquet| C[(MinIO curated bucket)]
    C -->|read-only download| L[API ephemeral local cache]
    L --> Q[DuckDB embedded in API]
    Q --> A[FastAPI query service]
    Client -->|HTTP / JSON| A
    A -->|private HTTP| M[Ollama + Qwen2.5-Coder 1.5B]
    M -->|generated SQL| A
    B[MinIO bootstrap job] -->|buckets, policies, users| R
    B --> C
    U -. job succeeds before ETL .-> E
    E -. job succeeds before API .-> A
```

Compose runs MinIO, a MinIO bootstrap helper, one-shot generator and ETL jobs, the API, and a private Ollama service plus one-shot model-download job. The model job downloads Qwen2.5-Coder 1.5B on first startup; the model is kept in its own named Docker volume. The bootstrap helper creates the raw and curated buckets plus separate service users and least-privilege policies. Compose waits for the one-shot jobs to complete successfully before starting the API.

The generator writes JSON Lines objects to MinIO's raw bucket. ETL validates each record and writes metric-specific Parquet objects to the curated bucket. The API downloads curated objects using read-only credentials at startup, then DuckDB queries those local Parquet files. MinIO stores data on a seperate Docker volume, so it persists across container recreation without cloud services or a separately installed database.

In my choice to have one single docker volume for the data, I considered the following: MinIO demonstrates a real storage boundary, but a separate container is not inherently safer. Isolation comes from separate credentials and policies: the generator writes raw data, ETL reads raw and writes curated, and the API reads curated only. The batch jobs, MinIO, and Ollama share a private internal Compose network. Ollama also joins the bridge network for outbound model downloads, but its API is not published to the host. The API joins that bridge network so Docker can publish its HTTP port; the MinIO console is published only on host (`127.0.0.1:9001`), and the API only on host (`127.0.0.1:8000`). MinIO's object API is not published to the host. Compose uses local-demo credentials by default so a clean start needs no manual setup.

## Data design

The deterministic default workload contains **100 synthetic users over 30 days**, using start date `2025-01-01`. A single Python generator has independent metric-specific logic; the different sampling patterns do not need separate generator containers or streaming systems.

| Metric | Shape and sampling | Curated fields |
|---|---|---|
| Heart rate | Five-minute samples while worn; one-minute workout samples; non-wear gaps; overnight patterns and workout recovery | `event_id`, `user_id`, `observed_at`, `bpm`, `activity` |
| Sleep | One overnight record per user/night, represented as contiguous stage intervals | `event_id`, `user_id`, `night_date`, `start_at`, `end_at`, `stage`, `duration_minutes` |
| Steps | Fifteen-minute daytime count buckets; zero counts retained while worn, non-wear periods omitted | `event_id`, `user_id`, `bucket_start`, `bucket_end`, `steps` |
| Workouts | Occasional session intervals, with activity type and intensity | `event_id`, `user_id`, `start_at`, `end_at`, `activity_type`, `intensity`, `avg_heart_rate` |
| Weight | Irregular weigh-ins, typically one to seven days apart; no fabricated interpolation | `event_id`, `user_id`, `measured_at`, `weight_kg` |

Users receive illustrative gradual-loss, stable, or mild-gain weight trajectories and varying activity profiles. Workouts correlate with elevated heart rate and steps by construction; trends can be compared across profiles. Data is plausible enough for exercising queries.

## Ingestion, storage, and query decisions

- **Batch, not streaming:** each metric has a different natural cadence, but this bounded workload is generated locally and ingested as a complete run. A message broker or per-source streaming deployment adds complexity without useful latency or cost benefits. Live wearable feeds could later stream independently into the same validated storage contract.
- **JSONL landing:** raw objects are inspectable and preserve each source's event shape.
- **Metric-specific Parquet:** ETL validates records and stores each metric separately, partitioned by its natural UTC event date. Parquet is compact and efficient for columnar analytical scans. Invalid records fail ETL with the object key and line number rather than being silently dropped.
- **DuckDB rather than PostgreSQL:** this workload is batch-generated, read-oriented analytics over Parquet. DuckDB runs in-process, scans Parquet directly, and avoids operating a database server and loading a second copy into tables. PostgreSQL would be preferable for frequent row updates, transactional workloads, or centralized concurrent application access; revisit that choice if those needs arise.
- **Python application stack:** generation, validation, ETL, object-store access, DuckDB queries, and FastAPI are implemented in Python. MinIO is the separate storage service. We chose not to use PySpark, because we don't have a lot of data and because we are working locally on one machine. In the case we need to use distributed processing on multiple workers, we can make the change from python to pyspark.
- **API cache tradeoff:** Boto3 downloads the curated set to an ephemeral local directory when the API starts; DuckDB then reads local files. This avoids installing DuckDB's S3 extension at runtime and keeps query requests simple. Startup cost is proportional to the curated data size; direct S3 scans or an incremental cache would be more appropriate if the data grows substantially.

## Run locally

### Prerequisites

- Docker Engine (or Docker Desktop) with the Compose plugin.
- An available local port `8000` and enough disk for the Docker-managed MinIO volume.
- Internet access on the first run to pull container images, build the Python image, and download the local model.
- Enough disk for the Docker images, generated dataset, and model; 8 GB of system memory is recommended for comfortable CPU inference.

### Start

```sh
docker compose up
```

On a clean machine, Compose builds the Python and MinIO images, starts MinIO and Ollama, initializes buckets and service credentials, generates raw data, runs ETL, downloads Qwen2.5-Coder 1.5B, then starts the API. First startup also downloads the model and can take several minutes; later starts reuse it. No `.env` file, manual model setup, cloud account, paid API, or separate database installation is required. The API is at `http://localhost:8000`, and the MinIO console is at `http://localhost:9001`, both bound to host loopback. Use `docker compose up --build` to force a rebuild after changing application code or dependencies.

The MinIO console uses the root credentials `healthdata-admin` / `local-minio-root-password` by default. If configured in `.env`, use the `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` values instead.

The MinIO volume preserves data when containers are recreated, and a separate Ollama volume preserves the downloaded model. The generator and ETL jobs write completion markers; when Compose reruns these one-shot containers after a restart, they detect a matching complete dataset and reuse it rather than regenerating or retransferring it. The bootstrap job reapplies the service policies on startup so policy changes are reflected in existing MinIO volumes. If generation is interrupted before its marker is written, the next run safely rewrites the same deterministic object keys and finishes the dataset.

To intentionally delete all raw and curated data and start over:

```sh
docker compose down -v
docker compose up --build
```

Do not run `down -v` if you want to retain generated data.
If you change `DATA_SEED` or the generator's dataset layout, reset the volume before regenerating; object keys are stable across runs of the same configuration.
Adding the local model does not require deleting existing data volumes. Rebuild and start the updated services with `docker compose up --build`.

### Bonus 1: HTTP query API

The parameterized metric endpoints use structured filters, while the workbench accepts a restricted, read-only SQL `SELECT` over the five curated tables.

Dates are UTC calendar dates, and `end_date` is inclusive. Identifiers must match `user-NNNN`.

```sh
curl http://localhost:8000/health
curl 'http://localhost:8000/metrics/heart_rate?user_id=user-0001&start_date=2025-01-01&end_date=2025-01-02&limit=100'
curl 'http://localhost:8000/metrics/summary?user_id=user-0001&metrics=weight,steps,workouts&start_date=2025-01-01&end_date=2025-01-30'
```

- `GET /health` reports service liveness.
- `GET /metrics/{metric}` returns records for `heart_rate`, `sleep`, `steps`, `workouts`, or `weight`. Results are bounded (`limit` 1–1,000).
- `GET /metrics/summary` returns daily summaries for a comma-separated metric list; it defaults to all five. Weight appears only on measurement days, and absent observations are not interpolated.

Both routes return a JSON object echoing the selected metric(s), user, date range, and results. The summary response also declares its daily grain. For example, a summary response has this shape:

```json
{
  "user_id": "user-0001",
  "start_date": "2025-01-01",
  "end_date": "2025-01-07",
  "metrics": ["heart_rate", "steps"],
  "grain": "day",
  "results": [
    {
      "date": "2025-01-01",
      "heart_rate": {
        "average_bpm": 65.2,
        "min_bpm": 51,
        "max_bpm": 92,
        "observations": 270
      },
      "steps": {
        "total_steps": 8612,
        "buckets": 64
      }
    }
  ]
}
```

The API validates metric names, user IDs, date ordering, date-range bounds, and result limits. Invalid parameters return HTTP `422`; unsupported metric names on the records route return `404`. Unexpected storage or query failures are not disguised as successful responses. On startup, failure to download curated data prevents the API from becoming ready.

### Local SQL workbench

Open `http://localhost:8000` to use the browser-based workbench. The left catalog lists available tables and Parquet file counts. Right-click a table to preview rows, inspect its schema, or add a starter `SELECT` to the editor. Ask a plain-language question in the natural-language panel; the generated SQL appears in the editor and results appear below. Run a query with the button or `Ctrl+Enter`; results appear in a scrollable grid. The editor includes sample queries, including an example that joins steps and workouts.

The workbench submits SQL to `POST /api/query` as JSON (`{"sql":"SELECT ... FROM heart_rate"}`) and returns the selected columns, rows, row count, and whether results were truncated. `GET /api/catalog`, `/api/catalog/{table}/schema`, and `/api/catalog/{table}/preview` support the catalog actions. Queries are capped at 1,000 returned rows and 20,000 characters.

For safety, only one `SELECT` statement is accepted. It can read only the known curated tables (`heart_rate`, `sleep`, `steps`, `workouts`, `weight`); table functions, external file reads, non-allowlisted functions, Data Definition Language, and Data Manipulation Language are rejected. SQL parse/validation and execution errors return HTTP `400` with a clear detail. Structured endpoint validation errors remain HTTP `422`.

OpenAPI/Swagger documentation is available at `http://localhost:8000/docs`.

### Bonus 2: local natural-language query

`POST /api/nl-query` accepts `{"question":"What was user-0001's average resting heart rate last week?"}` and returns the question, model name, generated SQL, columns, rows, row count, and truncation status. Relative dates are interpreted against the latest date in the loaded dataset. The model is prompted to use DuckDB SQL (not MySQL or PostgreSQL syntax) and to filter date partition columns directly using date literals. The API then submits the generated SQL through the same SQL parser, allowlist, and 1,000-row cap as manual queries. Model unavailability returns HTTP `503`; malformed model output or SQL rejected by validation returns HTTP `502`. A successful response is not a guarantee that the question was interpreted correctly: inspect the SQL and results.

```sh
curl -X POST http://localhost:8000/api/nl-query \
  -H 'Content-Type: application/json' \
  -d '{"question":"What was user-0001 average resting heart rate last week?"}'
```

The default model is `qwen2.5-coder:1.5b`, configured through `OLLAMA_MODEL` in `.env` or the environment. It is a smaller code-oriented model than the previous 3B general-purpose Qwen model and is intended to reduce local inference and download costs. It runs locally without an API key, account, or per-request fee. Actual latency and SQL quality depend on the machine and question; validate the generated SQL and results before relying on them. Ollama has no published host port; the API reaches it over the Compose network.

### Tests

```sh
python -m pip install -r requirements-dev.txt
python -m pytest
```

Tests cover deterministic generation, distinct metric shapes, completion-marker reuse on restart, ETL validation, DuckDB record and cross-metric summary queries, and mocked local-model request/response behavior. The full Compose path and the example requests should additionally be smoke-tested on a machine with Docker installed.

## Areas of depth

1. **Weight insights app** that can be found in the weight-insights-app branch of this repo: The area of depth would be the addition of a gold layer of data consisting of computed weight recommendations. This computation has been done by a machine learning script. It showcases how to deliver insightful data to the customer
2. **Heart rate alarm:** can be found in the heart-rate-alarm branch of this repo: The area of depth would be the difference of data injection (streaming in stead of batch), because the goal of this app would be to know the heart rate real-time

## AI usage

AI has been extensively used during this take home exam. The way I worked was as follows: I decided upon the use cases, architecture, what tools to use and the AI did the coding.

# What would I do if I would have 2 more weeks
If I would have more time, I would work on creating a production ready system in the cloud (for example in AWS). First by simply migrating the microsystems in their containers, but secondly by using the cloud native solutions, like native S3 buckets in stead of Minio in a container. 