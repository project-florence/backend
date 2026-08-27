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

## Async conventions (2026-08 full refactor)

The codebase is fully async. Follow these rules when editing:

- Endpoints are `async def`; dependencies (`get_current_user`, `validate_ticker`, `require_feature`, `require_job_slot`) are async.
- **DB:** `src/core/database.py` exposes an async `db` proxy on top of psycopg3 (`AsyncConnectionPool`). `async with db.cursor() as cur:` returns dict rows by default; pass `row_factory=None` for tuple rows (`row[0]` access). One connection per task is held in a ContextVar and returned to the pool by `db.commit()`/`db.rollback()`; `db.release_current()` is called by the auth middleware after each request. Never call sync psycopg2 APIs.
- **Redis:** `src/core/redis.py` `r` is an async proxy (redis.asyncio): `await r.get(...)`, `await r.set(..., nx=True, ex=...)`. Returns `None` when Redis is down (cache-less mode).
- **HTTP:** use `src/clients/http.py` shared `httpx.AsyncClient` (`await get_client()`), never `requests`.
- **LLM/embeddings:** `AsyncOpenAI` clients in `src/clients/llm.py` / `embedding.py`; always `await`.
- **yfinance / trafilatura / BigQuery / fredapi / argon2 / numpy:** sync libs stay sync — call them via `asyncio.to_thread(...)` so the event loop is never blocked.
- **Cron:** `src/clients/cron.py` is an asyncio scheduler; job sources must define `async def __cron_main__()`. `src/cron/tasks.py` functions are all async.
- **Scripts:** each script uses `asyncio.run(main())` with async internals.

## Database and Operations

- `src/core/database.py:init_db()` is the runtime schema source of truth and runs on API startup; there is no migration runner in the repository.
- The numbered files in `migrations/` are manual/historical SQL. When changing schema, reconcile `init_db()` and any applicable migration rather than assuming the files are applied automatically.
- `scripts/setup_crontab.sh` writes recurring jobs to the current user's crontab and `/var/log/florence`; run it only when intentionally installing the production schedule.
- Price refresh tiers are available as `python scripts/update_prices.py --tier bist30|popular|rest`; scheduled frequencies are documented in `scripts/setup_crontab.sh`.

## Verification

- No repository test, lint, formatter, typecheck, CI, or pre-commit configuration is present.
- For a dependency-free syntax check after Python edits, run `python -m compileall src scripts`.
- There is no `launch.sh` workflow to rely on; it is currently empty.

## Integration tests (opt-in, `tests/test_llm_integration.py`)

The rest of `tests/` is fully hermetic (`fake_db`/`fake_redis`, no network/DB — `python -m
pytest`, ~550 tests, ~3s). REFACTOR_PLAN.md Step 6 added one deliberately non-hermetic file that
runs against **real** local Postgres/Redis, because three real bugs surfaced during the LLM
provider refactor that a mocked suite structurally cannot see:

1. A synchronous bridge (`asyncio.run` in a worker thread) touching the loop-bound
   `AsyncConnectionPool` / async Redis client from a foreign event loop — `fake_db`/`fake_redis`
   carry no loop affinity, so this class of bug is invisible to them.
2. `llm_settings.provider` → `llm_providers` foreign key requiring a row even for keyless
   providers — only a real Postgres FK constraint enforces this.
3. `opencode-zen` serving `/models` without auth but rejecting `/chat/completions` with 401 —
   not covered by this test layer (no real network calls here), but the distinction is
   documented in `src/llm/providers.py`.

Marker: `@pytest.mark.integration` / `pytestmark = pytest.mark.integration`, registered in
`pyproject.toml`. `addopts = "-m 'not integration'"` means plain `python -m pytest` never runs
or connects for these tests; an explicit `-m integration` on the command line overrides that.

```bash
docker compose up -d postgres redis   # florence_postgres / florence_redis, if not already up
python -m pytest -m integration -q
```

If the containers aren't reachable, the tests **skip** cleanly (a short, independent connection
probe in the `_integration_target` fixture) rather than failing. Every test cleans up its own
rows in a `_cleanup` fixture (`llm_settings` before `llm_providers`, matching the FK order;
`token_usage` rows are tagged with a unique `purpose` value so pre-existing data is never
touched) — running the file twice in a row passes both times. A safety guard skips the whole
file unless `POSTGRES_HOST`/`REDIS_HOST` resolve to `localhost`/`127.0.0.1` (override with
`FLORENCE_INTEGRATION_ALLOW_REMOTE=1`) — this layer must never reach production.
