# RAG (Retrieval-Augmented Generation) Implementation

## Overview

InterviewPilot now implements **Retrieval-Augmented Generation (RAG)** for intelligent interview question generation. When a candidate uploads a resume, the system:

1. **Parses** the PDF/DOCX into text
2. **Chunks** the text into semantic segments
3. **Embeds** each chunk using Gemini's embedding API
4. **Stores** embeddings in ChromaDB vector database
5. **Retrieves** the most relevant sections when generating questions
6. **Generates** tailored questions using the retrieved context

This ensures questions are **personalized** to the candidate's actual experience, skills, and background.

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
                │ RAGService.index_resume()         │
                │  - Chunk text                     │
                │  - Generate embeddings            │
                │  - Store in ChromaDB              │
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

### 1. TextChunker (`app/services/ai/rag.py`)

Splits resume text into semantic chunks with configurable overlap for context continuity.

```python
chunks = TextChunker.chunk_text(
    text=resume_text,
    chunk_size=500,      # Characters per chunk
    overlap=100,          # Overlap between chunks
)
# Returns: ["Experience in systems...", "Led 3 engineers...", ...]
```

**Features:**
- Paragraph-aware splitting for semantic integrity
- Configurable chunk size and overlap
- Removes redundant whitespace
- Handles edge cases gracefully

### 2. EmbeddingService (`app/services/ai/embedding.py`)

Generates vector embeddings using Google's Gemini embedding API.

```python
embedding_service = EmbeddingService(api_key="AIzaSyD...")

# Single embedding
embedding = await embedding_service.embed_text("Python expert")
# Returns: list[float] (768-dimensional vector)

# Batch embeddings
embeddings = await embedding_service.embed_batch([
    "Python expert",
    "Backend engineer",
    "System design",
])
# Returns: list[list[float]]
```

**Features:**
- Uses `models/embedding-001` (Gemini's embedding model)
- 768-dimensional vectors
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
