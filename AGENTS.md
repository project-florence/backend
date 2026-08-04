# Florence Backend Agent Guide

## Setup and Run

- Use Python 3.12; dependencies are pinned in `requirements.txt`.
- Create a virtual environment, activate it, and install dependencies with `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.
- Copy `.env.example` to `.env` and provide the required secrets, database credentials, and Google service-account path before importing `src.main`; `.env` is ignored.
- Start local dependencies with `docker compose up -d postgres redis searxng`. The host ports are configured by `.env` and default to Postgres `5433`, Redis `5434`, and SearXNG `5435`.
- Run the API with `uvicorn src.main:app --reload --host 0.0.0.0 --port 7055`; startup immediately initializes the database, configuration, external clients, and ticker cache.
- The Docker image runs `src.main:app` on port `7055`; the compose `admin` service separately serves `src.admin:admin_app` over `/run/florence/admin.sock`.

## Structure

- `src/main.py` is the application entrypoint; `src/api/router.py` mounts feature routers under `/api/v1`.
- Keep HTTP handlers in `src/api`, domain and data operations in `src/services`, integrations in `src/clients`, and shared infrastructure in `src/core`.
- `scripts/` contains operational and data-maintenance jobs, not a test suite. They depend on the configured database, Redis, and external APIs.

## Database and Operations

- `src/core/database.py:init_db()` is the runtime schema source of truth and runs on API startup; there is no migration runner in the repository.
- The numbered files in `migrations/` are manual/historical SQL. When changing schema, reconcile `init_db()` and any applicable migration rather than assuming the files are applied automatically.
- `scripts/setup_crontab.sh` writes recurring jobs to the current user's crontab and `/var/log/florence`; run it only when intentionally installing the production schedule.
- Price refresh tiers are available as `python scripts/update_prices.py --tier bist30|popular|rest`; scheduled frequencies are documented in `scripts/setup_crontab.sh`.

## Verification

- No repository test, lint, formatter, typecheck, CI, or pre-commit configuration is present.
- For a dependency-free syntax check after Python edits, run `python -m compileall src scripts`.
- There is no `launch.sh` workflow to rely on; it is currently empty.
