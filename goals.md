# InterviewPilot AI — Goals & Backlog
Living checklist of remaining work. Derived from a full codebase read on **2026-07-26** (HEAD: `879a197 Feat: AI and RAG pipeline foundation`).
Legend: `[ ]` todo · `[~]` partially done · `[x]` done
Priority: **P0** blocking/broken · **P1** high value · **P2** nice to have
---
## Phase 0 — Broken / blocking (P0) — ✅ COMPLETE (2026-08-08)
Things that were wired but did not actually work. All closed; see commits
`df97c9a`, `4f660df`, `7e93bea`, `f4c1b8b`, `b637e70`, `7916278`, `e0e1ad6`.
- [x] **Test suite cannot import.** `chromadb` is in `pyproject.toml` but not installed in `.venv/` or `backend/.venv/`; `app/services/ai/vector_store.py:11` imports it at module scope and `app/api/deps.py` pulls it in transitively, so *every* test fails at conftest import. Fix by `pip install -e ".[dev]"`, and consider making the chroma import lazy so the app boots without the RAG extra.
  → **Done.** Deps installed; the chromadb import is now deferred into `ChromaVectorStore.__init__` and translated into `VectorStoreError`. 59 tests pass.
- [x] **ChromaDB index is ephemeral.** Persist directory is hardcoded to `/tmp/interviewpilot/chroma` (`app/api/deps.py:70`) and is not a Docker volume — every restart wipes the resume index and RAG silently degrades to `resume_text[:4000]`. Move to a configured path (`CHROMA_PATH` setting) + named volume in `docker-compose.yml`.
  → **Done.** `CHROMA_PATH` setting (default `/var/lib/interviewpilot/chroma`), `chroma_data` named volume, and the mount point is `mkdir`+`chown`ed in the Dockerfile so the non-root user can write to a freshly seeded volume.
- [x] **RAG services are rebuilt per request.** `get_rag_service()` constructs a new `EmbeddingService` and Chroma client on every call. Cache them (module-level `@lru_cache` or app state/lifespan).
  → **Done.** `@lru_cache(maxsize=1)`; tests that vary settings must call `get_rag_service.cache_clear()`.
- [x] **Every Gemini call 404s — the configured model is retired.** Observed **2026-07-27**: every AI-backed request logs `POST .../v1beta/models/gemini-1.5-flash:generateContent → 404 Not Found` and the route still returns `201`. `GEMINI_MODEL` defaults to `gemini-1.5-flash` (`app/core/config.py:54`, mirrored in `.env.example:37`), which no longer exists on the v1beta endpoint. **Nothing in the product has ever used real AI output** — every question, follow-up, and report currently comes from the deterministic fallbacks. Move to a current model and verify with `GET /v1beta/models` for the key in use. Check `EmbeddingService`'s `models/embedding-001` (`app/services/ai/embedding.py:25`) the same way — if it 404s too, RAG has been silently no-op as well.
  → **Done, and the suspicion was correct.** `gemini-flash-latest` is valid. `models/embedding-001` **is also retired** — absent from `GET /v1beta/models` and 404 on `embedContent`, so RAG had *never* produced a single embedding. Replaced with `models/gemini-embedding-001` (HTTP 200, **3072-dim**, up from 768) and made it configurable via `GEMINI_EMBEDDING_MODEL`. Verified end-to-end: index → retrieve returns the matching chunk.
- [x] **Silent AI failures.** ↑ The 404 above went unnoticed for exactly this reason: `FallbackQuestionGenerator` / `FallbackEvaluator` swallow every exception with no logging (`app/services/ai/base.py:92`, `:104`), so a dead model, a bad key, and "the AI is just generic" all look identical from the outside. Log at WARNING with the provider error before falling back, and surface a degraded-mode signal (health check field or a response flag) so this can't hide again.
  → **Done.** New `app/services/ai/degradation.py` records the four degradation points (`initial_questions`, `follow_up`, `evaluate`, `index_resume`) at WARNING with the exception attached, and `GET /health` now returns an `ai` block (`configured`, `fallbacks`, `last_operation`, `last_error`, `last_at`). Deliberately does **not** flip `status` — one historical 429 must not fail a liveness probe. Guarded by `tests/test_degradation.py`.
- [x] **Gemini reports render an empty summary.** `report-view.tsx:53` reads `detailed_feedback.summary`, but only `HeuristicEvaluator` sets it (`evaluator.py:149`); `GeminiEvaluator` writes only `recommendations` + `per_question` (`evaluator.py:228`). Add `summary` to the Gemini prompt + parse.
  → **Done.** Prompt asks for it, parser reads the usual key aliases, and a score-and-role line is synthesised if the model omits it. Verified against the live model.
---
## Phase 1 — Complete what's half-built (P1) — ✅ COMPLETE (2026-08-08)
Scaffolding exists; the feature does not.
### Interview flow
- [x] **Follow-ups have no resume context.** `interview_service.py:136` calls `follow_up(..., resume_text=None)` and never consults RAG. Pass the session's resume text and add a RAG retrieval keyed on the answer.
  → **Done** (`e1a985f`). `follow_up` seam widened with `resume_id`; retrieval is keyed on question+answer, not the role. Also fixed a latent `MissingGreenlet` crash in the follow-up branch (`session.questions` lazy-loaded on a session fetched without them) — never fired only because the model was 404ing. Replaced with `InterviewRepository.next_sequence_number`.
- [x] **Async evaluation.** (`7d87731`) `complete()` writes a PENDING report and returns; `app/services/evaluation_worker.py` runs it through GENERATING → COMPLETED/FAILED on its own session, never raising. `recover_stale_reports()` on startup flips work abandoned by a restart to FAILED so the UI shows a retry rather than an endless spinner. Frontend polls every 2s and stops once settled.
  - [ ] **Follow-up — durable queue (Redis + arq). Decided 2026-08-09, not yet implemented.**
    **Problem:** `BackgroundTasks` schedules an `asyncio` task inside the web-server process. Nothing records that the job exists except the report row sitting at `PENDING`/`GENERATING`, so a restart mid-evaluation — a deploy, a container reschedule, an OOM kill — drops the work silently. `recover_stale_reports()` limits the damage by flipping orphaned rows to `FAILED` at startup (a visible retry rather than an endless spinner), but the user's evaluation is genuinely lost and they have to notice and re-run it. Frequency scales with deploy rate, not usage.
    **Chosen approach:** Redis + `arq` — Redis-backed, async-native, matches the existing async stack. Rejected the Postgres-only alternative (have `recover_stale_reports()` re-run the work instead of failing it), which needs no new infrastructure but has no retry/backoff, no protection if the new process also dies, and needs an atomic `UPDATE ... RETURNING` claim to be safe across replicas — all of which arq gives for free.
    **Shape of the work:** `arq` dependency + `REDIS_URL` setting → `redis` service and a **new long-running `worker` service** in `docker-compose.yml` (the app has no such process today; same image, different command) → `app/worker.py` with `WorkerSettings` → redis pool created in the FastAPI lifespan, exposed via a `deps.py` dependency → the two `background.add_task(...)` call sites in `app/api/v1/interviews.py` (`complete_interview`, `reevaluate_interview`) become `enqueue_job` → `recover_stale_reports()` moves to worker startup and may become unnecessary entirely → `redis` service added to the CI workflow → test fixtures reworked, since the `api` fixture currently relies on `BackgroundTasks` running synchronously under `ASGITransport` to observe the report reaching `COMPLETED`.
    **Why it's cheap to swap:** `run_evaluation(session_id, user_id)` takes no request-scoped state, so the status machine and the worker function itself are unchanged.
- [x] **Abandon / delete a session.** (`ac2552f`) `POST /interviews/{id}/abandon` keeps the transcript; `DELETE /interviews/{id}` cascades to questions, answers, and the report (verified against Postgres). `SessionStatus.CREATED` is still unreachable — sessions are created IN_PROGRESS, which is arguably correct; left alone deliberately.
  ~~`SessionStatus.CREATED` and `ABANDONED` are unreachable~~ — sessions are created as `IN_PROGRESS` and there is no endpoint to abandon or delete one, even though `complete()` already handles the abandoned case (`interview_service.py:160`).
- [x] **Answer timing.** (`836c0b8`) Per-question timer in the UI, sent with the answer, shown on answered questions. Verified round-trip.
  - [ ] **Follow-up:** the evaluator still doesn't see timing. Pacing feedback ("4 minutes on a 30-second question") needs `duration` in `QAPair` and the prompt.
- [x] **Question controls.** (`56e6924`) Skip (persisted via migration 0003, withdrawn by answering), re-answer via `PUT /interviews/{id}/answers` (deletes and regenerates the now-stale follow-up), and regenerate-questions (refused once anything is answered). Found a real bug: the regenerate response returned the *old* question ids because SQLAlchemy will not overwrite an already-loaded collection on re-query.
- [x] **Interview configuration.** (`1e09557`, `2a84fae`) `interview_type` / `difficulty` / `question_count` on `InterviewCreate`, persisted via migration 0002, shaping the prompt, with UI selects. Migration verified up→down→up against Postgres with no autogenerate drift. System-design questions are stored as `question_type="technical"` — widening that enum was not needed.
  - Static fallback pool grew 3 → 10 distinct questions so any allowed count is honoured without repeats; its default output changed 3 → 5.
### Resumes
- [x] **Re-parse / re-index endpoint.** (`ddd07b4`) `POST /resumes/{id}/reprocess` re-reads the blob, re-parses, and rebuilds the index (dropping the old one first — chunk ids are positional). Adds `tests/test_resume_service.py`; the service previously had no tests at all.
- [x] **Resume preview** (`fe11933`) `GET /resumes/{id}/preview` returns the parsed text with word/character counts — kept off `ResumeRead` since it can run to thousands of characters. Makes a scanned PDF that extracts to nothing visible, which previously looked identical to a working resume.
### Reports & progress tracking
- [x] **Progress over time.** (`e5c23ad`) `GET /reports/progress` + an inline-SVG trend chart on the dashboard. Unscored reports are excluded rather than plotted as zero; `improvement` compares half-means and stays null below 4 sessions.
- [x] **Per-question scores.** (`63df2f9`) 0-10 per entry, clamped, alias-tolerant, `"8.5/10"` parsed; unparseable stays null rather than becoming 0. Heuristic scores on the same 80-word scale. Colour-coded badges in the report.
- [x] **Skill/category breakdown** (`a43a99f`) `recurring_strengths` / `recurring_weaknesses` on `/reports/progress`, grouped by keyword taxonomy in `app/services/skill_themes.py`. Deterministic and testable by design — asking the model to categorise its own output would cost a call per report and vary per run. Unmatched feedback lands in "Other" rather than being dropped.
- [x] **Report export and sharing.** (`f605289`) `GET /reports/{id}/export` returns Markdown as a download; a print stylesheet + "Print / PDF" button covers PDF via the browser. Server-side PDF was rejected deliberately: WeasyPrint needs cairo/pango in the slim image, ReportLab means maintaining a hand-built layout. Revisit only if PDFs must be generated without a browser.
### Frontend
- [x] **Pagination controls.** (`6896e83`) Shared `Pagination` component wired into interviews and reports. Verified: 7 records at size=3 paginate with zero overlap.
- [x] **Expose `/interviews/{id}/reevaluate`** (`6896e83`) — "Re-evaluate" on the session report page. Updates the report in place (same id).
---
## Phase 2 — Auth & account completeness (P1)
- [ ] **Logout endpoint.** Logout is client-side cookie clearing only (`use-auth.ts:63`); refresh tokens stay valid until expiry.
- [ ] **Refresh-token rotation + revocation list.** Called out in the README's hardening backlog.
- [ ] **Change password.** The profile page only edits `full_name` (`user_service.py:12`).
- [ ] **Forgot / reset password** (needs an email transport — none exists yet).
- [ ] **Email verification.**
- [ ] **Account deletion** (cascade resumes, sessions, reports, blobs, and vector index).
---
## Phase 3 — Production readiness (P1/P2)
### Testing (P1) — ✅ COMPLETE (2026-08-08)
- [x] **API-level tests.** (`9d61f64`) 63 tests across auth / interviews / resumes / reports. The ownership cases needed a real database: the service fakes implement `get_owned` themselves, so they prove the service *calls* it, not that the SQL filters by user.
- [x] **Test database fixture.** (`33725c5`) `api` fixture on a real Postgres. Schema built by running the **actual migrations**, not `create_all`. Per-test outer transaction + `join_transaction_mode="create_savepoint"`, so the app's own `commit()` works and nothing is durable. Skips without Postgres; `REQUIRE_TEST_DATABASE=1` (set in CI) turns that skip into a failure.
- [x] **Frontend tests.** (`908a24d`) vitest 4 + jsdom + RTL. 26 tests: `api-client` refresh-and-retry in depth, plus the `Pagination` component. The interview page state machine is still uncovered.
- [x] **E2E smoke test.** (`b82e1c4`) Full journey plus the abandon path.
### CI/CD (P1) — ✅ COMPLETE (2026-08-08)
- [x] **CI pipeline.** (`df3e09c`, `47965b9`, `e7115c4`) `.github/workflows/ci.yml`. Backend job with a Postgres 16 service: ruff, mypy, migrate, drift check, downgrade/upgrade round trip, pytest. Frontend job: typecheck, vitest, build. **Green: 203 backend + 26 frontend, zero skips.**
- [x] **Migration drift check.** `backend/scripts/check_migration_drift.py` compares `Base.metadata` against the migrated database via alembic's autogenerate API. (`alembic revision --autogenerate` has no output-dir flag, so the generate-and-grep approach does not work.) Verified in both directions — it catches an added column and exits 1.
  - **Decision:** GitHub Actions is the only CI target by design — the `gitlab` remote exists but a GitLab pipeline is explicitly out of scope, not a gap.
- [ ] **Follow-up:** no coverage measurement or threshold yet.
- [ ] **Follow-up:** no dependency scanning. `npm audit` reports pre-existing high findings in `next`, `postcss`, and `sharp`.
### Security (P1)
- [x] **Rate limiting.** (`66252e9`) **Pulled forward into Phase 1** — the free tier turned out to be **20 requests/day**, not 60/min, and live verification exhausted it. `app/core/rate_limit.py` (fixed-window, no new dependency); auth keyed by IP, AI/upload keyed by user; 429 in the standard envelope with `Retry-After`. Per-process and burst-tolerant at window boundaries — both documented in the module.
  - [ ] **Follow-up:** the defaults (20 AI req/user/hour) sit *above* the account's 20/day ceiling, so they bound one user's abuse but not account exhaustion. Lower them, or move off the free tier.
  - [ ] **Follow-up:** move counters to Redis before running more than one worker.
- [ ] **httpOnly cookie BFF for tokens.** Tokens currently live in JS-readable cookies (`api-client.ts:35`), flagged as MVP in the code itself.
- [x] **The Gemini API key is written to the logs in cleartext.** `GeminiClient` passes the key as a `?key=` query param, and httpx logs the full URL at INFO — so every AI call prints the key (seen in the backend container logs on 2026-07-27). Send it as the `x-goog-api-key` header instead, and/or set `logging.getLogger("httpx").setLevel(WARNING)`. Rotate the key that has already been logged.
  → **Done** in both `GeminiClient` and `EmbeddingService`; httpx/httpcore pinned to WARNING. Verified: at root DEBUG the key no longer appears in captured logs.
  → ⚠️ **STILL OUTSTANDING: rotate the key in `.env`.** It has already been written to logs and must be replaced in Google AI Studio.
- [ ] **CSP + security headers.**
- [ ] **Per-user quotas** on interview creation / resume uploads.
### Observability (P2)
- [ ] Metrics + tracing (request logging is all there is: `app/middleware/request_logging.py`).
- [ ] Error reporting (Sentry or equivalent).
- [ ] AI-call telemetry: latency, token spend, **fallback rate** — a non-zero fallback rate is the alert that would have caught the `gemini-1.5-flash` 404 on day one.
- [x] **Log level is too verbose.** Each AI call emits ~15 `httpcore` DEBUG lines (connect/TLS/request/response teardown) that bury the one line that matters. Pin third-party loggers (`httpcore`, `httpx`) to WARNING. → **Done** in `app/core/logging.py`.
### Config hygiene (P2)
- [ ] **Postgres port is inconsistent** across docker-compose (**5434**), `.env.example` (**5433**), and the code default (**5432**).
- [ ] **No `backend/.env`.** `Settings` loads `.env` relative to CWD, so running uvicorn from `backend/` silently uses code defaults (no Gemini key) — surprising, and it looks identical to a broken API key.
---
## Phase 4 — Roadmap (P2)
- [ ] **LangGraph orchestration** — multi-step chains (extract skills → generate → refine). Not even a dependency yet; the `QuestionGenerator` seam is ready for it.
- [ ] **S3 / R2 / MinIO storage.** `STORAGE_BACKEND` is typed `Literal["local"]` (`config.py:65`); the abstraction is ready, the impl isn't.
- [ ] **Streaming responses** for question generation and evaluation.
- [ ] **Multi-provider models** (Claude, GPT) behind the existing seams.
- [ ] **Embedding / question caching** for repeat roles and identical resumes.
- [ ] **Semantic chunking** to replace the fixed-size chunker (`rag.py`).
- [ ] **Voice interviews** (speech-to-text answers) — would give `duration_seconds` a real purpose.
---
## Suggested order
1. Phase 0 in full — RAG and the test suite are actively broken.
2. Follow-up resume context + async evaluation (the two biggest AI-quality wins).
3. CI with real API tests, plus a test database.
4. Auth completeness (logout, rotation, password change).
5. Progress-tracking dashboard — the largest gap between the README's promise and reality.
---
## Doc maintenance
- [ ] **`README.md` is stale** — it still describes question generation, evaluation, and RAG as unfilled "seams". They ship. Its layering rules and setup instructions are still accurate.
- [ ] Keep `CLAUDE.md`, `backend/AI_INTEGRATION.md`, and `backend/RAG_IMPLEMENTATION.md` in sync as these land.
___
## Discovery 
- [ ] **User-Data (PII) Masking** the pipeline doesn't handle the user data masking before sending it to the LLM it's a security issue.
- [ ] **RAG implementation** it's too simple , like I want production level rag implementaion (hybrid-search , langchain, langgraph and all that neccessary items ) ideal pipeline:
User
  │
  ▼
API Request
  │
  ▼
RequestTimer starts
  │
  ▼
LLM generates answer
  │
  ▼
Cache checked
  │
  ▼
Logger logs JSON
  │
  ▼
MetricsCollector records
      latency
      tokens
      errors
      cache hit/miss
  │
  ▼
Response returned

- [ ] **Query re-writing** the current pipeline doesn't check for any threats to the prompt injection we might need some query re-writing too.
- [ ] This treats every error the same:
expired token (which the API client may already have retried)
backend outage
network disconnected
server returning 500
In all those cases, the UI ends up looking as if the user is logged out.
A more informative approach would distinguish authentication failures (401/403) from transient network or server failures, allowing the UI to show "Unable to reach the server" instead of appearing to log the user out.
## Changes Made
- [x] **Changed Gemini_api_model** I changed gemini model to gemini-flash-latest and the test cases worked properly , I hanvn't checked for the application yet.
  → Confirmed correct: `gemini-flash-latest` is present in `GET /v1beta/models` and `generate_json` returns 200 against the live API. Propagated to `.env.example` and `docker-compose.yml`, which still said `gemini-1.5-flash`.

## Known lint debt — ✅ CLEARED (2026-08-08, `d136207`)
Was 13 ruff + 8 mypy errors. Now **zero of each**, and CI fails on any regression.
One was a real latent bug: `evaluator.py` called `.split()` on a `str | None`.
Linters are now **pinned** (`ruff==0.16.2`, `mypy==2.1.0`) with an explicit
`[tool.ruff.lint] select`. The first CI run failed on a rule local ruff did not
have — an unpinned range guarantees CI and developers eventually disagree.

## Suggested next step
Phases 0, 1, and 3 (testing/CI) are closed as of 2026-08-08.

What is left, roughly in order of value:
1. **Durable evaluation queue (Redis + arq)** — decided, scoped, not built. See
   the follow-up under *Async evaluation* in Phase 1 for the problem, the
   rejected alternative, and the shape of the work.
2. **Phase 2 — auth completeness.** Logout, refresh-token rotation/revocation,
   change password. The `jti` claim is already minted and never used.
3. **Discovery items below** — PII masking before sending to the LLM is a real
   security gap, not a nice-to-have.
4. **Phase 4 roadmap** — production RAG, multi-provider, streaming.

### Operational follow-ups (not code)
- **Rotate the Gemini API key.** It was written to logs in cleartext before `e0e1ad6`.
- **Rate-limit defaults sit above the account ceiling** (20/user/hour vs 20/day
  for the whole free-tier account). Lower them, or move off the free tier.
- **Move rate-limit counters to Redis** before running more than one worker —
  they are per-process today, so N replicas means N× the intended ceiling. The
  queue work above brings Redis into the stack anyway, so these pair naturally.

### Why testing came before the rest of Phase 1
Three genuine bugs were invisible to the test suite and only surfaced by driving
real HTTP against real Postgres: a `MissingGreenlet` in the follow-up path, a
mangled migration constraint name, and a rate limiter that returned 422 on every
route it protected. The fixture that made HTTP testing possible at all was itself
only fixed mid-session.

### Testing notes for whoever picks this up
- `conftest.py` now disposes the SQLAlchemy engine between tests — without it,
  the second DB-touching HTTP test fails with "attached to a different loop".
- Prefer `app.dependency_overrides` for HTTP tests; a request that reaches the
  session dependency with no Postgres re-raises through the test client.
- Live Gemini verification is limited to **20 requests/day**. Budget it.