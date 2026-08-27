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
- **LLM:** `src/llm/agents.py::build_agent(purpose)` resolves the single `llm_settings` selection (singleton — no per-purpose config, see REFACTOR_PLAN.md Adim 6.5) and builds a pydantic-ai model/provider; `src/clients/llm.py` is a health probe only, not the traffic path. There is no embedding client — `src/clients/embedding.py` was removed (embedding is not an LLM and had no callers); `src/analysis/stock_vector.py` fills numeric feature vectors and is unrelated.
- **yfinance / trafilatura / BigQuery / fredapi / argon2 / numpy:** sync libs stay sync — call them via `asyncio.to_thread(...)` so the event loop is never blocked.
- **Cron:** `src/clients/cron.py` is an asyncio scheduler; job sources must define `async def __cron_main__()`. `src/cron/tasks.py` functions are all async.
- **Scripts:** each script uses `asyncio.run(main())` with async internals.

## Database and Operations

- `src/core/database.py:init_db()` is the runtime schema source of truth and runs on API startup; there is no migration runner in the repository.
- The numbered files in `migrations/` are manual/historical SQL. When changing schema, reconcile `init_db()` and any applicable migration rather than assuming the files are applied automatically.
- `scripts/setup_crontab.sh` writes recurring jobs to the current user's crontab and `/var/log/florence`; run it only when intentionally installing the production schedule.
- Price refresh tiers are available as `python scripts/update_prices.py --tier bist30|popular|rest`; scheduled frequencies are documented in `scripts/setup_crontab.sh`.

## Verification

- No repository lint, formatter, typecheck, CI, or pre-commit configuration is present. There
  *is* a test suite (`tests/`, pytest) — see "Test suite" below; it does not run in CI.
- For a dependency-free syntax check after Python edits, run `python -m compileall src scripts`.
- There is no `launch.sh` workflow to rely on; it is currently empty.

## Test suite (`tests/`, pytest) — two lanes

Two lanes, selected by the `integration` marker (registered in `pyproject.toml`):

- **Hermetic lane (default)** — `python -m pytest`. 553 tests, **~3.3s warm** (measured; see
  "Measured timings" below). No real Postgres, Redis, or network socket is reachable from this
  lane — see "Hermeticity guard" below. `addopts = "-m 'not integration'"` in `pyproject.toml`
  makes plain `pytest` skip the integration file entirely by default.
- **Integration lane (opt-in)** — `python -m pytest -m integration -q`. 8 tests in
  `tests/test_llm_integration.py`, run against **real** local Postgres/Redis (see below).

```bash
python -m pytest -q                    # hermetic lane, 553 tests
python -m pytest -m integration -q     # integration lane, 8 tests, containers must be up
```

### Hermeticity guard

`tests/conftest.py::_forbid_real_db_and_redis_sockets` is an autouse fixture that patches the
real connection-establishment entry points — `src.core.database._get_pool` and
`src.core.redis._AsyncRedisProxy._get_conn` — to `pytest.fail()` immediately instead of opening a
socket, for every test **outside** `-m integration`. `fake_db`/`fake_redis` (below) patch the
*higher-level* methods (`db.cursor`/`commit`/`rollback`/`release_current`, `r.get`/`set`/...), so
a test that correctly uses them never reaches the guard; a test that forgets to (or wires a fake
badly) gets a fast, explicit failure at the connection point instead of silently falling through
to a real socket. `pytest.fail()` raises `_pytest.outcomes.Failed`, which subclasses
`BaseException` (not `Exception`) specifically so broad `except Exception:` blocks in application
code (e.g. `get_economy_rate_history`'s DB try/except) cannot swallow it. The guard is a no-op
under `@pytest.mark.integration` (checks `request.node.get_closest_marker("integration")`), where
real connections are the point.

This guard is what caught the bug below and is meant to catch the next one automatically instead
of manifesting as a multi-minute slowdown days later.

### Known bug fixed: `test_get_price_history_stock` was not hermetic

`tests/test_services_ticker.py::test_get_price_history_stock` monkeypatched
`price_module.get_price_history` but not `ticker_module.get_currency` (the sibling test
`test_get_price_history_currency`, three tests above it, does both). `ticker.get_price_history`
calls the real `get_currency()` before reaching the stock branch; unstubbed, that call falls
through to `finance_service.get_quotes()`, which hits real Redis (`fx:quotes` cache check) and,
on a miss, real Postgres (provider refresh + persistence) — invisible when the local dev
containers happen to be up (fast success, ~0.95s — already the single slowest test in the suite),
catastrophic when they are down (real connection attempts retry/timeout instead of failing
instantly, ~55s for this one test alone). Fixed by stubbing `ticker_module.get_currency` in that
test, matching its neighbor. The hermeticity guard above now fails this class of bug in
milliseconds instead of letting it degrade into a multi-second hang.

### Measured timings (2026-08-27, this machine)

| Scenario | Before fix | After fix |
|---|---|---|
| `python -m pytest -q`, containers **up** | 553 passed in 4.24s | 553 passed in 3.28s |
| `python -m pytest -q`, containers **down** | 553 passed in **58.13s** | 553 passed in **3.33s** |
| `python -m pytest -m integration -q`, containers up | 8 passed in 1.67s | 8 passed in 1.67s (unchanged) |
| `pytest --collect-only -q` | 553/561 collected in ~1.45s (unchanged; collection was never the bottleneck — no heavy top-level imports like `yfinance`/`pandas` showed up under `python -X importtime`, they're already lazily imported inside the provider modules) | |

Before the fix, `--durations=25` with containers down showed a single outlier —
`54.78s call tests/test_services_ticker.py::test_get_price_history_stock` — against a suite
otherwise identical (same 25 next-slowest entries, all <0.25s) to the containers-up run. That one
test accounted for effectively all of the 54s regression.

**`pytest-xdist` was evaluated and rejected.** Installed temporarily and measured against the
hermetic lane (24 cores available): `-n auto` 7.56s, `-n 4` 4.04s, `-n 2` 4.05s, all *slower* than
the 3.28s serial baseline — worker startup/IPC overhead dominates a suite this small and fast, as
the task brief anticipated. Uninstalled afterward; `requirements.txt` is untouched.

### Why `tests/test_llm_integration.py` is the one non-hermetic file

REFACTOR_PLAN.md Step 6 added this file deliberately non-hermetic, against **real** local
Postgres/Redis, because three real bugs surfaced during the LLM provider refactor that a mocked
suite structurally cannot see:

1. A synchronous bridge (`asyncio.run` in a worker thread) touching the loop-bound
   `AsyncConnectionPool` / async Redis client from a foreign event loop — `fake_db`/`fake_redis`
   carry no loop affinity, so this class of bug is invisible to them.
2. `llm_settings.provider` → `llm_providers` foreign key requiring a row even for keyless
   providers — only a real Postgres FK constraint enforces this.
3. `opencode-zen` serving `/models` without auth but rejecting `/chat/completions` with 401 —
   not covered by this test layer (no real network calls here), but the distinction is
   documented in `src/llm/providers.py`.

If the containers aren't reachable, the tests **skip** cleanly (a short, independent connection
probe in the `_integration_target` fixture) rather than failing. Every test cleans up its own
rows in a `_cleanup` fixture (`llm_settings` before `llm_providers`, matching the FK order;
`token_usage` rows are tagged with a unique `purpose` value so pre-existing data is never
touched) — running the file twice in a row passes both times. A safety guard skips the whole
file unless `POSTGRES_HOST`/`REDIS_HOST` resolve to `localhost`/`127.0.0.1` (override with
`FLORENCE_INTEGRATION_ALLOW_REMOTE=1`) — this layer must never reach production.
