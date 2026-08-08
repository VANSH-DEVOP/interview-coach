# One-Week Codebase Onboarding Plan

Goal: by Sunday evening you can open any file in this repo and know *why* it exists, *who calls it*, and *what breaks if you change it* — without re-reading everything.

**Scope: existing code only.** No new features this week. That's `goals.md`, starting next week.

**Size of the thing you're learning:** ~4,450 lines of backend Python + ~1,900 lines of frontend TypeScript. That's small. You can genuinely read all of it — the difficulty is not volume, it's that the architecture is layered and indirection-heavy (ABCs, factories, dependency injection), so a single feature is spread across 5–6 files. This plan reads it *by flow*, not by folder.

**Budget:** ~2–3 focused hours a day. Each day is: read in a fixed order → trace one request end to end → answer the self-check questions from memory → do one small hands-on.

**Rule for the week:** don't change behavior. Add `print()`/`logger.debug()`, set breakpoints, write throwaway scripts, delete them after. If you catch a bug, write it in `goals.md` instead of fixing it.

**Keep a notes file.** After each day, write 5–10 lines in `notes/day-N.md` answering "what surprised me". Those notes are worth more next week than the reading itself.

---

## Day 1 — Make it run, then see it whole

**Goal:** the app works on your machine and you've watched one full user journey with your own eyes.

### Read (in this order, ~45 min)
1. `README.md` — the intended architecture (note: it's stale about AI; see Day 5).
2. `CLAUDE.md` — the accurate current map. Read the "Backend architecture" and "AI pipeline" tables slowly.
3. `docker-compose.yml` — three services, which ports, which volumes.
4. `.env.example` — every knob the app has.
5. `backend/Dockerfile` — note the `CMD`: migrations run, *then* the server starts.

### Hands-on (~1.5 h)
```bash
cp .env.example .env        # set JWT_SECRET_KEY: openssl rand -hex 32
docker compose up --build
```
Then, in the browser:
1. Register an account at http://localhost:3000/register.
2. Upload a PDF resume.
3. Start an interview with a target role, answer all questions, end it.
4. Read the report.
5. Open http://localhost:8000/docs and re-do steps 2–4 through Swagger, by hand. Copy the access token out of your browser cookies (`ip_access_token`) and use "Authorize".

While doing this, keep `docker compose logs -f backend` open in a second terminal. Watch the request log lines appear as you click.

**Also fix your local (non-Docker) env today**, because you'll need tests all week:
```bash
cd backend && source .venv/bin/activate && pip install -e ".[dev]"
pytest -q                   # should collect; see CLAUDE.md if it doesn't
```

### Self-check
- What are the three containers, and which one talks to which?
- Where does the resume file physically live? (Not in the database — where?)
- Which port is Postgres on from your host? (Careful: the compose file, `.env.example`, and the code default disagree.)
- What happens to your session if you `docker compose down -v`?

---

## Day 2 — The backend spine

**Goal:** understand the layering contract. Every feature next week will follow it, so this is the highest-leverage day.

### Read (in this order, ~90 min)
1. `app/main.py` (47 lines) — the app factory. Small, but it's the root of everything.
2. `app/core/config.py` (77) — every setting; note `@lru_cache` and that **no other module reads env vars**.
3. `app/core/exceptions.py` (57) — the domain error taxonomy.
4. `app/middleware/error_handler.py` (43) — how those exceptions become the `{"error": {...}}` envelope.
5. `app/middleware/request_logging.py` (33).
6. `app/core/logging.py` (43).
7. `app/api/v1/router.py` (13) — the registry.
8. `app/api/deps.py` (126) — **the most important file in the backend.** This is where the object graph gets built per request.
9. `app/schemas/common.py` (33) — `Page`, `PageParams`.

### Trace exercise (~45 min)
Follow `GET /api/v1/health` from HTTP request to JSON response, naming every function it passes through: ASGI → `RequestLoggingMiddleware` → CORS → router → `health()` → `get_session()` dependency → back out. Write the chain down in your notes.

Then do the same for `GET /api/v1/users/me`, which adds the auth dependency. You should end up with something like:
`get_current_user` → `_bearer` → `decode_token` → `UserRepository.get` → route → `UserRead` serialization.

### Self-check
- A service needs to signal "this resume doesn't exist". What does it raise, and what HTTP status does the client see? Which file made that decision?
- Why do routes never call `HTTPException`?
- Where does the database transaction get committed? What happens on an exception? (`app/db/session.py:26`)
- If you add a new router file, what are the *two* places you must touch?
- What does `Annotated[X, Depends(...)]` mean, and why is `deps.py` full of it?

### Hands-on
Add a temporary route `GET /api/v1/debug/boom` that raises `NotFoundError("nope")`, hit it, and observe the envelope. Then make it raise a bare `ValueError` and observe the difference. **Delete the route afterwards.**

---

## Day 3 — Auth, the data layer, and migrations

**Goal:** understand how a user is identified and how rows get in and out of Postgres.

### Read (~90 min)
**Auth:**
1. `app/core/security.py` (61) — bcrypt + JWT primitives, pure functions.
2. `app/services/auth_service.py` (61) — register / login / refresh.
3. `app/api/v1/auth.py` (28) and `app/schemas/auth.py` (16).
4. Re-read `get_current_user` in `deps.py:108` now that you've seen `decode_token`.

**Data layer:**
5. `app/db/base.py` (34) — `Base`, the naming convention, the two mixins.
6. `app/db/session.py` (34) — engine, session factory, the request-scoped dependency.
7. `app/models/user.py` (26), then `resume.py` (41), `interview_session.py` (52), `question.py` (50), `answer.py` (29), `evaluation_report.py` (44). Read them as one graph, not six files — sketch the ERD on paper: who points at whom, which cascades are `ondelete="CASCADE"`, which relationships are 1:1 vs 1:N.
8. `app/models/__init__.py` (21) — and understand *why* it exists (Alembic autogenerate).
9. `app/repositories/base.py` (41), then `user_repository.py` (12), `resume_repository.py` (28), `interview_repository.py` (54), `report_repository.py` (56).
10. `alembic/versions/0001_initial_schema.py` (168) and `alembic/env.py` (59).

### Trace exercise (~30 min)
Follow `POST /auth/login` all the way to the two JWTs, then follow one of those JWTs back in on the next request until it becomes a `User` object in a route handler.

### Self-check
- What's inside an access token? (Decode one at jwt.io — it's HS256 with your secret.) What distinguishes access from refresh?
- Tokens carry a `jti`. Is it used for anything? (This answers *why* "token revocation" is on the backlog.)
- Why does `get_owned(id, user_id)` exist instead of just `get(id)`? What's the security bug if you use the wrong one?
- Which repositories eagerly load relationships, and why does `with_questions=True` exist? What breaks in async SQLAlchemy if you lazy-load?
- Why do repositories `flush()` but never `commit()`?
- Naming conventions in `db/base.py` — what goes wrong in migrations without them?

### Hands-on
```bash
docker compose exec db psql -U interviewpilot -d interviewpilot
\dt
\d interview_sessions
select * from questions limit 5;
```
Look at the rows *your* Day 1 interview created. Then run `alembic revision --autogenerate -m "scratch"` without changing any model — it should produce an empty migration. Read it, then **delete the file**. (If it's *not* empty, you've found a real drift bug — log it in `goals.md`.)

---

## Day 4 — Resumes and the storage abstraction

**Goal:** see the "ABC + impl + factory" pattern in its simplest form, before meeting it again in the AI layer where it's harder.

### Read (~60 min)
1. `app/services/storage/base.py` (46) — the `StorageService` interface. Read the docstrings.
2. `app/services/storage/local.py` (68) — the only implementation. Note path-traversal protection.
3. `app/services/storage/__init__.py` (29) — the factory keyed on `STORAGE_BACKEND`.
4. `app/services/resume_parser.py` (82) — pypdf / python-docx text extraction.
5. `app/services/resume_service.py` (121) — the orchestrator. **Read this one twice.**
6. `app/api/v1/resumes.py` (67) and `app/schemas/resume.py` (17).
7. `tests/test_storage.py` (37) and `tests/test_resume_parser.py` (76) — note these are *real* unit tests with no DB and no network. This is the pattern to copy next week.

### Trace exercise (~30 min)
Walk `POST /resumes` step by step and list, in order, every side effect: validation → blob write → text parse → DB row → RAG indexing. For each one, answer: *if this step fails, what happens to the others?* (Look closely at the `try/except` around RAG indexing at `resume_service.py:82` and the parse at `:62` — the failure semantics are deliberately different.)

Then trace `DELETE /resumes/{id}` and note the ordering choice at `resume_service.py:112` and the comment explaining it.

### Self-check
- Why is the stored filename an opaque UUID instead of the user's filename? Two reasons.
- Where is the 5 MiB limit enforced, and where does the allowed content-type list live?
- `ResumeStatus` has three values. Which code path sets each? Which one has no recovery path today?
- If you were adding S3 support tomorrow, exactly which files would you touch? (The answer should be short. That's the point of the abstraction.)

### Hands-on
Run `pytest tests/test_storage.py -v` and read the tests alongside `local.py`. Then break something deliberately — make `LocalStorageService.read` return `b""` — and watch which test fails. Revert.

---

## Day 5 — The interview domain

**Goal:** the core business logic of the product. This is the file you'll modify most next week.

### Read (~90 min)
1. `app/schemas/interview.py` (56) — the request/response contracts first, so the service reads easier.
2. `app/services/interview_service.py` (215) — **the heart of the app.** Read method by method: `create` → `submit_answer` → `complete` → `reevaluate`. Don't worry about *how* the generator/evaluator work yet (that's tomorrow); treat them as black boxes with known signatures.
3. `app/api/v1/interviews.py` (84).
4. `app/repositories/interview_repository.py` (54) — re-read now that you know the queries' purpose.
5. `app/api/v1/reports.py` (51), `app/repositories/report_repository.py` (56), `app/schemas/report.py` (21).
6. `tests/test_interview_flow.py` (226) — an end-to-end service test with fake repositories. **Read this carefully**; it's the best executable documentation in the repo.

### Trace exercise (~45 min)
On paper, draw the state of the database after each of these calls, for one session:
`POST /interviews` → `POST /interviews/{id}/answers` (×3) → `POST /interviews/{id}/complete`.
Which rows exist in `interview_sessions`, `questions`, `answers`, `evaluation_reports`, and what are their status columns at each step?

Then answer: where could a follow-up question appear in that sequence, and how is it linked to its parent?

### Self-check
- Which `SessionStatus` values can a session actually reach today? Which are dead? (Cross-reference `models/interview_session.py:18` with every assignment in the service.)
- What is `sequence_number` for, and how is it computed for follow-ups (`interview_service.py:139`)? Can you see a bug there?
- Why does `complete()` produce a `COMPLETED` report immediately, and what does that mean for a slow Gemini call?
- What's the difference between `complete()` and `reevaluate()`? Why does the latter exist?
- Why does `_utcnow()` strip `tzinfo`? What error does that prevent? (`interview_service.py:12`, and the regression guard in the test file.)

### Hands-on
`pytest tests/test_interview_flow.py -v`, then read `_FakeSession` and `_FakeRepo` in that file. Understanding how they stand in for a real `AsyncSession` will teach you both the test pattern *and* the real session's contract.

---

## Day 6 — The AI layer

**Goal:** the most indirection-heavy part of the codebase. The trick is that **every AI capability is the same three-part shape**: an ABC, a real implementation, and a deterministic fallback, chosen by a factory.

Read `backend/AI_INTEGRATION.md` first (10 min) — its flow diagrams are accurate. Then `backend/RAG_IMPLEMENTATION.md`.

### Read (~2 h)
**The seam pattern:**
1. `app/services/ai/base.py` (130) — `QuestionGenerator` ABC, `StaticQuestionGenerator`, `FallbackQuestionGenerator`, `get_question_generator()`. Understand the wrapper before the implementations.
2. `app/services/ai/gemini_client.py` (57) — a hand-rolled httpx call to the REST API. No SDK. Small and worth reading line by line.
3. `app/services/ai/gemini.py` (116) — prompt construction + response parsing.
4. `app/services/ai/evaluator.py` (262) — the same shape again: `Evaluator` ABC, `HeuristicEvaluator`, `GeminiEvaluator`, `FallbackEvaluator`. Pay attention to `_first()` and `_as_str_list()` and *why* they're so defensive.

**RAG:**
5. `app/services/ai/rag.py` (189) — `TextChunker` + `RAGService`.
6. `app/services/ai/embedding.py` (95) — Gemini embeddings.
7. `app/services/ai/vector_store.py` (178) — `VectorStore` ABC + `ChromaVectorStore`.
8. Re-read `deps.py:59` `get_rag_service()` — now you can see how all of it gets assembled per request.

**Tests:**
9. `tests/test_question_generator.py` (151) and `tests/test_evaluator.py` (218) — note `_FakeClient`: this is how you test AI code with no network.

### Trace exercise (~45 min)
Answer this precisely: **what exactly happens when `GEMINI_API_KEY` is empty vs. set?** Walk `get_question_generator()` and `get_evaluator()` in both cases and name the concrete object that ends up inside `InterviewService`.

Then: with the key set, but Gemini returning HTTP 429 — trace what the user sees. Which layer catches it? Is anything logged? (This is Phase-0 item #4 in `goals.md`; now you'll know why it matters.)

### Self-check
- Draw the full path from "user uploads resume" to "a question mentions their Kafka experience." It crosses ~6 files.
- Why does the whole AI layer degrade to deterministic implementations instead of erroring? What product guarantee does that protect?
- Where does the vector index physically live, and what happens to it on container restart?
- Why is `resume_text[:4000]` still in the code if RAG exists?
- What does `follow_up()` receive as `resume_text` when called from `InterviewService`? (`interview_service.py:136` — this one's a real gap.)

### Hands-on
Run `pytest tests/test_evaluator.py -v` and read a `_FakeClient` payload alongside the parsing code. Then, with your real API key set locally, run `python tests/test_gemini_integration.py`-style probes (or just call the API) and compare a real Gemini report to what `HeuristicEvaluator` produces for the same transcript. Seeing both outputs side by side makes the fallback design click.

---

## Day 7 — The frontend, then synthesis

**Goal:** close the loop — you've seen the API from the inside; now see how it's consumed.

### Read (~90 min)
1. `frontend/src/types/index.ts` — the hand-written mirror of the Pydantic schemas. Read it next to `backend/app/schemas/`.
2. `frontend/src/lib/api-client.ts` (133) — **the most important frontend file.** Token storage, the error envelope → `ApiError` mapping, and the one transparent refresh-and-retry.
3. `frontend/middleware.ts` (36) — route protection, and why it's only a UX guard.
4. `frontend/src/hooks/use-auth.ts` (71).
5. `frontend/src/app/layout.tsx`, `(auth)/layout.tsx`, `(app)/layout.tsx` — how route groups produce two different shells.
6. `frontend/src/app/(app)/interviews/[sessionId]/page.tsx` (192) — the most complex page; read it against yesterday's understanding of the interview API.
7. Skim: `dashboard/page.tsx`, `resumes/page.tsx`, `reports/page.tsx`, `components/shared/report-view.tsx` (134).
8. Skim `components/ui/*` — hand-rolled shadcn-style primitives, `cva` variants, and `styles/globals.css` for the design tokens.

### Trace exercise (~30 min)
Follow a login click from `login/page.tsx` → `useAuth.login` → `api.post` → cookie write → redirect → `middleware.ts` allowing `/dashboard` → `useAuth.loadUser` → `GET /users/me`. Then answer: what happens 31 minutes later when the access token has expired and you click something?

### Synthesis (~1 h) — do this, don't skip it
Without looking at the code, on one page, write out the **complete path of a single answer submission**: the React handler, the fetch, the auth dependency, the route, the service, the repository, the AI seam, the database, and back to the re-render. Then open the files and check yourself. Every gap you find is exactly what to re-read.

Finally, re-read `goals.md` end to end. It should now read as obvious rather than cryptic — and you'll likely want to re-prioritize a few items. That edited `goals.md` is your Week 2 plan.

---

## Cheat-sheet: where things live

| I want to change… | Go to |
|---|---|
| A setting / env var | `app/core/config.py` — nowhere else reads env |
| What a URL does | `app/api/v1/<resource>.py` |
| How objects get built per request | `app/api/deps.py` |
| Business rules | `app/services/<x>_service.py` |
| A SQL query | `app/repositories/` — the only place queries are built |
| The database shape | `app/models/` + a new Alembic revision |
| Request/response JSON | `app/schemas/` **and** `frontend/src/types/index.ts` |
| An error's HTTP status | `app/core/exceptions.py` |
| Prompts / model behavior | `app/services/ai/gemini.py`, `evaluator.py` |
| Where files are stored | `app/services/storage/` |
| How the frontend calls the API | `frontend/src/lib/api-client.ts` |

## Recurring patterns to internalize

You'll see these three shapes over and over. Once they're familiar, the codebase gets small:

1. **ABC + implementation + factory** — `StorageService`, `QuestionGenerator`, `Evaluator`, `VectorStore`. Callers depend on the interface; a factory picks the impl from config.
2. **Primary + fallback wrapper** — `FallbackQuestionGenerator`, `FallbackEvaluator`. Try the real thing, catch everything, degrade to deterministic.
3. **Route → service → repository**, with dependency injection in `deps.py` and domain exceptions instead of HTTP errors.

## Prerequisites to look up as you hit them

Don't pre-study these — look them up the day you meet them:
- **Day 2:** FastAPI dependency injection, `Annotated`, ASGI middleware ordering.
- **Day 3:** async SQLAlchemy 2.0 (`Mapped`, `mapped_column`, why lazy loading is dangerous), Alembic autogenerate, JWT claims.
- **Day 5:** Python ABCs and `@abstractmethod`.
- **Day 6:** embeddings and cosine similarity — conceptually only, one article is enough.
- **Day 7:** Next.js App Router route groups, `"use client"`, and Next middleware.
