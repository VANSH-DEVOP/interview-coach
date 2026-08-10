"""Test RAG pipeline with resume chunking, embedding, and retrieval."""

import asyncio
import logging
import uuid

from app.services.ai.embedding import EmbeddingService
from app.services.ai.rag import RAGService, ResumeChunker
from app.services.ai.vector_store import ChromaVectorStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_text_chunking():
    """Test resume text chunking."""
    print("\n" + "="*70)
    print("TEST 1: Text Chunking")
    print("="*70)
    
    sample_resume = """
    John Doe
    Senior Software Engineer
    
    Professional Summary
    Experienced backend engineer with 5 years building scalable microservices.
    Strong expertise in Python, Go, and system design. Led teams of 3-5 engineers.
    
    Experience
    
    Senior Backend Engineer at TechCorp (2022-Present)
    - Architected microservices platform handling 1M requests/second
    - Led team of 3 engineers; mentored 2 junior developers
    - Reduced API latency by 60% through caching optimization
    - Implemented event sourcing for payment processing system
    
    Backend Engineer at StartupXYZ (2020-2022)
    - Built REST APIs serving 100K+ users
    - Designed PostgreSQL schemas for high-concurrency systems
    - Implemented Redis caching layer; improved throughput by 3x
    
    Skills
    Languages: Python, Go, JavaScript, SQL
    Databases: PostgreSQL, Redis, MongoDB
    Systems: Docker, Kubernetes, AWS, GCP
    Concepts: Microservices, System Design, Event Sourcing, API Design
    
    Education
    BS Computer Science, State University (2018)
    """
    
    chunks = ResumeChunker().chunk(sample_resume)
    print(f"\n✅ Split resume into {len(chunks)} chunks:\n")
    for chunk in chunks:
        print(f"Chunk {chunk.ordinal} [{chunk.section}] ({len(chunk.content)} chars):")
        print(f"  {chunk.content[:100]}...")
        print()


async def test_vector_store():
    """Test ChromaDB vector store operations."""
    print("\n" + "="*70)
    print("TEST 2: Vector Store (ChromaDB)")
    print("="*70)
    
    # Create in-memory vector store for testing
    store = ChromaVectorStore(persist_directory=None)
    print("✅ ChromaDB initialized\n")
    
    # Sample data
    resume_id = uuid.uuid4()
    user_id = uuid.uuid4()
    documents = [
        "Backend engineer with 5 years experience in microservices",
        "Expert in Python, Go, and system design",
        "Led teams implementing payment processing systems",
        "Specialized in Redis caching and query optimization",
    ]
    
    # Create sample embeddings (same dimension as Gemini: 768)
    import numpy as np
    embeddings = [
        np.random.randn(768).tolist() for _ in documents
    ]
    
    # Add documents
    await store.add_resume(resume_id, user_id, documents, embeddings)
    print(f"✅ Added {len(documents)} chunks to vector store\n")
    
    # Retrieve similar documents
    query_embedding = np.random.randn(768).tolist()
    results = await store.retrieve_relevant(query_embedding, resume_id, top_k=2)
    
    print(f"✅ Retrieved {len(results.documents)} similar documents:\n")
    for i, (doc, dist) in enumerate(zip(results.documents, results.distances), 1):
        print(f"Result {i} (distance: {dist:.4f}):")
        print(f"  {doc}")
        print()


async def test_embedding_service():
    """Test embedding generation with Gemini (requires API key)."""
    print("\n" + "="*70)
    print("TEST 3: Embedding Service")
    print("="*70)
    
    from app.core.config import get_settings
    settings = get_settings()
    
    if not settings.GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY not configured. Skipping embedding test.")
        print("   Set GEMINI_API_KEY in .env to enable this test.\n")
        return
    
    try:
        embedding_service = EmbeddingService(settings.GEMINI_API_KEY)
        print("✅ EmbeddingService initialized\n")
        
        test_texts = [
            "Senior backend engineer with microservices experience",
            "Python and Go expert",
            "Led payment processing systems",
        ]
        
        embeddings = await embedding_service.embed_batch(test_texts)
        
        print(f"✅ Generated embeddings for {len(test_texts)} texts:\n")
        for text, embedding in zip(test_texts, embeddings):
            if embedding:
                print(f"Text: {text}")
                print(f"  Embedding dimension: {len(embedding)}")
                print(f"  First 5 values: {embedding[:5]}")
                print()
        
    except Exception as e:
        print(f"❌ Embedding test failed: {e}")
        print("   Check your Gemini API key and rate limits\n")


async def test_rag_pipeline():
    """Test full RAG pipeline."""
    print("\n" + "="*70)
    print("TEST 4: Full RAG Pipeline")
    print("="*70)
    
    from app.core.config import get_settings
    settings = get_settings()
    
    if not settings.GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY not configured. Skipping RAG test.")
        print("   Set GEMINI_API_KEY in .env to enable this test.\n")
        return
    
    try:
        # Initialize services
        embedding_service = EmbeddingService(settings.GEMINI_API_KEY)
        vector_store = ChromaVectorStore(persist_directory=None)
        rag_service = RAGService(embedding_service, vector_store)
        
        # Sample resume
        sample_resume = """
        Alice Chen
        Principal Engineer
        
        Experience:
        - 8 years building distributed systems at Meta and Google
        - Designed payment processing pipeline handling 10M transactions/day
        - Expert in Python, Rust, C++
        - Implemented event sourcing architecture
        - Led team of 5 engineers on microservices migration
        
        Skills: System Design, Distributed Systems, Python, Rust, C++, 
        Payment Systems, Event Sourcing, Kubernetes, PostgreSQL
        """
        
        resume_id = uuid.uuid4()
        user_id = uuid.uuid4()
        
        print(f"Resume to index:\n{sample_resume[:200]}...\n")
        
        # Index resume
        chunks = ResumeChunker().chunk(sample_resume)
        embedded = await rag_service.index_chunks(resume_id, user_id, chunks)
        print(f"✅ Indexed resume: {len(embedded)}/{len(chunks)} chunks embedded\n")
        
        # Retrieve relevant context for various queries
        queries = [
            "What payment systems experience does the candidate have?",
            "What languages does the candidate know?",
            "Tell me about their distributed systems background",
        ]
        
        for query in queries:
            print(f"Query: {query}")
            context = await rag_service.retrieve_context(resume_id, query, top_k=2)
            print(f"Retrieved Context:\n{context[:200]}...\n")
        
        print("✅ RAG pipeline test complete!")
        
    except Exception as e:
        print(f"❌ RAG pipeline test failed: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("InterviewPilot RAG Integration Tests")
    print("="*70)
    
    await test_text_chunking()
    await test_vector_store()
    await test_embedding_service()
    await test_rag_pipeline()
    
    print("\n" + "="*70)
    print("✅ All tests complete!")
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
