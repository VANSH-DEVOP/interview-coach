# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

InterviewPilot AI — resume upload → AI-generated mock interview → evaluation report. Monorepo at `interview-coach/`: `backend/` (FastAPI, Python 3.12, async SQLAlchemy, PostgreSQL 16) and `frontend/` (Next.js 15 App Router, React 19, Tailwind, no UI library beyond hand-rolled shadcn-style primitives).

## Commands

### Docker (full stack)
```bash
cp .env.example .env          # set JWT_SECRET_KEY: openssl rand -hex 32
docker compose up --build     # frontend :3000, backend :8000, postgres :5434
docker compose restart backend  # required after changing GEMINI_API_KEY
```
Alembic migrations run automatically in the backend container's CMD.

### Backend
```bash
cd backend
source .venv/bin/activate
pip install -e ".[dev]"       # do this after pulling; deps drift (chromadb was added late)
alembic upgrade head
uvicorn app.main:app --reload

pytest                                       # whole suite (asyncio_mode = auto)
pytest tests/test_evaluator.py -v            # one file
pytest tests/test_evaluator.py::test_name -v # one test
ruff check .
mypy app
```

### Frontend
```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1 npm run dev
npm run typecheck   # tsc --noEmit
npm run build
npm run lint
```

## Environment gotchas

- `Settings` (`app/core/config.py`) loads `.env` **relative to the process CWD**. The only `.env` lives at the repo root, and there is no `backend/.env` — running `uvicorn` from `backend/` silently uses code defaults (no Gemini key, Postgres on 5432). Export vars, or create `backend/.env`, when running the backend outside Docker.
- Postgres port differs per path: docker-compose publishes **5434**, `.env.example` says **5433**, the code default is **5432**. Check which one your DB is actually on.
- Every environment variable enters the app through `app/core/config.py`. No other module reads `os.environ`.

## Backend architecture

Strict layering, enforced by convention — respect it when adding features:

```
router (app/api/v1/*)  → no DB access, no construction logic
  ↓ Depends(...) from app/api/deps.py builds session → repository → service
service (app/services/*) → business logic; depends on abstractions only
  ↓
repository (app/repositories/*) → the ONLY place SQLAlchemy queries are built
```

- Adding a resource: model → schema → repository → service → router → register in `app/api/v1/router.py`. New DI wiring goes in `app/api/deps.py`, never inline in a route.
- Services raise domain exceptions from `app/core/exceptions.py` (`NotFoundError`, `ConflictError`, …). Routes must not raise `HTTPException` for domain errors — `app/middleware/error_handler.py` translates them into the envelope `{"error": {"code", "message", "details"}}`, which `frontend/src/lib/api-client.ts` parses into `ApiError`.
- One session per request, committed on success by `get_session` (`app/db/session.py`). Repositories `flush()`; they never commit.
- Ownership is enforced in repositories via `get_owned(entity_id, user_id)` — always use those, not `get()`, for user-scoped resources.
- New ORM models must be imported in `app/models/__init__.py` or Alembic autogenerate won't see them. Constraint names come from the naming convention in `app/db/base.py`, so migrations stay deterministic.
- Timestamps: columns are `TIMESTAMP WITHOUT TIME ZONE`. Use the `_utcnow()` helper pattern (UTC then `.replace(tzinfo=None)`) — passing an aware datetime makes asyncpg raise "offset-naive vs offset-aware". `test_interview_flow.py` guards this.
- Schema change → edit models → `alembic revision --autogenerate -m "..."` → review the generated file.

## AI pipeline (`app/services/ai/`)

Everything is behind an ABC with a factory, and **every AI path degrades gracefully to a deterministic local implementation** when `GEMINI_API_KEY` is unset or the provider fails. Preserve this property in any change — an interview must always be completable and always produce a populated report.

| Seam | Real impl | Fallback |
|---|---|---|
| `QuestionGenerator` (`base.py`, factory `get_question_generator`) | `GeminiQuestionGenerator` (`gemini.py`), 5 tailored questions + adaptive follow-ups | `StaticQuestionGenerator`: 3 fixed questions, `follow_up()` → `None` |
| `Evaluator` (`evaluator.py`, factory `get_evaluator`) | `GeminiEvaluator` | `HeuristicEvaluator`: score from answer coverage + avg word depth |
| RAG (`deps.py::get_rag_service`) | `RAGService` = `EmbeddingService` (`GEMINI_EMBEDDING_MODEL`, default `models/gemini-embedding-001`, 3072-dim) + `ChromaVectorStore` | returns `None`; generator falls back to `resume_text[:4000]` |

Google retires model IDs, and a retired ID is a 404 that the fallback layer hides — this has already happened twice here. Before changing `GEMINI_MODEL` or `GEMINI_EMBEDDING_MODEL`, check the ID against `GET https://generativelanguage.googleapis.com/v1beta/models` with the key in use.

Degradations are recorded by `app/services/ai/degradation.py` and reported in the `ai` block of `GET /health` (`fallbacks`, `last_operation`, `last_error`). **Any new `except` that silently swaps in a fallback must call `record_fallback()`** — that counter is the only thing standing between a dead provider and a system that looks fine. A non-zero `fallbacks` count in a test environment means the AI path is broken, not that the fallback is working.

`FallbackQuestionGenerator` / `FallbackEvaluator` wrap the primary and catch *any* exception. `GeminiClient` (`gemini_client.py`) is a hand-written httpx call to the REST API in JSON mode (no SDK); it raises `GeminiError` on bad shape/status. Model output is deliberately parsed leniently (`_first`, `_as_str_list` in `evaluator.py`) because field names vary between responses.

Flow: `ResumeService.upload()` parses PDF/DOCX (`resume_parser.py`) → sets `Resume.parsed_text` + `status` → indexes chunks into ChromaDB (non-blocking; failure is logged, upload still succeeds). `InterviewService.create()` generates questions (RAG-retrieved resume context when available), `submit_answer()` may append a `follow_up` question linked by `parent_question_id`, `complete()` runs the evaluator and writes a `COMPLETED` `EvaluationReport`. `reevaluate()` regenerates a report for an already-completed session.

ChromaDB persists to `CHROMA_PATH` (default `/var/lib/interviewpilot/chroma`, backed by the `chroma_data` Docker volume). `get_rag_service()` is `@lru_cache`d, so tests that vary settings must call `get_rag_service.cache_clear()`. Outside Docker that default path is usually unwritable — RAG then logs a warning and disables itself, so set `CHROMA_PATH` to something local when running the backend directly.

Deeper docs: `backend/AI_INTEGRATION.md`, `backend/RAG_IMPLEMENTATION.md`.

## Evaluation queue

Completing an interview writes a `PENDING` report and hands the scoring off; the client polls. Two runners, chosen per request by whether `app.state.arq_pool` exists (`app/services/job_queue.py`):

- **arq over Redis** (`REDIS_URL` set) — durable, retried with backoff. Needs the separate `worker` service (`arq app.worker.WorkerSettings`), which shares the image and the code and serves no HTTP.
- **`BackgroundTasks` in-process** — no Redis, no durability. Keeps `uvicorn app.main:app` and the test suite working with nothing else running; a fallback here is counted and surfaced at `/health` alongside the AI ones.

The two runners want opposite things from a failure, which is why `evaluation_worker.py` has two entry points: `evaluate` raises (arq needs that to retry; only the final attempt writes `FAILED`), `run_evaluation` swallows and marks `FAILED` itself (an exception in a BackgroundTask vanishes into the event loop).

Reports orphaned by a restart are recovered on two different paths, and they must not be swapped:

- **No queue** → `recover_stale_reports()` on API startup flips everything `PENDING`/`GENERATING` to `FAILED`. Gated in the lifespan to the no-Redis case — with a queue those rows have live jobs behind them and failing them on boot would kill every evaluation in flight on every deploy.
- **Queue** → `reconcile_stale_reports()` on an arq cron every 10 minutes (`RECONCILE_EVERY_MINUTES`) re-queues instead of failing, since the work is recoverable. This is what catches a *Redis* restart, which drops the whole queue. Staleness is measured from `updated_at`, so a merely slow job is left alone; `EVALUATION_STALE_AFTER_SECONDS` must stay above `EVALUATION_MAX_TRIES × EVALUATION_JOB_TIMEOUT_SECONDS` or the sweep double-evaluates live work. Past `EVALUATION_STALE_GIVE_UP_SECONDS` a report is failed rather than retried forever. The cron is `unique=True`: with several worker replicas only one sweeps per tick.

The arq function name is matched as a *string* (`EVALUATE_SESSION` in `job_queue.py` vs the callable in `worker.py`). Renaming one side alone gives jobs that enqueue cleanly and never run — `tests/test_worker.py` guards it.

## Storage

`app/services/storage/` mirrors the same pattern: `StorageService` ABC, `LocalStorageService` impl, `get_storage_service()` factory keyed on `STORAGE_BACKEND`. Blobs live outside the code tree (`STORAGE_LOCAL_PATH`, a named Docker volume). Uploads use opaque keys `resumes/{user_id}/{uuid}.pdf`; the client filename is metadata only. Swapping to S3/R2 should touch this package only.

## Tests

`pytest` splits into three kinds, all collected together:

- **Unit tests with fakes** — `test_evaluator.py`, `test_question_generator.py`, `test_interview_flow.py`, `test_report_service.py`, `test_resume_service.py`, `test_storage.py`, `test_rate_limit.py`, `test_degradation.py`. No network, no database.
- **API tests against a real Postgres** — `tests/api/`. The `api` fixture builds the schema by running the **actual migrations**, wraps each test in a transaction that is rolled back (`join_transaction_mode="create_savepoint"`, so the app's own `commit()` still works), and forces `GEMINI_API_KEY=None` plus rate limiting off. They **skip** when Postgres is unreachable; `REQUIRE_TEST_DATABASE=1` makes that a failure instead, which is what CI sets.
- **Script-style probes** — `test_gemini_integration.py`, `test_rag_pipeline.py`. They print rather than assert and self-skip without a key.

Prefer an API test for anything touching ownership: the service fakes implement `get_owned` themselves, so they prove the service *calls* it, not that the SQL filters by user.

Frontend: `npm test` (vitest + jsdom + React Testing Library). `tsc --noEmit` covers test files too.

## Frontend

- Route groups: `(auth)/` for login/register, `(app)/` for the authenticated shell (sidebar + mobile nav). Pages are client components calling the backend directly through `@/lib/api-client`.
- Tokens are stored in **cookies** (`ip_access_token`, `ip_refresh_token`) so `middleware.ts` can gate routes; `api-client.ts` does one transparent refresh-and-retry on a 401. `middleware.ts` is a UX guard only — real authorization is server-side.
- `src/types/index.ts` hand-mirrors the backend Pydantic schemas. Changing a response schema means editing both sides.
- `src/hooks/use-auth.ts` is the single entry point for login/register/logout and current-user state.

## Note on the README

`README.md` predates the AI work and still describes question generation/evaluation as unimplemented "seams". The seams are filled: Gemini generation, evaluation, and RAG all ship. The layering rules and setup instructions in it are still accurate.
