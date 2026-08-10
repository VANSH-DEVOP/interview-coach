# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

InterviewPilot AI — resume upload → AI-generated mock interview → evaluation report. Monorepo at `interview-coach/`: `backend/` and `frontend/`. No UI library beyond hand-rolled shadcn-style primitives.

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
```

### Frontend
```bash
cd frontend
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1 npm run dev
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

Flow: `ResumeService.upload()` parses PDF/DOCX (`resume_parser.py`) → sets `Resume.parsed_text` + `status` → chunks, saves the chunks as rows, then embeds them into ChromaDB (non-blocking; failure is logged, upload still succeeds).

### Chunking and the chunk table

`ResumeChunker` splits on the resume's own **section headings** (`EXPERIENCE`, `EDUCATION`, …), then packs paragraphs to ~800 chars within a section. Two rules that look like details and are not:

- **Split at blank lines, never at line breaks.** Resume text arrives from a PDF hard-wrapped, so a physical line ending is a typographic accident — splitting on one cut a sentence about gRPC latency in half and the query about latency then matched neither piece.
- **`retrieval_text()` prepends the section heading**, so the third chunk of a long EXPERIENCE section still says what it is. `content` stays clean in the database for reading and for the keyword index. It lives in `app/models/resume_chunk.py` and is shared by the chunker and the model **because rank fusion matches candidates by chunk text** — if the two formattings differed by a newline, no chunk would ever be recognised as found by both halves and `agreed` would sit at zero for ever.

Chunks are rows in `resume_chunks` (`ResumeChunkRepository`), which is what makes re-indexing possible without re-embedding — at 20 provider requests/day, re-embedding a resume to rebuild an index is not a thing you can casually do. `embedded_at` NULL means the text is stored and the retriever cannot see it: the durable version of the produced-vs-embedded gap.

The ordering in `ResumeService._index` is deliberate: **save chunks, then embed, then mark embedded.** A provider failure leaves rows with `embedded_at` NULL rather than leaving nothing, so which parts of the resume are missing from the index survives the failure.

`replace_for_resume` deletes then inserts rather than upserting by ordinal — re-chunking can produce *fewer* pieces, and updating in place would leave the previous run's tail behind as rows matching no part of the document. `InterviewService.create()` generates questions (RAG-retrieved resume context when available), `submit_answer()` may append a `follow_up` question linked by `parent_question_id`, `complete()` runs the evaluator and writes a `COMPLETED` `EvaluationReport`. `reevaluate()` regenerates a report for an already-completed session.

ChromaDB persists to `CHROMA_PATH` (default `/var/lib/interviewpilot/chroma`, backed by the `chroma_data` Docker volume). `get_rag_service()` is `@lru_cache`d, so tests that vary settings must call `get_rag_service.cache_clear()`. Outside Docker that default path is usually unwritable — RAG then logs a warning and disables itself, so set `CHROMA_PATH` to something local when running the backend directly.

### Retrieval observability

Retrieval degrades more quietly than anything else here: off, empty, or broken, generation falls back to `resume_text[:4000]` and produces plausible questions anyway — the interview works, it is just no longer personalised. `degradation.py` does **not** count these; none of them are provider failures.

`app/services/ai/retrieval_metrics.py` is where that becomes visible, reported in `/health`'s `rag` block:

- `enabled` / `disabled_reason` — recorded by `get_rag_service()`. Usually `no_api_key`, or an `init_failed` when `CHROMA_PATH` is unwritable, which is the normal case outside Docker.
- `full_text_fallbacks` climbing while `retrievals` stays flat → retrieval is never being reached. Counted in `gemini.py::_resume_context`, the only place that sees every route to a truncated-resume prompt.
- `hits` climbing with `last_best_distance` near 1.0 → retrieval is reached and returning junk. A hit is not a success.
- Indexing records produced-vs-embedded, in a `finally`, so a run that chunked 30 and embedded 4 is visible — that resume answers from a partial index for ever after.

Any new structured field goes through `extra=`; `JsonFormatter` emits every non-standard record attribute, so nothing needs registering.

**The retrieval benchmark** is `tests/api/test_retrieval_eval.py`: one fixture resume, twelve queries, real (in-memory) Chroma, deterministic lexical embeddings. Two tiers — lexical queries pin the machinery at 1.00/1.00, semantic ones sit at recall@3 0.50 / precision@1 0.33. Baselines are floors: raise them when a change earns it, and **never lower one to make a suite pass** — if a change drops a score, either the change or the instrument is wrong, and both have happened here.

Three traps in that harness, all hit already:

- The hashing trick needs enough dimensions that collisions don't decide rankings — at 512 for a 229-token vocabulary, a 46-char chunk beat the paragraph that answered the query.
- Comparisons between pipeline versions must hold the embedder fixed, or they measure collision luck rather than retrieval.
- **Measurements without the distance cutoff are not reproducible.** A paraphrased query leaves several chunks at cosine distance exactly 1.0, and which of those ties lands in the top 3 varies between processes — the same code scored 0.50 or 0.67 run to run. Any new probe added to the benchmark must apply `RAG_MAX_DISTANCE` the way the pipeline does.

Deeper docs: `backend/AI_INTEGRATION.md`, `backend/RAG_IMPLEMENTATION.md`. The six-part production-RAG plan lives in `goals.md`.

## Evaluation queue

Completing an interview writes a `PENDING` report and hands the scoring off; the client polls. Two runners, chosen per request by whether `app.state.arq_pool` exists (`app/services/job_queue.py`):

- **arq over Redis** (`REDIS_URL` set) — durable, retried with backoff. Needs the separate `worker` service (`arq app.worker.WorkerSettings`), which shares the image and the code and serves no HTTP.
- **`BackgroundTasks` in-process** — no Redis, no durability. Keeps `uvicorn app.main:app` and the test suite working with nothing else running; a fallback here is counted and surfaced at `/health` alongside the AI ones.

The two runners want opposite things from a failure, which is why `evaluation_worker.py` has two entry points: `evaluate` raises (arq needs that to retry; only the final attempt writes `FAILED`), `run_evaluation` swallows and marks `FAILED` itself (an exception in a BackgroundTask vanishes into the event loop).

Reports orphaned by a restart are recovered on two different paths, and they must not be swapped:

- **No queue** → `recover_stale_reports()` on API startup flips everything `PENDING`/`GENERATING` to `FAILED`. Gated in the lifespan to the no-Redis case — with a queue those rows have live jobs behind them and failing them on boot would kill every evaluation in flight on every deploy.
- **Queue** → `reconcile_stale_reports()` on an arq cron every 10 minutes (`RECONCILE_EVERY_MINUTES`) re-queues instead of failing, since the work is recoverable. This is what catches a *Redis* restart, which drops the whole queue. Staleness is measured from `updated_at`, so a merely slow job is left alone; `EVALUATION_STALE_AFTER_SECONDS` must stay above `EVALUATION_MAX_TRIES × EVALUATION_JOB_TIMEOUT_SECONDS` or the sweep double-evaluates live work. Past `EVALUATION_STALE_GIVE_UP_SECONDS` a report is failed rather than retried forever. The cron is `unique=True`: with several worker replicas only one sweeps per tick.

The arq function name is matched as a *string* (`EVALUATE_SESSION` in `job_queue.py` vs the callable in `worker.py`). Renaming one side alone gives jobs that enqueue cleanly and never run — `tests/test_worker.py` guards it.

### Scheduled work

`WorkerSettings.cron_jobs` is where anything periodic goes — it is the only process already running on a clock. Two jobs today:

- `reconcile_reports` — every 10 minutes and at startup (above).
- `prune_tokens` — hourly at `:07`, deliberately off the reconciliation tick. `refresh_tokens` gains a row per login and rotation, `one_time_tokens` one per reset/verification email, and nothing in the request path can clean them up. `app/services/token_pruning.py` deletes only *expired* rows: a revoked-but-unexpired refresh token is what makes logout work, and a consumed one-time token is what makes a replayed reset link fail, so neither may be deleted early.

Both swallow their exceptions and return counts — a cron job that raises stops until the worker restarts, and neither task is urgent enough to be worth that.

### Worker liveness

The worker writes a heartbeat to Redis every `HEALTH_CHECK_INTERVAL_SECONDS` (30; arq's default is an hour, useless for this) with a TTL just past it, and deletes it on clean shutdown — so *presence of the key is the whole signal* and nothing needs a clock. Read two ways: `GET /health`'s `worker` block (`job_queue.worker_health`), and the `worker` container's own healthcheck, `arq --check app.worker.WorkerSettings`.

A missing worker flips `/health` `status` to `degraded`, unlike an AI or queue fallback. Nothing downstream covers for it: the API keeps accepting interviews and every report sits `PENDING` until a human notices. `alive` is `null`, not `false`, when the heartbeat could not be read at all (no pool, Redis unreachable) — that is already reported as a queue fallback, and the worker may be perfectly fine.

### Hybrid retrieval

A request retrieves through `HybridRetriever` (`app/services/ai/retrieval.py`), built per request in `deps.py` because its two halves have different lifetimes: the keyword half is a repository on the request's session, the dense half is the process-wide `RAGService`.

- **Dense** — Chroma, generalises, bad at exact tokens (`gRPC`, a version number) that get averaged away inside a chunk.
- **Sparse** — Postgres full-text over `resume_chunks.search_vector`, a generated column so nothing in the application maintains it. Query terms are ORed, not ANDed: `plainto_tsquery` would require a chunk containing *every* word of "skills and experience relevant to Senior Backend Engineer", which is no chunk. Terms are tokenised in Python and bound as a parameter — `to_tsquery` has a syntax and a candidate's answer will eventually contain a stray `&`.
- **Fusion** — Reciprocal Rank Fusion (`fuse`), not a weighted score. Cosine distance and `ts_rank` have different ranges and neither is calibrated, so any weighted sum invents an exchange rate; RRF keeps only the orderings, which is what each half is reliable about.

`RAG_MAX_DISTANCE` (default 1.0) drops dense results **strictly** beyond the cutoff — 1.0 is exactly orthogonal, so `<=` would keep precisely the chunks the cutoff exists to remove. Either half failing degrades to the other; both empty returns `""` and the generator falls back to raw resume text, counted in `full_text_fallbacks`.

`retrieve_scored()` returns ranks and which half found each candidate; `retrieve_context()` is the string wrapper. Sessionless callers (the evaluation worker) can still use `RAGService` directly and get dense-only retrieval.

## Rate limiting

`app/core/rate_limit.py` is a pure mechanism — fixed-window counters, no knowledge of routes or users. The wiring (`limit_by_ip`, `limit_by_user`) lives in `app/api/deps.py` with the rest of the DI, and **must stay there**: that module deliberately has no `from __future__ import annotations`, because FastAPI has to resolve `Annotated[User, Depends(...)]` at runtime. When these dependencies lived in `core/`, FastAPI silently reinterpreted `user` as a *query parameter* and every AI route answered 422.

`enforce()` is async and takes the store: the arq pool (`rate_limit_store()` in `deps.py`) when Redis is configured, `None` otherwise.

- **Redis** — one counter per deployment. The limits guard *shared* things (the Gemini account's daily quota, guesses against one account), so per-replica counters would mean N× the intended ceiling. Counting and expiry happen in one Lua script: `INCR` then `EXPIRE` from the client is not atomic, and a process dying between them leaves a counter with no TTL that locks its subject out permanently.
- **In-process** — correct for a single process, and the fallback when Redis fails. A Redis blip must not become either a total auth outage (fail closed) or unbounded credential stuffing (fail open), so the counters degrade to this process, get counted in `rate_limit.snapshot()`, and show up in `/health`'s `rate_limit` block. Keys are namespaced `ratelimit:{scope}:{key}`; arq owns `arq:*` in the same database.

## Storage

`app/services/storage/` mirrors the same pattern: `StorageService` ABC, `LocalStorageService` impl, `get_storage_service()` factory keyed on `STORAGE_BACKEND`. Blobs live outside the code tree (`STORAGE_LOCAL_PATH`, a named Docker volume). Uploads use opaque keys `resumes/{user_id}/{uuid}.pdf`; the client filename is metadata only. Swapping to S3/R2 should touch this package only.

## Tests

`pytest` splits into three kinds, all collected together:

- **Unit tests with fakes** — `test_evaluator.py`, `test_question_generator.py`, `test_interview_flow.py`, `test_report_service.py`, `test_resume_service.py`, `test_storage.py`, `test_rate_limit.py`, `test_degradation.py`. No network, no database.
- **API tests against a real Postgres** — `tests/api/`. The `api` fixture builds the schema by running the **actual migrations**, wraps each test in a transaction that is rolled back (`join_transaction_mode="create_savepoint"`, so the app's own `commit()` still works), and forces `GEMINI_API_KEY=None` plus rate limiting off. They **skip** when Postgres is unreachable; `REQUIRE_TEST_DATABASE=1` makes that a failure instead, which is what CI sets.
- **Integration tests against a real Redis** — `tests/api/test_queue_integration.py`, `tests/api/test_rate_limit_redis.py`, via the `redis_pool` fixture (db 15, flushed). Same bargain as Postgres: skip when unreachable, `REQUIRE_TEST_REDIS=1` in CI turns that into a failure.
- **Script-style probes** — `test_gemini_integration.py`, `test_rag_pipeline.py`. They print rather than assert and self-skip without a key.

Prefer an API test for anything touching ownership: the service fakes implement `get_owned` themselves, so they prove the service *calls* it, not that the SQL filters by user.

Frontend: `npm test` (vitest + jsdom + React Testing Library). `tsc --noEmit` covers test files too. `npm run lint` is unconfigured (`next lint` prompts to set ESLint up) — CI runs typecheck, tests, and build instead.

## Frontend

- Route groups: `(auth)/` for login/register, `(app)/` for the authenticated shell (sidebar + mobile nav). Pages are client components calling the backend directly through `@/lib/api-client`.
- Tokens are stored in **cookies** (`ip_access_token`, `ip_refresh_token`) so `middleware.ts` can gate routes; `api-client.ts` does one transparent refresh-and-retry on a 401. `middleware.ts` is a UX guard only — real authorization is server-side.
- `src/types/index.ts` hand-mirrors the backend Pydantic schemas. Changing a response schema means editing both sides.
- `src/hooks/use-auth.ts` is the single entry point for login/register/logout and current-user state.

### Failure kinds

`ApiError.kind` (`auth` / `client` / `server` / `network`) is the distinction the UI runs on, and `isTransient` is the one that matters: **only an auth failure may empty the user or clear tokens.** A server that is down, a dropped connection, and a 500 all leave the session alone — otherwise an outage renders exactly like a logout and the user "fixes" it by signing in against a backend that cannot answer.

Consequences to preserve when touching `api-client.ts`:

- `fetch` rejecting (offline, DNS, connection refused, CORS) becomes a `NetworkError` with status 0. It subclasses `ApiError` so the `err instanceof ApiError ? err.message : "..."` pattern used across the pages shows a real message.
- `tryRefresh()` returns three outcomes, not a boolean. It clears tokens **only** on `rejected` (the server refused the token); an unreachable or 500-ing refresh endpoint returns `unavailable` and the session survives. Collapsing those two back into one is how a momentary network drop destroys a live session.
- A 401 that could not be renewed surfaces as the transient error, not as `unauthorized`.
- `useAuth().connectionError` carries the transient case; `(app)/layout.tsx` renders `ConnectionBanner` from it and passes its own `reload` as the retry.

## Note on the README

`README.md` predates the AI work and still describes question generation/evaluation as unimplemented "seams". The seams are filled: Gemini generation, evaluation, and RAG all ship. The layering rules and setup instructions in it are still accurate.
