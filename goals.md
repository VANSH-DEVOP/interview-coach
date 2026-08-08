# InterviewPilot AI — Goals & Backlog
Living checklist of remaining work. Derived from a full codebase read on **2026-07-26** (HEAD: `879a197 Feat: AI and RAG pipeline foundation`).
Legend: `[ ]` todo · `[~]` partially done · `[x]` done
Priority: **P0** blocking/broken · **P1** high value · **P2** nice to have
---
## Phase 0 — Broken / blocking (P0)
Things that are wired but do not actually work today. Do these first.
- [ ] **Test suite cannot import.** `chromadb` is in `pyproject.toml` but not installed in `.venv/` or `backend/.venv/`; `app/services/ai/vector_store.py:11` imports it at module scope and `app/api/deps.py` pulls it in transitively, so *every* test fails at conftest import. Fix by `pip install -e ".[dev]"`, and consider making the chroma import lazy so the app boots without the RAG extra.
- [ ] **ChromaDB index is ephemeral.** Persist directory is hardcoded to `/tmp/interviewpilot/chroma` (`app/api/deps.py:70`) and is not a Docker volume — every restart wipes the resume index and RAG silently degrades to `resume_text[:4000]`. Move to a configured path (`CHROMA_PATH` setting) + named volume in `docker-compose.yml`.
- [ ] **RAG services are rebuilt per request.** `get_rag_service()` constructs a new `EmbeddingService` and Chroma client on every call. Cache them (module-level `@lru_cache` or app state/lifespan).
- [ ] **Every Gemini call 404s — the configured model is retired.** Observed **2026-07-27**: every AI-backed request logs `POST .../v1beta/models/gemini-1.5-flash:generateContent → 404 Not Found` and the route still returns `201`. `GEMINI_MODEL` defaults to `gemini-1.5-flash` (`app/core/config.py:54`, mirrored in `.env.example:37`), which no longer exists on the v1beta endpoint. **Nothing in the product has ever used real AI output** — every question, follow-up, and report currently comes from the deterministic fallbacks. Move to a current model and verify with `GET /v1beta/models` for the key in use. Check `EmbeddingService`'s `models/embedding-001` (`app/services/ai/embedding.py:25`) the same way — if it 404s too, RAG has been silently no-op as well.
- [ ] **Silent AI failures.** ↑ The 404 above went unnoticed for exactly this reason: `FallbackQuestionGenerator` / `FallbackEvaluator` swallow every exception with no logging (`app/services/ai/base.py:92`, `:104`), so a dead model, a bad key, and "the AI is just generic" all look identical from the outside. Log at WARNING with the provider error before falling back, and surface a degraded-mode signal (health check field or a response flag) so this can't hide again.
- [ ] **Gemini reports render an empty summary.** `report-view.tsx:53` reads `detailed_feedback.summary`, but only `HeuristicEvaluator` sets it (`evaluator.py:149`); `GeminiEvaluator` writes only `recommendations` + `per_question` (`evaluator.py:228`). Add `summary` to the Gemini prompt + parse.
---
## Phase 1 — Complete what's half-built (P1)
Scaffolding exists; the feature does not.
### Interview flow
- [ ] **Follow-ups have no resume context.** `interview_service.py:136` calls `follow_up(..., resume_text=None)` and never consults RAG. Pass the session's resume text and add a RAG retrieval keyed on the answer.
- [ ] **Async evaluation.** `complete()` runs the evaluator inline, so ending an interview blocks on a Gemini round-trip. `ReportStatus.PENDING / GENERATING / FAILED` are currently dead code — write the report as `PENDING`, hand off to a worker, and have the frontend poll. (`app/models/evaluation_report.py:16`)
- [ ] **Abandon / delete a session.** `SessionStatus.CREATED` and `ABANDONED` are unreachable — sessions are created as `IN_PROGRESS` and there is no endpoint to abandon or delete one, even though `complete()` already handles the abandoned case (`interview_service.py:160`).
- [ ] **Answer timing.** `Answer.duration_seconds` exists in the model, schema, and TS types but is never sent — the interview UI has no timer.
- [ ] **Question controls.** No skip, no re-answer, no regenerate-questions.
- [ ] **Interview configuration.** Type (behavioral / technical / system design), difficulty, and question count are all decided by the model; expose them on `InterviewCreate`.
### Resumes
- [ ] **Re-parse / re-index endpoint.** A parse failure sets `ResumeStatus.FAILED` permanently with no retry path (`resume_service.py:66`), and a resume uploaded before RAG was enabled is never indexed.
- [ ] **Resume preview** in the UI (currently download-only).
### Reports & progress tracking
- [ ] **Progress over time.** The README promises it; the dashboard has three counters and a recent-sessions list (`app/(app)/dashboard/page.tsx`). Needs a score trend across sessions.
- [ ] **Per-question scores.** `per_question` is passed through raw from the model with no numeric score (`evaluator.py:230`).
- [ ] **Skill/category breakdown** across reports.
- [ ] **Report export (PDF) and sharing.**
### Frontend
- [ ] **Pagination controls.** The API is paginated; every page hardcodes `page=1` (`interviews/page.tsx:29`, `reports/page.tsx:26`) with no next/prev UI.
- [ ] **Expose `/interviews/{id}/reevaluate`** — the endpoint exists and is never called from the frontend.
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
### Testing (P1)
- [ ] **API-level tests.** The only HTTP test is `/health` (`tests/api/test_health.py`). Nothing covers auth, resumes, interviews, or reports over HTTP.
- [ ] **Test database fixture.** No DB-backed tests, so repositories and migrations are unverified. Add a throwaway Postgres (testcontainers or a CI service) + transactional fixture.
- [ ] **Frontend tests.** None exist. Start with `api-client` refresh-retry logic and the interview page state machine.
- [ ] **E2E smoke test** (register → upload resume → interview → report).
### CI/CD (P1)
- [ ] **No CI at all** despite `origin` and `gitlab` remotes. Pipeline should run `ruff check`, `mypy app`, `pytest`, `npm run typecheck`, `npm run build`.
- [ ] Migration check in CI (autogenerate produces no diff against models).
### Security (P1)
- [ ] **Rate limiting.** None anywhere — neither on auth endpoints nor on the Gemini-backed routes, where each request costs money and burns a 60 req/min free-tier quota.
- [ ] **httpOnly cookie BFF for tokens.** Tokens currently live in JS-readable cookies (`api-client.ts:35`), flagged as MVP in the code itself.
- [ ] **The Gemini API key is written to the logs in cleartext.** `GeminiClient` passes the key as a `?key=` query param, and httpx logs the full URL at INFO — so every AI call prints the key (seen in the backend container logs on 2026-07-27). Send it as the `x-goog-api-key` header instead, and/or set `logging.getLogger("httpx").setLevel(WARNING)`. Rotate the key that has already been logged.
- [ ] **CSP + security headers.**
- [ ] **Per-user quotas** on interview creation / resume uploads.
### Observability (P2)
- [ ] Metrics + tracing (request logging is all there is: `app/middleware/request_logging.py`).
- [ ] Error reporting (Sentry or equivalent).
- [ ] AI-call telemetry: latency, token spend, **fallback rate** — a non-zero fallback rate is the alert that would have caught the `gemini-1.5-flash` 404 on day one.
- [ ] **Log level is too verbose.** Each AI call emits ~15 `httpcore` DEBUG lines (connect/TLS/request/response teardown) that bury the one line that matters. Pin third-party loggers (`httpcore`, `httpx`) to WARNING.
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
- [ ] **RAG implementation** it's too simple , like I want production level rag implementaion (hybrid-search , langchain, langgraph and all that neccessary items )
- [ ] **Query re-writing** the current pipeline doesn't check for any threats to the prompt injection we might need some query re-writing too.
- [ ] This treats every error the same:
expired token (which the API client may already have retried)
backend outage
network disconnected
server returning 500
In all those cases, the UI ends up looking as if the user is logged out.
A more informative approach would distinguish authentication failures (401/403) from transient network or server failures, allowing the UI to show "Unable to reach the server" instead of appearing to log the user out.
## Changes Made
- [ ] **Changed Gemini_api_model** I changed gemini model to gemini-flash-latest and the test cases worked properly , I hanvn't checked for the application yet.