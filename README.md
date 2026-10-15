# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. The contract and business rules are in `openapi.yaml` and `PROBLEM_STATEMENT.docx`; stored document shapes are in `DATA_MODEL.md`. All application code is in `app/main.py`.

## Requirements and setup

- Python 3.10 or newer
- MongoDB 6.0 or newer (the aggregation pipelines use window functions; MongoDB 7 is recommended)

Create and activate a virtual environment, then install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the process environment or a local `.env` file before starting. Process environment values take precedence over `.env`; defaults are `mongodb://localhost:27017` and `attendance_db`. Never commit `.env` or credentials. For a local MongoDB instance, start MongoDB separately. The optional `sample_seed.py` script loads sample records and drops/replaces collections; do not run it against a database containing data you need.

## Run

```powershell
python -m uvicorn app.main:app --port 8000
```

Interactive API documentation is available at `http://localhost:8000/docs`. Indexes are created idempotently at application startup.

## Endpoints

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`
- `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

Analytics use MongoDB aggregation pipelines. The explain endpoint uses MongoDB `executionStats` for supported query identifiers. See `openapi.yaml` for parameters, response schemas, status codes, and business rules.

## Tests

Install the test runner if it is not already available (`python -m pip install pytest`), then run:

```powershell
python -m pytest -q
python -m py_compile app/main.py tests/*.py
```

Current tests use fake/in-memory database doubles; they do not verify aggregation results or query plans against a live MongoDB server. A real MongoDB integration run is still needed before relying on server-side pipeline behavior and index plans.

## Known limitations

- There is no authentication or authorization; `regularized_by` is audit text supplied by the caller.
- `sample_seed.py` is destructive to the target collections.
- Explain returns 404 for a referenced employee or department that does not exist, and 503 if MongoDB cannot execute the explain command; both are declared in the OpenAPI response contract.
