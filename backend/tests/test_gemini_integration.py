"""Test Gemini question generation and evaluation with a mock interview."""

import asyncio

from app.services.ai.gemini_client import GeminiClient, GeminiError
from app.services.ai.evaluator import QAPair


async def test_gemini_question_generation():
    """Test generating interview questions using Gemini."""
    print("\n=== Testing Gemini Question Generation ===\n")
    
    # This will only work if GEMINI_API_KEY is set in .env
    from app.core.config import get_settings
    settings = get_settings()
    
    if not settings.GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY not configured. Skipping test.")
        print("   To enable: set GEMINI_API_KEY in .env and restart backend")
        return
    
    # Initialize Gemini client
    client = GeminiClient(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
    
    # Test with sample resume
    sample_resume = """
    John Doe
    Senior Software Engineer
    
    Experience:
    - 5 years designing microservices architectures
    - Led team of 3 engineers building payment systems
    - Expertise in Python, Go, PostgreSQL, Redis
    
    Skills:
    - Backend engineering
    - System design
    - API design
    """
    
    print("Resume excerpt:")
    print(sample_resume)
    print("\n")
    
    # Generate questions
    from app.services.ai.gemini import GeminiQuestionGenerator
    generator = GeminiQuestionGenerator(client)
    
    try:
        questions = await generator.initial_questions(
            target_role="Senior Backend Engineer",
            resume_text=sample_resume
        )
        
        print(f"✅ Generated {len(questions)} questions:\n")
        for i, q in enumerate(questions, 1):
            print(f"{i}. [{q.question_type}] {q.content}")
            print(f"   Metadata: {q.metadata}")
            print()
        
        # Test follow-up question
        if questions:
            first_q = questions[0]
            sample_answer = (
                "I architected a microservices system for payment processing. "
                "We used Python with FastAPI, PostgreSQL for transactions, and Redis for caching. "
                "This reduced latency by 40% and improved throughput by 3x."
            )
            
            print(f"Testing follow-up to: {first_q.content}")
            print(f"Sample answer: {sample_answer}\n")
            
            follow_up = await generator.follow_up(
                question=first_q.content,
                answer=sample_answer,
                resume_text=sample_resume
            )
            
            if follow_up:
                print("✅ Follow-up generated:")
                print(f"   {follow_up.content}")
            else:
                print("ℹ️  No follow-up needed for this answer.")
    
    except GeminiError as e:
        print(f"❌ Gemini error: {e}")
        print("   Check your API key and rate limits")


async def test_gemini_evaluation():
    """Test evaluating interview answers using Gemini."""
    print("\n=== Testing Gemini Answer Evaluation ===\n")
    
    from app.core.config import get_settings
    settings = get_settings()
    
    if not settings.GEMINI_API_KEY:
        print("⚠️  GEMINI_API_KEY not configured. Skipping test.")
        return
    
    # Initialize Gemini client and evaluator
    client = GeminiClient(settings.GEMINI_API_KEY, settings.GEMINI_MODEL)
    from app.services.ai.evaluator import GeminiEvaluator
    evaluator = GeminiEvaluator(client)
    
    # Sample interview transcript
    transcript = [
        QAPair(
            question="Tell me about yourself and your professional background.",
            answer=(
                "I'm a software engineer with 5 years of experience building backend systems. "
                "I started as a junior developer and progressed to senior engineer. "
                "I'm passionate about system design and mentoring junior engineers."
            )
        ),
        QAPair(
            question="Describe a challenging project you worked on.",
            answer=(
                "I led a team to migrate our monolith to microservices. "
                "It was complex due to data consistency challenges. "
                "We used event sourcing and achieved 99.99% uptime."
            )
        ),
        QAPair(
            question="Walk me through how you would design a payment system.",
            answer=(
                "I'd use event sourcing for immutability, "
                "separate services for payments/reconciliation, "
                "strong consistency guarantees, and comprehensive logging."
            )
        ),
    ]
    
    print("Interview Transcript:")
    for i, qa in enumerate(transcript, 1):
        print(f"\nQ{i}: {qa.question}")
        print(f"A{i}: {qa.answer}")
    
    print("\n" + "="*60)
    print("Evaluating...")
    print("="*60 + "\n")
    
    try:
        result = await evaluator.evaluate(
            target_role="Senior Backend Engineer",
            transcript=transcript
        )
        
        print(f"Overall Score: {result.overall_score}/10")
        print("\n✅ Strengths:")
        for s in result.strengths:
            print(f"   • {s}")
        
        print("\n⚠️  Weaknesses/Areas for Improvement:")
        for w in result.weaknesses:
            print(f"   • {w}")
        
        print("\n📋 Detailed Feedback:")
        if result.detailed_feedback.get("recommendations"):
            print("   Recommendations:")
            for rec in result.detailed_feedback["recommendations"]:
                print(f"     • {rec}")
        
        if result.detailed_feedback.get("per_question"):
            print("   Per-Question Feedback:")
            for i, feedback in enumerate(result.detailed_feedback["per_question"], 1):
                if isinstance(feedback, dict):
                    print(f"     Q{i}: {feedback.get('feedback', 'N/A')}")
    
    except GeminiError as e:
        print(f"❌ Gemini error: {e}")
        print("   Check your API key and rate limits")


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("InterviewPilot AI Integration Test")
    print("="*60)
    
    await test_gemini_question_generation()
    await test_gemini_evaluation()
    
    print("\n" + "="*60)
    print("✅ Tests complete!")
    print("="*60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
