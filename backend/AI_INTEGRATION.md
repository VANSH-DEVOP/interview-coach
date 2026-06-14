# AI Integration Guide

## Overview

InterviewPilot AI integrates with Google Gemini for intelligent question generation and interview evaluation. The system is designed with graceful degradation—if Gemini is unavailable or not configured, the application falls back to deterministic, built-in implementations.

## Architecture

### Question Generation Pipeline

```
User uploads resume + starts interview
    ↓
ResumeService.parse() extracts text from PDF/DOCX
    ↓
InterviewService.create() instantiates interview
    ↓
QuestionGenerator.initial_questions(target_role, resume_text)
    ├─ If GEMINI_API_KEY set: GeminiQuestionGenerator (5 tailored questions)
    └─ Else: StaticQuestionGenerator (3 default questions)
    ↓
Questions saved to database with metadata
    ↓
Interview begins
```

### Adaptive Follow-ups

```
User submits answer
    ↓
InterviewService.submit_answer() records response
    ↓
QuestionGenerator.follow_up(question, answer, resume_text)
    ├─ If GEMINI_API_KEY set: GeminiQuestionGenerator decides & generates
    └─ Else: returns None (no follow-up)
    ↓
If follow_up exists, new Question added to session
    ↓
User sees follow-up question
```

### Evaluation Pipeline

```
User completes interview
    ↓
InterviewService.complete_session() calls evaluator
    ↓
Evaluator.evaluate(target_role, QA_transcript)
    ├─ If GEMINI_API_KEY set: GeminiEvaluator (AI-powered scoring)
    └─ Else: HeuristicEvaluator (deterministic fallback)
    ↓
EvaluationReport stored with:
  - overall_score (0-10)
  - strengths (list)
  - weaknesses (list)
  - detailed_feedback (per-question annotations)
    ↓
Report returned to frontend
```

## Configuration

### Getting a Gemini API Key

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Click **"Create API Key"** (or select an existing one)
3. Copy the API key (example: `AIzaSyD...`)
4. Add to `.env`:
   ```bash
   GEMINI_API_KEY=AIzaSyD...
   GEMINI_MODEL=gemini-1.5-flash
   ```
5. Restart the backend:
   ```bash
   docker-compose restart backend
   # or
   cd backend && python -m uvicorn app.main:app --reload
   ```

### Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `GEMINI_API_KEY` | No | (empty) | Leave empty to use fallback |
| `GEMINI_MODEL` | No | `gemini-1.5-flash` | Model to use; alternatives: `gemini-1.5-pro`, `gemini-2.0-flash` |

## Implementation Details

### Question Generation

**GeminiQuestionGenerator** calls Gemini with:
- **System instruction**: "You are an expert technical interviewer..."
- **Prompt**: Role, resume excerpt (first 4000 chars), request for exactly 5 questions
- **Response format**: JSON with question content and type (behavioral/technical)
- **Timeout**: 30 seconds
- **Fallback**: Returns `StaticQuestionGenerator` questions on failure

**Follow-up Logic**:
- Analyzes the user's answer to the previous question
- Decides if a probing follow-up would add value
- Returns a single `follow_up` question or `None`
- Stored in database with `question_type = "follow_up"` and parent question link

### Answer Evaluation

**GeminiEvaluator** receives:
- Interview target role
- Full question/answer transcript
- Optional resume context (passed but not used in evaluation)

**Gemini is asked to return**:
```json
{
  "overall_score": 7.5,
  "strengths": ["Clear explanations", "Good examples"],
  "weaknesses": ["Lacked quantified metrics", "Could mention team impact"],
  "recommendations": ["Use STAR method", "Research company before interview"],
  "per_question": [
    {"question": "...", "feedback": "..."},
    ...
  ]
}
```

**Post-processing**:
- Clamps score to 0-10 range
- Handles field name variations (e.g., "weaknesses" vs "areas_for_improvement")
- Ensures non-perfect scores always have actionable weaknesses

### Fallback Behavior

**Without Gemini API Key**:

| Feature | Fallback |
|---------|----------|
| Initial Questions | 3 hardcoded behavioral/technical questions |
| Follow-ups | Disabled (always returns `None`) |
| Evaluation | Heuristic scoring based on coverage (answered %) + depth (avg words) |
| Scored 0-10 | Yes, deterministic |
| Feedback | Generic recommendations |

The fallback is **non-functional** (not random; repeatable), so the interview flow always works and users always see a report.

## Testing the Integration

### 1. Verify Fallback Works (No API Key)

```bash
# Ensure .env has GEMINI_API_KEY=
docker-compose up -d
curl -X POST http://localhost:8000/api/v1/interviews \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "Test", "target_role": "Senior Engineer"}'
```

**Expected**: Interview created with 3 static questions; evaluation uses heuristic.

### 2. Enable Gemini (With API Key)

```bash
# Update .env with your GEMINI_API_KEY
GEMINI_API_KEY=AIzaSyD...
docker-compose restart backend
```

Create interview again. **Expected**: 5 tailored questions based on role + resume context.

### 3. Check Question Generation Logs

```bash
docker-compose logs backend | grep -i gemini
# or
docker-compose logs backend | grep -i "parsed resume"
```

### 4. Test Adaptive Follow-ups

Submit an answer in an active interview:

```bash
curl -X POST http://localhost:8000/api/v1/interviews/{session_id}/answers \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "question_id": "<question_id>",
    "content": "I designed a microservices architecture...",
    "duration_seconds": 120
  }'
```

**With Gemini**: Watch the backend logs for a new follow-up question being generated.  
**Without Gemini**: No follow-up question added.

### 5. Complete Interview & Check Report

```bash
curl -X POST http://localhost:8000/api/v1/interviews/{session_id}/complete \
  -H "Authorization: Bearer <token>"
```

Fetch report:

```bash
curl -X GET http://localhost:8000/api/v1/reports \
  -H "Authorization: Bearer <token>"
```

**With Gemini**: Report includes nuanced feedback, per-question scoring.  
**Without Gemini**: Report includes generic heuristic evaluation.

## Pricing & Quotas

### Google Gemini API

- **Free tier**: 60 requests/minute, 1,500 requests/day (quotas reset daily)
- **Paid**: $0.075/million input tokens, $0.30/million output tokens (1.5-flash is cheaper; pro is more accurate)
- **Monitor usage**: [Google Cloud Console](https://console.cloud.google.com/)

### Cost Estimate

Per interview session (5 questions + evaluation):
- Question generation: ~0.5 questions × 3 API calls = ~1.5 KB input, 2 KB output ≈ $0.0001
- Evaluation: 1 call with full transcript ≈ 5 KB input, 1 KB output ≈ $0.0005
- **Total: ~$0.0006/interview** (roughly 0.06¢)

## Troubleshooting

### Gemini API Key Not Recognized

**Symptom**: Backend starts but falls back to static questions.

**Fix**:
```bash
# Verify .env is in root (interview-coach/.env, not backend/.env)
cat .env | grep GEMINI_API_KEY

# Restart backend to reload env
docker-compose restart backend
```

### Gemini Rate Limited

**Symptom**: Errors in logs like `HTTP 429: Too Many Requests`

**Fix**:
- Free tier: 60 req/min limit. Upgrade to paid API or wait.
- Paid tier: Check Cloud Console quotas; may need to increase limits.

### Gemini Returns Malformed JSON

**Symptom**: `GeminiError: Unexpected Gemini response shape`

**Fix**:
- Check backend logs for response details
- May indicate model instability; use `gemini-1.5-flash` (more stable)
- Fallback ensures interview still completes

### Interview Not Using Parsed Resume

**Symptom**: Questions are generic, not tailored to resume

**Fix**:
- Ensure resume was uploaded and parsed before starting interview
- Check `Resume.status` in database (should be `parsed`)
- Verify `Resume.parsed_text` is populated

## Future Enhancements

1. **ChromaDB Integration**: Store parsed resumes in vector DB for semantic search
2. **LangGraph Orchestration**: Chain multiple LLM calls (extract skills → generate → refine)
3. **Streaming Responses**: Real-time question/evaluation generation
4. **Model Swap**: Support Claude, GPT-4, open-source models
5. **Caching**: Cache generated questions for identical roles + resume profiles
6. **Rate Limiting**: Per-user, per-role question generation quotas

## References

- [Google AI Studio](https://aistudio.google.com)
- [Gemini API Docs](https://ai.google.dev/gemini-api/)
- [Pricing](https://ai.google.dev/pricing)
