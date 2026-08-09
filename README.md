# InterviewPilot AI

AI-powered interview preparation platform. Users register, upload resumes, run adaptive AI mock interviews, receive evaluation reports, and track progress over time.

This repository contains the production-grade foundation: full authentication, resource APIs, a normalized database schema, a responsive enterprise UI, and clearly marked seams where the AI pipeline (Gemini, LangGraph, ChromaDB) plugs in.

---

## 1. Project Overview

| Capability | Status |
|---|---|
| Registration / login (JWT, refresh tokens) | ✅ Implemented |
| Resume upload (local storage behind abstraction) | ✅ Implemented |
| Interview sessions with question/answer flow | ✅ Implemented (static questions) |
| Adaptive AI follow-up questions | 🔌 Seam ready (`QuestionGenerator`) |
| Evaluation reports | 🔌 Stub created on completion; AI fills later |
| History & progress tracking | ✅ Implemented (sessions + reports lists) |

## 2. Architecture

```
┌─────────────────────────────────────────────────────┐
│  PRESENTATION · Next.js App Router, TS, Tailwind,    │
│  shadcn/ui · dark enterprise theme · responsive      │
└───────────────────────┬─────────────────────────────┘
                        │ REST /api/v1 · JWT Bearer
┌───────────────────────▼─────────────────────────────┐
│  APPLICATION · FastAPI                               │
│  api (routers) → services (logic) → repositories    │
│  core: settings · security · logging · exceptions   │
│  middleware: error envelope · request logging        │
│  seams: StorageService · QuestionGenerator (AI)      │
└──────────┬────────────────────────────┬─────────────┘
           │ SQLAlchemy (async)         │ arq / Redis
           │ Alembic                    │ (evaluation jobs)
┌──────────▼──────────────┐  ┌──────────▼─────────────┐
│  DATA · PostgreSQL 16    │  │  WORKER · arq          │
│                          │◄─┤  app/worker.py         │
└──────────────────────────┘  └────────────────────────┘
```

The worker is a **second process running the same image**. Ending an interview
returns immediately with a PENDING report; the evaluation itself is a provider
round-trip, so it is queued and the client polls. Without `REDIS_URL` the API
falls back to in-process background tasks — same result, but a restart loses
the work. See `app/services/job_queue.py`.

**Layer rules**

- Routers never touch the database; they call services.
- Services contain business logic and depend on abstractions (repositories, `StorageService`, `QuestionGenerator`).
- Repositories are the only layer that builds SQL.
- Domain errors are raised as typed exceptions and translated to a consistent envelope: `{"error": {"code", "message", "details"}}`.

## 3. Folder Structure

```
backend/
├── app/
│   ├── api/            # deps.py (DI) + v1/ routers
│   ├── core/           # config, security, logging, exceptions
│   ├── db/             # base (naming conventions), session
│   ├── models/         # SQLAlchemy ORM (6 entities)
│   ├── schemas/        # Pydantic request/response contracts
│   ├── services/       # business logic + storage/ + ai/ seams
│   ├── repositories/   # all SQL lives here
│   ├── middleware/     # error envelope, request logging
│   └── main.py         # app factory
├── alembic/            # migrations (0001_initial_schema)
└── tests/              # health contract + storage contract tests

frontend/
├── src/
│   ├── app/            # / · (auth)/login,register · (app)/dashboard,profile,interviews,reports
│   ├── components/     # ui/ (primitives) · layout/ (sidebar, mobile nav) · shared/
│   ├── lib/            # api-client (typed, auto-refresh), utils
│   ├── hooks/          # use-auth
│   ├── types/          # mirrors backend schemas
│   └── styles/         # design tokens (teal / black / gray)
└── middleware.ts       # route protection
```

## 4. Setup Instructions

### Docker (recommended)

```bash
cp .env.example .env       # then set JWT_SECRET_KEY: openssl rand -hex 32
docker compose up --build
```

- Frontend: http://localhost:3000
- API docs (Swagger): http://localhost:8000/docs
- Health: http://localhost:8000/api/v1/health

Migrations run automatically on backend start.

### Local development (without Docker)

Backend:

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Postgres must be running; see .env.example for variables
alembic upgrade head
uvicorn app.main:app --reload
```

Evaluations run in-process unless `REDIS_URL` is set. To run them the way
production does, start Redis and add a second terminal:

```bash
cd backend
arq app.worker.WorkerSettings     # needs REDIS_URL, same as the API
```

Frontend:

```bash
cd frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1 npm run dev
```

Tests:

```bash
cd backend && pytest
```

Tests that need Postgres or Redis **skip** when the service is not reachable, so
`pytest` stays useful with nothing running. CI sets `REQUIRE_TEST_DATABASE=1`
and `REQUIRE_TEST_REDIS=1` to turn those skips into failures — a broken service
container must not produce a green build.

## 5. Development Workflow

1. Branch from `main`.
2. Schema change? Edit models, then `alembic revision --autogenerate -m "describe change"` and review the generated migration.
3. New resource = model → schema → repository → service → router → register in `api/v1/router.py`.
4. Keep business logic in services; keep SQL in repositories.
5. Run `pytest`, `ruff check`, and `npm run typecheck` before pushing.
6. Open an MR; CI (to be added) gates merges.

## 6. Future AI Integration Points

| Integration | Seam | What changes |
|---|---|---|
| **Gemini** (question generation, evaluation) | `app/services/ai/base.py` → implement `QuestionGenerator`; register in `get_question_generator()` | New module only; `InterviewService` untouched |
| **LangGraph** (adaptive interview orchestration) | Same seam; the `follow_up()` hook in `InterviewService.submit_answer` already persists follow-up questions with `parent_question_id` chains | New module only |
| **ChromaDB** (resume RAG) | `Resume.parsed_text` column exists; add a parser/embedder service consuming the `StorageService.read()` bytes | New service + worker |
| **Object storage** (S3 / R2 / MinIO) | `app/services/storage/` → implement `StorageService`; add a case in `get_storage_service()` and a `STORAGE_BACKEND` value | Zero changes to business logic, routes, or models |
| **Evaluation pipeline** | `EvaluationReport` rows are created with `status=pending` on session completion; a worker picks them up and fills scores/feedback | New worker only |

## 7. Security Notes (MVP → production hardening)

- Passwords: bcrypt. Tokens: short-lived access + 7-day refresh JWTs.
- **Refresh tokens rotate and are revocable.** Every issued token is recorded by
  `jti`; refreshing revokes the old one. Presenting an already-rotated token is
  treated as replay and revokes every session for that account. Logout, password
  change, password reset and account deletion all end sessions server-side.
- **Password reset does not reveal who has an account.** `/auth/forgot-password`
  answers identically for known and unknown addresses, and delivery failures are
  swallowed rather than surfaced, because an error that only appears for real
  accounts is the same oracle by another route.
- **Email verification gates nothing** by design — the default email backend
  writes to a log, so gating would make a fresh local or demo deployment
  unusable. It is recorded and surfaced, not enforced.
- **`EMAIL_BACKEND=log` is refused in production.** It prints reset links in
  full. A production start fails until SMTP is configured; that is deliberate.
- **Direct identifiers are redacted before any text reaches the AI provider.**
  Prompts and embedding requests both pass through `app/services/ai/masking.py`,
  which strips email addresses, phone numbers, URLs, government identity
  numbers, and the account holder's own name. Employers, titles, schools,
  technologies and dates survive — they are the interview, and none of them is
  a direct identifier. This is pseudonymisation, not anonymity.
  - Redaction happens **at the two HTTP boundaries** (`GeminiClient` and
    `EmbeddingService`), not in the code that builds prompts, so a new call
    site cannot forget it. Both default to a pattern-only redactor, meaning a
    caller that omits the account holder's identity degrades to "no name
    matching" rather than to "no redaction".
  - It is **one-way**: nothing is restored on the way back, so no later bug can
    re-attach a redacted value to model output.
  - Postgres and the Chroma index still hold the resume in full. The control is
    about what crosses the network to a third party, not about storage at rest.
- Resume uploads: content-type allowlist, 5 MiB cap, opaque storage keys, path-traversal protection.
- Account deletion is a hard delete, and clears the resume blobs and the vector
  index as well as the database rows — the database cascade cannot reach either.
- Hardening backlog: httpOnly cookie BFF for tokens, CSP headers, and a scheduled
  purge of expired token rows (`delete_expired()` exists on both token
  repositories; nothing calls it yet).