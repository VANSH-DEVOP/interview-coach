# RAG (Retrieval-Augmented Generation) Implementation

## Overview

InterviewPilot now implements **Retrieval-Augmented Generation (RAG)** for intelligent interview question generation. When a candidate uploads a resume, the system:

1. **Parses** the PDF/DOCX into text
2. **Chunks** the text along its own section headings, and stores the chunks as
   rows in `resume_chunks`
3. **Embeds** each chunk using Gemini's embedding API
4. **Stores** embeddings in ChromaDB vector database
5. **Retrieves** the most relevant sections when generating questions
6. **Generates** tailored questions using the retrieved context

This ensures questions are **personalized** to the candidate's actual experience, skills, and background.

> **Status, 2026-08-10.** The above describes the shipped pipeline, which is a
> working first version rather than a finished one — dense-only retrieval,
> fixed-size chunking with a duplicating overlap, one embedding HTTP call per
> chunk, no caching, no relevance threshold. `goals.md` holds a six-part plan
> to take it further. **Parts 1 (observability + benchmark), 2 (chunks as rows
> + structure-aware chunking), 3 (hybrid retrieval), 4 (embedding cache) and
> 5 (query rewriting + prompt-injection defence) and 6 (multi-step
> generation) have landed**; see
> below. **All six parts are complete.** The open item is the LangChain
> provider layer and LangSmith tracing; see `goals.md`.

## Observability

Retrieval fails quietly: when it is disabled, empty, or broken, question
generation falls back to `resume_text[:4000]` and still produces plausible
questions. The interview works and is simply no longer personalised, which is
why this needed instrumenting before it needed improving.

`app/services/ai/retrieval_metrics.py` records it, and `/health` reports it
under `rag`:

| Field | Reads as |
|---|---|
| `enabled`, `disabled_reason` | RAG off entirely — `no_api_key`, or `init_failed` when `CHROMA_PATH` is not writable (the usual case outside Docker) |
| `retrievals`, `hits`, `empty`, `failed` | Attempts and how they ended. `empty` means the resume was never indexed; `failed` means retrieval broke |
| `full_text_fallbacks` | Prompts built from truncated resume text. Climbing while `retrievals` stays flat means retrieval is never reached at all |
| `last_best_distance` | Cosine distance of the closest chunk on the last hit. Near 1.0 means the "hit" returned junk |
| `chunks_produced` vs `chunks_embedded` | A gap means the resume is indexed partially, and answers from a partial index for ever after |

Each retrieval also emits one structured log line (`rag.retrieval`) with the
same fields, so "how often does retrieval return nothing" is a query rather
than an afternoon of grepping.

## Benchmark

`tests/api/test_retrieval_eval.py` scores a fixed resume against twelve queries
using real (in-memory) Chroma and deterministic lexical embeddings, so it runs
in CI with no API key and no quota. Two tiers:

- **lexical** — queries sharing words with the resume. Pinned at recall@3 1.00
  / precision@1 1.00. This is a machinery check: chunk → embed → store → filter
  by resume → rank → assemble.
- **semantic** — the same six facts asked the way an interviewer would ask
  them. Currently recall@3 0.50 / precision@1 0.33, asserted against the hybrid
  retriever; the dense and sparse rows are printed alongside it for attribution.

Two rules for anyone extending it:

- **Hold the embedder fixed** when comparing pipeline versions, or the
  comparison measures hash-collision luck rather than retrieval.
- **Apply `RAG_MAX_DISTANCE` in any new probe.** Without the cutoff a
  paraphrased query leaves several chunks at cosine distance exactly 1.0, and
  which of those ties reaches the top 3 varies between processes — the same
  code scored 0.50 on one run and 0.67 on the next, which is how part 2 came to
  record a number that could not be reproduced.

The semantic score is a *lower bound* on the real system: a real embedding
model handles paraphrase and the deterministic stand-in cannot, so treat it as
roughly what the sparse half of a hybrid retriever contributes. Judging real
semantic quality still needs `test_rag_pipeline.py` and a live key.

Baselines are asserted as floors. Raise them when a change earns it; never
lower one to make the suite pass.

## Hybrid retrieval

A request retrieves through `HybridRetriever`, which runs both halves and fuses
them:

| Half | Store | Good at | Bad at |
|---|---|---|---|
| Dense | Chroma vectors | paraphrase — "distributed streaming" → a Kafka paragraph | exact rare tokens, averaged away inside a chunk |
| Sparse | Postgres `to_tsquery` over `resume_chunks.search_vector` | exact terms: `gRPC`, `Kubernetes`, version numbers | anything the query rephrases |

- **Fusion is Reciprocal Rank Fusion**, not a weighted score: cosine distance
  and `ts_rank` are not comparable, so a weighted sum would invent an exchange
  rate between them. A chunk both halves ranked beats either half's private
  favourite.
- **The tsvector is a generated column.** Postgres keeps it in step with
  `section` and `content`; nothing in the application writes it, so a chunk
  inserted by a migration or by psql is searchable too.
- **Query terms are ORed.** `plainto_tsquery` ANDs them, which for a query like
  "skills and experience relevant to Senior Backend Engineer" matches no chunk.
  Terms are tokenised in Python and bound as a parameter — `to_tsquery` has a
  syntax and raises on a stray operator, and a candidate's answer reaches this.
- **`RAG_MAX_DISTANCE` (default 1.0) is strict.** Cosine distance 1.0 is
  exactly orthogonal, so `<=` would keep precisely the chunks the cutoff exists
  to drop.
- **Either half failing degrades to the other**, and both empty returns `""`,
  which the generator turns into the raw-resume fallback and counts.

The dense and sparse halves must format chunk text identically, because fusion
matches candidates *by text*. One `retrieval_text()` in
`app/models/resume_chunk.py` serves both; if they diverged, nothing would ever
be recognised as found by both halves and the only symptom would be `agreed`
stuck at zero.

## Embedding cache

The free tier allows **20 requests per day for the whole account**, and indexing
one resume costs one embedding call per chunk — nine for the benchmark fixture,
more for a real CV. So two uploads exhausted the day, and `reprocess()`, whose
entire job is rebuilding an index, was unaffordable in practice.

With `REDIS_URL` set, `EmbeddingCache` stores each vector under
`embed:{model}:{sha256(redacted_text)}`. Measured end to end: indexing the
fixture resume costs 9 provider calls, re-indexing it costs **0**.

| Decision | Why |
|---|---|
| Key on the **redacted** text | The cache lives inside `EmbeddingService`, after redaction. Hashing earlier would fingerprint the candidate's name and email in Redis, and would miss for two resumes differing only in redacted identifiers |
| Model in the key | Vectors from different models are not comparable. Changing `GEMINI_EMBEDDING_MODEL` makes old entries unreachable instead of silently mixing incompatible vectors into one index |
| Not scoped per user | The vector is a pure function of the text; a hit tells the requester nothing they did not supply. Scoping would break cross-resume reuse for no gain |
| Packed float32 | 12 KiB for 3072 dimensions against ~60 KiB of JSON. The precision lost is far below anything cosine similarity can distinguish |
| Failures swallowed, counted apart | An unreachable cache behaves exactly like a cold one — the request succeeds at full price. `cache_errors` climbing while `cache_hits` stays flat is the only sign |
| Empty vectors not stored | `embed_batch` represents a per-chunk failure as `[]`; caching one would make that failure permanent for the life of the entry |

**Question sets are deliberately not cached.** It would save one call per
interview and make a candidate who practises the same role twice sit the
identical interview both times. The quota is spent on indexing, not generation.

## Query rewriting

The generator issues a rewritten query, not the raw phrase it has to hand.
`rewrite()` drops interview filler, collapses repeats, keeps technical tokens
(`C++`, `.NET`, `8.0`, `gRPC`) and caps the length.

- **Initial questions**: `"skills and experience relevant to {role}"` → the
  role's own words. The four dropped terms each match most of any resume.
- **Follow-ups**: the whole question plus the whole answer → the distinctive
  terms, answer first, so the cap bites the interviewer's phrasing rather than
  the candidate's specifics.

Deterministic rather than a model call: rewriting via the provider costs one
request per retrieval against a ceiling of twenty per day.

Measured on realistic follow-up exchanges (`FOLLOWUP_EXCHANGES` in the
benchmark), precision@1 went from 0.75 raw to 1.00 rewritten.

## Prompt injection

The resume and the answers are written by the person being assessed. The
evaluator is the high-value target: the candidate grades themselves.

Each untrusted span is fenced with a per-prompt random nonce and the prompt
states that fenced text is data rather than instructions. An attacker cannot
close a fence whose nonce they cannot predict, so injected text cannot escape
into instruction position; answers are fenced individually so a forged
`Q3:/A3:` turn stays inside the answer that contains it.

Phrase matching is deliberately **not** used — blocklists on natural language
fail against paraphrase and mangle legitimate text. And fencing is not a
guarantee: the controls that bound the damage are the 0–10 score clamp, the
JSON shape validation, and the fact that evaluation output is never executed.

## Question generation chain

`initial_questions` runs extract → generate → critique → refine. Only
`generate` always costs a provider call; at twenty requests per day for the
account, a model call per step would take the deployment from roughly six
interviews a day to two.

| Step | Cost | What it does |
|---|---|---|
| extract | free | Reads the resume's own `SKILLS`/`CERTIFICATIONS` block (labelled by the chunker) for the technologies to probe |
| generate | 1 call | The existing call, now naming those technologies |
| critique | free | Count, duplicates, requested type mix, whether any question touches a stated skill |
| refine | 1 call, conditional | Fixes exactly what the critique named; never retried, kept only if it has fewer problems |

Nothing enforced `question_count` before this, so a model returning three
questions when five were requested produced a three-question interview
silently. Extras are trimmed for free; a short set triggers the refinement.

## Architecture

Two flows. Indexing happens once per upload; retrieval happens on every
generation.

```
INDEXING                                RETRIEVAL (per request)
─────────────────────────────────       ───────────────────────────────────────
ResumeService.upload()                  free text: role, or question + answer
  store blob, parse PDF/DOCX                        │
        │                                           ▼
        ▼                               query.rewrite()   ← deterministic,
ResumeChunker.chunk()                     drops filler       no provider call
  split on the resume's own                       │
  section headings, pack to ~800                  │
        │                              ┌──────────┴──────────┐
        ▼                              ▼                     ▼
resume_chunks  (PostgreSQL)      DENSE                  SPARSE
  content, section, ordinal      embed query,           Postgres FTS over
  search_vector  (generated)     Chroma kNN,            search_vector,
  embedded_at NULL until done    RAG_MAX_DISTANCE       terms ORed
        │                              │                     │
        ▼                              └──────────┬──────────┘
RAGService.index_chunks()                         ▼
  redact → embed → upsert                fuse()  — Reciprocal Rank Fusion
        │                                  keeps orderings, not scores
        ▼                                         │
   ChromaDB                                       ▼
  (CHROMA_PATH volume)                   top-k chunks → prompt
                                                  │
                                    empty? ───────┴──→ resume_text[:4000]
                                                       counted as a
                                                       full_text_fallback
```

**Save chunks, then embed, then mark embedded.** The ordering in
`ResumeService._index` is deliberate: a provider failure leaves rows with
`embedded_at` NULL rather than leaving nothing, so *which* parts of the resume
are missing from the index survives the failure.

**Either half of retrieval failing degrades to the other.** Both empty returns
`""`, and the generator falls back to raw resume text — which is counted, not
silent.

## Components

### 1. ResumeChunker (`app/services/ai/rag.py`)

Splits resume text along the resume's own structure.

```python
chunks = ResumeChunker().chunk(resume_text)
# [Chunk(ordinal=0, section=None, content="Priya Raman\n..."),
#  Chunk(ordinal=1, section="SUMMARY", content="Backend engineer..."), ...]

chunks[1].retrieval_text     # "SUMMARY\nBackend engineer..." -- a property, not a call
```

**How it splits:**
- **Section headings first.** A short line that is all-caps or a known resume
  heading (`EXPERIENCE`, `Technical Skills`, …) starts a new section. Text
  before the first heading — the name and contact block — is kept with
  `section=None` rather than dropped or mislabelled.
- **Paragraphs within a section**, packed to ~800 characters. Splits happen at
  blank lines, never at line breaks: resume text comes out of a PDF
  hard-wrapped, so a line ending is a typographic accident and splitting on one
  cuts sentences in half.
- **A paragraph over the budget is left whole.** Half a job entry retrieves as
  neither of the two things it was.
- **No overlap.** The chunker this replaced faked overlap by duplicating the
  previous chunk's last 100 characters behind a `\n...\n` marker, so the same
  sentences were embedded twice, could be retrieved twice, and cost prompt
  budget as a copy of themselves.

**`retrieval_text` prepends the section heading**, so the third chunk of a long
EXPERIENCE section still says what it is; `content` stays clean in the database
for reading and for the keyword index. The implementation is a free function in
`app/models/resume_chunk.py`, exposed as a property on both `Chunk` and
`ResumeChunk` rather than written twice,
**because rank fusion matches candidates by chunk text** — if the two
formattings differed by a newline, no chunk would ever be recognised as found by
both halves and `agreed` would sit at zero for ever.

**Where chunks live:** rows in `resume_chunks`, written before the embedding
call. That makes re-indexing possible without re-embedding — at 20 provider
requests per day, re-embedding a resume to rebuild an index is not a casual
operation — and `embedded_at IS NULL` marks text the retriever cannot see.

`replace_for_resume` deletes then inserts rather than upserting by ordinal:
re-chunking can produce *fewer* pieces, and updating in place would leave the
previous run's tail behind as rows matching no part of the document.

### 2. EmbeddingService (`app/services/ai/embedding.py`)

```python
service = EmbeddingService(api_key=..., model="models/gemini-embedding-001")

vector  = await service.embed_text("Python expert")          # list[float], 3072-dim
vectors = await service.embed_batch(["...", "...", "..."])   # list[list[float]]
```

- **Redaction happens here**, not at the call sites, so a new caller cannot
  forget it. Same reasoning as `ModelClient`; see `AI_INTEGRATION.md` §8.
- **The cache sits inside this class**, keyed on the `sha256` of the **redacted**
  text plus the model. Hashing before redaction would put a fingerprint of the
  candidate's name and email in Redis, and would miss for two resumes differing
  only in redacted identifiers. The model is in the key, so changing
  `GEMINI_EMBEDDING_MODEL` is safe — old entries are never read again rather
  than silently mixed into an index where distances would mean nothing.
- **Token counts are absent, not zero.** Google returns no usage for embeddings,
  so `/health` reports `input_tokens: null` for the `embed` operation.
- **`models/embedding-001`, the previous default, was retired by Google** and
  returns 404 — behind the fallback layer, silently, which is how RAG here went
  weeks without producing a single embedding. Verify any replacement against
  `GET /v1beta/models` for the key in use.

### 3. ChromaVectorStore (`app/services/ai/vector_store.py`)

Drives **`langchain_chroma.Chroma`** over a shared `chromadb` client. Only the
transport lives here; `RAGService`, `HybridRetriever` and the benchmark are
written against this interface.

```python
store = get_vector_store()          # process-wide, CHROMA_PATH

await store.add_resume(resume_id=..., user_id=..., chunks=[...], embeddings=[...])
result = await store.retrieve_relevant(query_embedding=[...], resume_id=..., top_k=5)
await store.delete_resume(resume_id)
```

Three things that look like details and are not:

- **`retrieve_relevant` returns cosine *distances*, ascending — lower is
  better.** They come from `similarity_search_by_vector_with_relevance_scores`,
  whose name is a misnomer: it hands back Chroma's `distances` untouched. This
  matters because `RAG_MAX_DISTANCE` is a strict `<` in distance space, so a
  value flipped to a similarity keeps precisely the chunks the cutoff exists to
  drop, with no error anywhere.
- **Embeddings are computed upstream and carried in by
  `_PrecomputedEmbeddings`.** `Chroma.add_texts` has no parameter for
  precomputed vectors, but binding an embedding function to a process-wide store
  would freeze one user's redactor into everybody's indexing. The courier is
  constructed per call and computes nothing; its `embed_query` **raises**, since
  embedding there would be a provider call outside redaction, outside the cache,
  against 20 requests a day.
- **`delete_resume` filters on metadata, not an id range**, because a re-chunk
  can produce fewer pieces. It swallows its exceptions by design, so only a
  read-back proves the filter matched anything — which is what
  `tests/test_vector_store.py` is for.

`add_texts` upserts, where the raw `collection.add` it replaced errored on a
duplicate id, so re-indexing a resume now overwrites in place.

**`get_vector_store()` holds a lock, and it is not decoration.**
`chromadb.PersistentClient` is unsafe to build concurrently: two cold calls for
the same path race on chromadb's own registry and the losers see a half-started
system. Worse, the failing attempt stops the system the *winner* is using, so
RAG stays off for the rest of the process rather than recovering. `lru_cache`
does not help — it prevents repeat work after a call returns, not two threads
entering the body at once, and `get_rag_service` is a sync dependency that
FastAPI runs in the threadpool.

### 4. RAGService (`app/services/ai/rag.py`)

Dense-only orchestration: embedding plus the vector store.

```python
rag = get_rag_service()          # None when unavailable

ordinals = await rag.index_chunks(resume_id, user_id, chunks, redactor=redactor)
ranked   = await rag.retrieve_ranked(resume_id, query, top_k=5)   # list[str], best first
context  = await rag.retrieve_context(resume_id, query, top_k=5)  # the same, joined
await rag.delete_index(resume_id)
```

`index_chunks` takes **chunks, not raw text**: chunking is a pure function and
the rows belong to the caller's transaction, so `ResumeService` splits the text
and saves it while this stays free of a database session — it is cached
process-wide and could not hold one anyway.

`get_rag_service()` returns **`None`** when there is no API key or Chroma cannot
be opened, and records why in `retrieval_metrics` (`disabled_reason`). It is
`@lru_cache`d, so tests that vary settings must call
`get_rag_service.cache_clear()`.

### 5. HybridRetriever (`app/services/ai/retrieval.py`)

**The path a request actually takes.** Built per request in `deps.py`, because
its two halves have different lifetimes: the keyword half is a repository on the
request's session, the dense half is the process-wide `RAGService`.

```python
scored = await retriever.retrieve_scored(resume_id, query, top_k=5)
# [Scored(text=..., score=..., dense_rank=1, sparse_rank=3), ...]

context = await retriever.retrieve_context(resume_id, query, top_k=5)   # str wrapper
```

- **Dense** generalises and is bad at exact tokens (`gRPC`, a version number)
  that get averaged away inside a chunk.
- **Sparse** is Postgres full-text over `resume_chunks.search_vector`, a
  generated column, so nothing in the application maintains it. Query terms are
  ORed rather than ANDed: `plainto_tsquery` would require a chunk containing
  *every* word of "skills and experience relevant to Senior Backend Engineer",
  which is no chunk. Terms are tokenised in Python and bound as a parameter —
  `to_tsquery` has a syntax, and a candidate's answer will eventually contain a
  stray `&`.
- **Fusion** is Reciprocal Rank Fusion, not a weighted score. Cosine distance
  and `ts_rank` have different ranges and neither is calibrated, so any weighted
  sum invents an exchange rate; RRF keeps only the orderings, which is what each
  half is reliable about.

Sessionless callers — the evaluation worker — use `RAGService` directly and get
dense-only retrieval.

## Where it plugs in

**`ResumeService.upload()`** parses the file, saves `parsed_text`, chunks it,
saves the chunks as rows, then embeds them. Indexing is **non-blocking**: a
failure is logged and counted, and the upload still succeeds. `reprocess()`
re-runs it, which is affordable only because of the embedding cache.

**`GeminiQuestionGenerator._resume_context()`** is the single place that decides
what resume text reaches a prompt, and therefore the only place that sees every
route to a truncated-resume prompt — which is why `full_text_fallbacks` is
counted there:

```python
# A retriever, a rewritten query, a distance cutoff -- or the honest fallback.
context = await retriever.retrieve_context(resume_id, rewrite(role_or_exchange))
if not context:
    # `reason` distinguishes "no retriever" from "no resume" from "found nothing",
    # which is the difference between a misconfiguration and a bad query.
    retrieval_metrics.record_full_text_fallback(purpose=purpose, reason=reason)
    context = resume_text[:4000]
```

**The query is rewritten first, and that is not cosmetic.** Retrieval used to be
issued `"skills and experience relevant to {role}"` — four filler terms against
one real one — and, for follow-ups, the entire question plus the entire answer.
Filler hurts both halves differently: the keyword half ORs its terms, so common
words drag irrelevant chunks up, and the dense half averages the query into one
vector that filler pulls toward the centre. Measured on realistic follow-up
exchanges, rewriting moved **precision@1 from 0.75 to 1.00**.

`rewrite()` is deterministic — the obvious implementation spends one provider
request per retrieval against a ceiling of 20/day, undoing the embedding cache
in order to improve retrieval. It falls back to the original string when every
term is filler, because a bad query is bad but an empty one retrieves nothing
and the caller cannot tell those apart from the result.

## Configuration

RAG turns itself on when there is an API key and a writable Chroma path. There
is no separate enable flag.

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | *(unset)* | Unset ⇒ `get_rag_service()` returns `None`, `disabled_reason: no_api_key`. |
| `GEMINI_EMBEDDING_MODEL` | `models/gemini-embedding-001` | 3072-dim. Verify against `GET /v1beta/models` before changing. |
| `CHROMA_PATH` | `/var/lib/interviewpilot/chroma` | The `chroma_data` Docker volume. |
| `RAG_MAX_DISTANCE` | `1.0` | Strict `<`. 1.0 is exactly orthogonal, so `<=` would keep precisely the chunks the cutoff exists to remove. |
| `REDIS_URL` | *(unset)* | Enables the embedding cache. Strongly recommended. |
| `EMBEDDING_CACHE_TTL_SECONDS` | 30 days | |

**Outside Docker the default `CHROMA_PATH` is usually unwritable.** RAG then
logs a warning and disables itself — `disabled_reason: init_failed` — which is
the normal case when running the backend directly. Point it somewhere local:

```bash
CHROMA_PATH=./.chroma
```

## Cost

At the free tier the meaningful unit is **requests**, not dollars: 20 per day
for the whole account.

| | Requests |
|---|---|
| Indexing a resume | 1 per chunk — ~9 for the benchmark fixture; **0 on a cache hit** |
| One retrieval | 1 (embed the query). The keyword half is free. |
| Re-indexing unchanged text | **0**, with `REDIS_URL` set |

Two uploads used to exhaust the day, and `reprocess()` — which exists to rebuild
an index — was unaffordable. Vectors are cached as packed float32 (`array('f')`),
12 KiB for a 3072-dim vector against ~60 KiB as JSON; the precision loss is far
below what cosine similarity distinguishes.

Every cache failure is swallowed and counted as `cache_errors`, **separately
from `cache_misses`**: an unreachable cache behaves exactly like a cold one — the
request succeeds at full provider price — and reading the two as one number is
how you keep paying while believing the cache works.

## Testing

```bash
# The benchmark: one fixture resume, twelve queries, real in-memory Chroma,
# deterministic lexical embeddings. No network, no key.
pytest tests/api/test_retrieval_eval.py -v

# Unit tests for the store, the chunker, fusion, the cache and the counters.
pytest tests/test_vector_store.py tests/test_resume_chunker.py \
       tests/test_hybrid_retrieval.py tests/test_embedding_cache.py \
       tests/test_retrieval_metrics.py -v

# The halves that need a real Postgres / Redis.
pytest tests/api/test_hybrid_retrieval_api.py tests/api/test_embedding_cache_api.py -v

# Script-style probes. They print rather than assert and self-skip with no key.
python tests/test_rag_pipeline.py
```

**Baselines are floors.** Lexical queries pin the machinery at 1.00/1.00;
semantic ones sit at recall@3 0.50 / precision@1 0.33. Raise them when a change
earns it, and **never lower one to make a suite pass** — if a change drops a
score, either the change or the instrument is wrong, and both have happened here.

Three traps in that harness, all hit already:

- The hashing trick needs enough dimensions that collisions do not decide
  rankings. At 512 for a 229-token vocabulary, a 46-character chunk beat the
  paragraph that answered the query.
- Comparisons between pipeline versions must hold the embedder fixed, or they
  measure collision luck rather than retrieval.
- **Measurements without the distance cutoff are not reproducible.** A
  paraphrased query leaves several chunks at cosine distance exactly 1.0, and
  which of those ties lands in the top 3 varies between processes — the same
  code scored 0.50 or 0.67 run to run. Any new probe must apply
  `RAG_MAX_DISTANCE` the way the pipeline does.

## Troubleshooting

Retrieval degrades more quietly than anything else here: off, empty, or broken,
generation falls back to `resume_text[:4000]` and produces plausible questions
anyway. The interview works; it is just no longer personalised. `degradation.py`
does **not** count these — none of them is a provider failure. The `rag` block of
`GET /health` is where they show.

| Symptom | Check |
|---|---|
| Questions ignore the resume entirely | `rag.enabled`. `false` ⇒ read `disabled_reason`: `no_api_key`, or `init_failed` when `CHROMA_PATH` is unwritable. |
| `full_text_fallbacks` climbing, `retrievals` flat | Retrieval is never being reached — no resume on the session, or no chunks indexed. |
| `hits` climbing, `last_best_distance` near 1.0 | Retrieval is reached and returning junk. **A hit is not a success.** |
| Indexed 30 chunks, embedded 4 | Provider failed mid-run. The produced-vs-embedded gap is recorded in a `finally`; those rows have `embedded_at` NULL and that resume answers from a partial index for ever. Run `reprocess()`. |
| Every embedding call 404s | Retired embedding model. `GET /v1beta/models`. |
| `cache_errors` climbing | Redis unreachable. Requests still succeed, at full provider price. |
| Index empty after a restart | `CHROMA_PATH` is not on a persistent volume. |
| `'RustBindingsAPI' object has no attribute 'bindings'` | Concurrent cold construction of the Chroma client. The lock in `get_vector_store()` prevents this; if you see it, something built a client around it. |

Structured fields all go through `extra=`; `JsonFormatter` emits every
non-standard record attribute, so nothing needs registering.

## Still open

- **Swappable embedding providers.** A Chroma collection has fixed
  dimensionality and models disagree (3072 vs 1536 vs 768), so embeddings
  deliberately do **not** follow `AI_PROVIDER` — switching would raise on the
  first query against an existing index rather than degrade. Doing it properly
  means keying the collection name on the model so a switch starts a fresh
  index, plus re-embedding every resume against 20 requests a day.
- **Semantic chunking**, and richer indexing strategies (parent-document
  retrieval, hypothetical questions). Both cost provider calls per document.
- **Relevance feedback** — nothing currently records whether a retrieved chunk
  made the question better. `retrieval_metrics` measures reach, not quality; the
  benchmark measures quality, but offline.

`EnsembleRetriever` was evaluated and declined: it lives in `langchain` proper
and arrives with langgraph and three of its packages, to replace `fuse()`.

## References

- [ChromaDB](https://docs.trychroma.com/) · [`langchain-chroma`](https://python.langchain.com/docs/integrations/vectorstores/chroma/)
- [Gemini embeddings](https://ai.google.dev/gemini-api/docs/embeddings) · [`GET /v1beta/models`](https://generativelanguage.googleapis.com/v1beta/models)
- [Reciprocal Rank Fusion (Cormack et al., 2009)](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
- [Postgres full-text search](https://www.postgresql.org/docs/current/textsearch.html)
