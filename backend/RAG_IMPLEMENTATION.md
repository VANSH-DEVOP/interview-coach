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

`tests/test_retrieval_eval.py` scores a fixed resume against twelve queries
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

```
┌─────────────────────────────────────────────────────────────────┐
│ Resume Upload Flow                                              │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
                ┌───────────────────────┐
                │ ResumeService.upload()│
                │  - Store file         │
                │  - Parse PDF/DOCX     │
                └───────────┬───────────┘
                            │
                            ▼
                ┌───────────────────────────────────┐
                │ ResumeChunker.chunk()             │
                │  - Split on section headings      │
                │  - Pack paragraphs to ~800 chars  │
                └───────────┬───────────────────────┘
                            │
                            ▼
                ┌───────────────────────────────────┐
                │ resume_chunks (PostgreSQL)        │
                │  - text, section, ordinal         │
                │  - embedded_at NULL until indexed │
                └───────────┬───────────────────────┘
                            │
                            ▼
                ┌───────────────────────────────────┐
                │ RAGService.index_chunks()         │
                │  - Generate embeddings            │
                │  - Store in ChromaDB              │
                │  - Return embedded ordinals       │
                └───────────┬───────────────────────┘
                            │
                            ▼
                ┌──────────────────────┐
                │  ChromaDB Vector DB  │
                │ [Resume Embeddings]  │
                └──────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Question Generation Flow (with RAG)                             │
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
        ┌───────────────────────────────────────┐
        │ InterviewService.create()             │
        │ - Start interview session             │
        └───────────────┬───────────────────────┘
                        │
                        ▼
    ┌──────────────────────────────────────────────┐
    │ QuestionGenerator.initial_questions()       │
    │ - Receives: target_role, resume_id          │
    └──────────────────┬───────────────────────────┘
                       │
              ┌────────┴────────┐
              │                 │
              ▼                 ▼
         ┌─────────┐     ┌────────────────────┐
         │ No RAG  │     │ With RAG Enabled   │
         │         │     │                    │
         │ Static  │     │ 1. Create query:   │
         │ 3 Qs   │     │    "skills for X"  │
         └─────────┘     │                    │
                         │ 2. Embed query     │
                         │                    │
                         │ 3. Retrieve from   │
                         │    ChromaDB        │
                         │                    │
                         │ 4. Gemini uses     │
                         │    top-k chunks   │
                         │    to generate 5  │
                         │    tailored Qs    │
                         └────────────────────┘
```

## Components

### 1. ResumeChunker (`app/services/ai/rag.py`)

Splits resume text along the resume's own structure.

```python
chunks = ResumeChunker().chunk(resume_text)
# Returns: [Chunk(ordinal=0, section=None, content="Priya Raman\n..."),
#           Chunk(ordinal=1, section="SUMMARY", content="Backend engineer..."), ...]

chunks[1].retrieval_text   # "SUMMARY\nBackend engineer..." -- what gets embedded
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

**Where chunks live:** rows in `resume_chunks`, written before the embedding
call. That makes re-indexing possible without re-embedding — at 20 provider
requests per day, re-embedding a resume to rebuild an index is not a casual
operation — and `embedded_at IS NULL` marks text the retriever cannot see.

### 2. EmbeddingService (`app/services/ai/embedding.py`)

Generates vector embeddings using Google's Gemini embedding API.

```python
embedding_service = EmbeddingService(api_key="AIzaSyD...")

# Single embedding
embedding = await embedding_service.embed_text("Python expert")
# Returns: list[float] (3072-dimensional vector)

# Batch embeddings
embeddings = await embedding_service.embed_batch([
    "Python expert",
    "Backend engineer",
    "System design",
])
# Returns: list[list[float]]
```

**Features:**
- Uses `models/gemini-embedding-001`, overridable via `GEMINI_EMBEDDING_MODEL`
- 3072-dimensional vectors
- Note: the previous default, `models/embedding-001`, was retired by Google and
  returns HTTP 404. Verify any replacement against `GET /v1beta/models` for the
  key in use — a retired ID fails silently behind the fallback layer.
- Async/await support
- Batch processing with error tolerance
- Graceful error handling

### 3. VectorStore (`app/services/ai/vector_store.py`)

Persistent vector database using ChromaDB for semantic search.

```python
vector_store = ChromaVectorStore(persist_directory="/path/to/chroma")

# Add resume chunks
await vector_store.add_resume(
    resume_id=uuid.UUID("..."),
    user_id=uuid.UUID("..."),
    chunks=["chunk1", "chunk2", ...],
    embeddings=[[0.1, 0.2, ...], ...],
)

# Retrieve similar chunks
results = await vector_store.retrieve_relevant(
    query_embedding=[0.1, 0.2, ...],
    resume_id=uuid.UUID("..."),
    top_k=5,
)
# Returns: RetrievalResult with documents and distances
```

**Features:**
- Persistent storage (DuckDB backend)
- Cosine similarity for semantic search
- Per-resume indexing with metadata
- Automatic collection management

### 4. RAGService (`app/services/ai/rag.py`)

High-level orchestration of chunking, embedding, and retrieval.

```python
rag_service = RAGService(embedding_service, vector_store)

# Index a resume
chunk_count = await rag_service.index_resume(
    resume_id=uuid.UUID("..."),
    user_id=uuid.UUID("..."),
    resume_text=parsed_text,
)

# Retrieve context for a query
context = await rag_service.retrieve_context(
    resume_id=uuid.UUID("..."),
    query="experience with payment systems",
    top_k=5,
)
# Returns: concatenated string of top-5 relevant chunks
```

## Integration Points

### ResumeService

**Before (without RAG):**
```python
resume = Resume(
    parsed_text=parsed_text,
    status=ResumeStatus.PARSED,
)
await resumes.add(resume)
```

**After (with RAG):**
```python
resume = Resume(
    parsed_text=parsed_text,
    status=ResumeStatus.PARSED,
)
resume = await resumes.add(resume)

# NEW: Index for RAG
if rag_service and parsed_text:
    chunk_count = await rag_service.index_resume(
        resume.id, user_id, parsed_text
    )
```

### GeminiQuestionGenerator

**Before (static truncation):**
```python
resume_block = f"\nCandidate resume excerpt:\n{resume_text[:4000]}"

prompt = f"Generate 5 questions for {role}. Resume: {resume_block}"
```

**After (RAG-enhanced):**
```python
if rag_service and resume_id:
    query = f"skills and experience relevant to {role}"
    context = await rag_service.retrieve_context(
        resume_id, query, top_k=5
    )
    resume_context = f"...[most relevant sections]..."
else:
    resume_context = f"...[first 4000 chars]..."

prompt = f"Generate 5 questions for {role}. Resume: {resume_context}"
```

**Result:** Questions focus on the **most relevant** skills/experience, not just the first 4KB.

## How RAG Improves Question Generation

### Example: Backend Engineer Position

**Resume Content:**
- Worked at 5 different companies
- 10+ years experience
- Skills: Python, Java, Go, Rust, JavaScript
- Experience with databases, caching, messaging queues
- Recent focus on Kubernetes and microservices

**Without RAG (first 4KB):**
- Generic questions about general background
- May miss domain expertise deeper in resume

**With RAG:**
1. **Query**: "What backend technologies and tools has this candidate used?"
2. **Retrieved Chunks:**
   - "Implemented message queues with Kafka"
   - "Optimized database queries, reduced latency 60%"
   - "Deployed microservices to Kubernetes"
   - "Built distributed systems handling 1M req/sec"
3. **Generated Questions:**
   - "Tell me about your experience with message brokers like Kafka"
   - "Describe how you've optimized database performance"
   - "What's your experience with Kubernetes and containerization?"
   - "Walk me through a high-scale distributed system you've built"
   - "How do you approach system design for scalability?"

**Result:** All 5 questions directly target the candidate's actual expertise!

## Configuration

### Enable RAG

Set `GEMINI_API_KEY` in `.env`:

```bash
GEMINI_API_KEY=AIzaSyD...
```

RAG will **automatically enable** when:
1. `GEMINI_API_KEY` is configured
2. Resume is uploaded and parsed
3. Interview is created with resume context

### Storage Configuration

ChromaDB persists to `/tmp/interviewpilot/chroma` by default (created automatically).

To change:

```python
# In deps.py
vector_store = get_vector_store(
    persist_directory=Path("/custom/path/to/chroma")
)
```

## Performance & Costs

### Chunking
- Splits resume into ~3-5 chunks (typical resume)
- No external API calls required

### Embeddings
- ~3-5 API calls per resume upload (one per chunk)
- ~$0.0001-0.0002 per resume

### Retrieval
- ~1 API call per question generation (embed query)
- ~$0.00002 per question

### Total Cost Per Interview
- Indexing: $0.0001-0.0002
- 5 questions: $0.0001
- Evaluation: $0.0005
- **Total: ~$0.0007** (~0.07¢ per interview)

## Testing

### Run RAG Tests

```bash
cd backend
python tests/test_rag_pipeline.py
```

**Output:**
```
✅ TEST 1: Text Chunking
   - Split resume into N chunks

✅ TEST 2: Vector Store (ChromaDB)
   - Add chunks to vector store
   - Retrieve similar documents

✅ TEST 3: Embedding Service
   - (Skipped if GEMINI_API_KEY not set)
   - Generate embeddings for texts

✅ TEST 4: Full RAG Pipeline
   - (Skipped if GEMINI_API_KEY not set)
   - Index resume, retrieve context
```

### Integration Tests

```bash
pytest tests/test_resume_parser.py -v
pytest tests/ -v  # All tests
```

## Troubleshooting

### RAG Not Activating

**Check:**
1. `GEMINI_API_KEY` is set in `.env`
2. Resume was parsed successfully (`status=PARSED`)
3. Backend restarted after setting API key

**Debug:**
- Check logs: `docker-compose logs backend | grep -i rag`
- Verify ChromaDB dir exists: `/tmp/interviewpilot/chroma`

### Slow Question Generation

**Cause:** Embedding API latency (usually <2s)

**Solutions:**
- Check rate limits: Google Cloud Console
- Cache embeddings for identical resumes
- Use `gemini-1.5-flash` (default, faster)

### Out of Memory

**Cause:** ChromaDB loading large indices

**Solutions:**
- Increase container memory: `docker-compose.yml`
- Reduce chunk size: `TextChunker.chunk_text(chunk_size=300)`
- Clear old indices: `rm -rf /tmp/interviewpilot/chroma`

## Future Enhancements

1. **Caching**: Reuse embeddings for identical resumes
2. **Filtering**: Filter questions by skill tags
3. **Streaming**: Real-time question generation with streaming
4. **Model Selection**: Support Claude, GPT-4 embeddings
5. **Semantic Chunking**: Use sentence-transformers for smarter splits
6. **Indexing Strategies**: Parent-document retrieval, hypothetical questions
7. **Metrics**: Track question relevance, coverage

## References

- **ChromaDB**: https://docs.trychroma.com/
- **Gemini Embeddings**: https://ai.google.dev/
- **RAG Pattern**: https://docs.anthropic.com/en/docs/build-a-system#retrieval-augmented-generation
