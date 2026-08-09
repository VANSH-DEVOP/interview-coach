import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import (
    CurrentUser,
    EvaluationQueueDep,
    get_interview_service,
    limit_by_user,
)
from app.models.interview_session import SessionStatus
from app.schemas.common import Page, PageParams
from app.schemas.interview import (
    AnswerCreate,
    AnswerRead,
    InterviewCreate,
    InterviewDetail,
    InterviewRead,
    QuestionRead,
)
from app.schemas.report import ReportRead
from app.services.interview_service import InterviewService

router = APIRouter(prefix="/interviews", tags=["interviews"])

InterviewSvc = Annotated[InterviewService, Depends(get_interview_service)]

# Applied to every route that costs a Gemini call. Reads are not limited.
AiRateLimit = Depends(limit_by_user("ai"))


@router.post(
    "",
    response_model=InterviewRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AiRateLimit],
)
async def create_interview(
    payload: InterviewCreate, current_user: CurrentUser, interviews: InterviewSvc
) -> InterviewRead:
    session = await interviews.create(current_user.id, payload)
    return InterviewRead.model_validate(session)


@router.get("", response_model=Page[InterviewRead])
async def list_interviews(
    current_user: CurrentUser,
    interviews: InterviewSvc,
    params: Annotated[PageParams, Depends()],
    status_filter: Annotated[SessionStatus | None, Query(alias="status")] = None,
) -> Page[InterviewRead]:
    items, total = await interviews.list(
        current_user.id, status=status_filter, offset=params.offset, limit=params.size
    )
    return Page(
        items=[InterviewRead.model_validate(s) for s in items],
        total=total,
        page=params.page,
        size=params.size,
    )


@router.get("/{session_id}", response_model=InterviewDetail)
async def get_interview(
    session_id: uuid.UUID, current_user: CurrentUser, interviews: InterviewSvc
) -> InterviewDetail:
    session = await interviews.get_detail(session_id, current_user.id)
    return InterviewDetail.model_validate(session)


@router.post(
    "/{session_id}/answers",
    response_model=AnswerRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[AiRateLimit],
)
async def submit_answer(
    session_id: uuid.UUID,
    payload: AnswerCreate,
    current_user: CurrentUser,
    interviews: InterviewSvc,
) -> AnswerRead:
    answer = await interviews.submit_answer(session_id, current_user.id, payload)
    return AnswerRead.model_validate(answer)


@router.post("/{session_id}/questions/{question_id}/skip", response_model=QuestionRead)
async def skip_question(
    session_id: uuid.UUID,
    question_id: uuid.UUID,
    current_user: CurrentUser,
    interviews: InterviewSvc,
) -> QuestionRead:
    """Pass over a question without answering it. No AI call, so no AI limit."""
    question = await interviews.skip_question(session_id, current_user.id, question_id)
    return QuestionRead.model_validate(question)


@router.put(
    "/{session_id}/answers",
    response_model=AnswerRead,
    dependencies=[AiRateLimit],
)
async def update_answer(
    session_id: uuid.UUID,
    payload: AnswerCreate,
    current_user: CurrentUser,
    interviews: InterviewSvc,
) -> AnswerRead:
    """Replace an answer, regenerating any follow-up it produced."""
    answer = await interviews.update_answer(session_id, current_user.id, payload)
    return AnswerRead.model_validate(answer)


@router.post(
    "/{session_id}/regenerate-questions",
    response_model=InterviewDetail,
    dependencies=[AiRateLimit],
)
async def regenerate_questions(
    session_id: uuid.UUID, current_user: CurrentUser, interviews: InterviewSvc
) -> InterviewDetail:
    """Swap the question set for a freshly generated one. Only before answering."""
    session = await interviews.regenerate_questions(session_id, current_user.id)
    return InterviewDetail.model_validate(session)


@router.post("/{session_id}/abandon", response_model=InterviewRead)
async def abandon_interview(
    session_id: uuid.UUID, current_user: CurrentUser, interviews: InterviewSvc
) -> InterviewRead:
    """Stop an interview without evaluating it. Keeps the transcript."""
    session = await interviews.abandon(session_id, current_user.id)
    return InterviewRead.model_validate(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_interview(
    session_id: uuid.UUID, current_user: CurrentUser, interviews: InterviewSvc
) -> Response:
    """Permanently delete a session, its transcript, and its report."""
    await interviews.delete(session_id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{session_id}/complete",
    response_model=InterviewRead,
    dependencies=[AiRateLimit],
)
async def complete_interview(
    session_id: uuid.UUID,
    queue: EvaluationQueueDep,
    current_user: CurrentUser,
    interviews: InterviewSvc,
) -> InterviewRead:
    """End the interview. The report is generated afterwards, out of band.

    Returns as soon as the session is closed and a PENDING report exists; the
    client polls /reports/by-session/{id} until it leaves PENDING/GENERATING.
    """
    session = await interviews.complete(session_id, current_user.id)
    # Handed off rather than awaited, so the request does not wait on the
    # provider. The job opens its own database session.
    await queue.enqueue(session_id, current_user.id)
    return InterviewRead.model_validate(session)


@router.post(
    "/{session_id}/reevaluate",
    response_model=ReportRead,
    dependencies=[AiRateLimit],
)
async def reevaluate_interview(
    session_id: uuid.UUID,
    queue: EvaluationQueueDep,
    current_user: CurrentUser,
    interviews: InterviewSvc,
) -> ReportRead:
    """Queue a fresh evaluation, replacing the existing report in place.

    Also the retry path for a FAILED report. Returns the PENDING report.
    """
    report = await interviews.reevaluate(session_id, current_user.id)
    await queue.enqueue(session_id, current_user.id)
    return ReportRead.model_validate(report)
