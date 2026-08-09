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
- Resume uploads: content-type allowlist, 5 MiB cap, opaque storage keys, path-traversal protection.
- Hardening backlog: httpOnly cookie BFF for tokens, refresh-token rotation/revocation list, rate limiting, CSP headers.