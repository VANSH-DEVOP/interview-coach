"""Evaluation of a completed interview, off the request path.

Evaluation is a provider round-trip that can take many seconds, so it must not
sit inside the request that ends the interview. `complete()` writes a PENDING
report and hands the work off (see `app/services/job_queue.py`), which walks
the report through GENERATING to COMPLETED or FAILED while the client polls.

This module deliberately breaks the one-session-per-request rule that holds
everywhere else: by the time it runs, the request's session is closed. It opens
and commits its own.

Two entry points, because the two runners want opposite things from a failure:

- `evaluate` raises. The arq worker wants that -- a raised exception is what
  triggers a retry with backoff, and only the final attempt should write FAILED.
- `run_evaluation` never raises, and marks the report FAILED itself. The
  in-process fallback wants that: an unhandled exception in a BackgroundTask
  vanishes into the event loop, leaving the report stuck on GENERATING with
  nothing written anywhere and nothing to retry it.
"""

import logging
import uuid

from sqlalchemy import select, update

from app.db.session import AsyncSessionFactory
from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.repositories.interview_repository import InterviewRepository
from app.repositories.report_repository import ReportRepository
from app.repositories.user_repository import UserRepository
from app.services.ai.evaluator import QAPair, get_evaluator
from app.services.ai.masking import redactor_for

logger = logging.getLogger(__name__)


async def run_evaluation(session_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Evaluate and write the report, marking it FAILED on error. Never raises.

    The single-attempt path, used when there is no queue behind the handoff.
    """
    try:
        await evaluate(session_id, user_id)
    except Exception:
        logger.exception("Evaluation failed for session %s.", session_id)
        await mark_failed(session_id)


async def evaluate(session_id: uuid.UUID, user_id: uuid.UUID) -> None:
    async with AsyncSessionFactory() as db:
        interviews = InterviewRepository(db)
        reports = ReportRepository(db)

        users = UserRepository(db)

        session = await interviews.get_owned(session_id, user_id, with_questions=True)
        report = await reports.get_owned_by_session(session_id, user_id)
        if session is None or report is None:
            # The session was deleted between the handoff and now. Nothing to
            # do, and nothing has gone wrong.
            logger.info("Session %s no longer exists; skipping evaluation.", session_id)
            return

        # Claim the work before the slow part, so a stuck report is
        # distinguishable from one that was never picked up.
        report.status = ReportStatus.GENERATING
        await db.commit()

        # There is no request and no CurrentUser out here, so the account
        # holder is looked up explicitly. Worth one query: without it the
        # transcript goes to the provider with the candidate's name intact
        # wherever they typed it into an answer.
        owner = await users.get(user_id)
        redactor = redactor_for(owner.full_name if owner else None)

        result = await get_evaluator(redactor).evaluate(
            target_role=session.target_role,
            transcript=[
                QAPair(
                    question=question.content,
                    answer=question.answer.content if question.answer else None,
                )
                for question in session.questions
            ],
        )

        report.overall_score = result.overall_score
        report.strengths = result.strengths
        report.weaknesses = result.weaknesses
        report.detailed_feedback = result.detailed_feedback
        report.status = ReportStatus.COMPLETED
        await db.commit()
        logger.info("Evaluation completed for session %s.", session_id)


async def mark_failed(session_id: uuid.UUID) -> None:
    """Record the failure on a fresh session; the previous one may be poisoned."""
    try:
        async with AsyncSessionFactory() as db:
            await db.execute(
                update(EvaluationReport)
                .where(EvaluationReport.session_id == session_id)
                .values(status=ReportStatus.FAILED)
            )
            await db.commit()
    except Exception:
        # Nothing further to try. The report stays GENERATING and
        # recover_stale_reports will pick it up on the next start.
        logger.exception("Could not mark the report for session %s failed.", session_id)


async def recover_stale_reports() -> int:
    """Flip reports left mid-flight by a restart to FAILED. Returns the count.

    Only correct when the work was in-process: without a queue, a PENDING or
    GENERATING report has nothing behind it after a restart, so leaving it alone
    means a spinner that never resolves. FAILED is visible and re-evaluatable.

    With Redis configured this must NOT run -- those rows have a real job
    waiting in the queue, and failing them would destroy live work. The lifespan
    in app/main.py gates the call accordingly.
    """
    try:
        async with AsyncSessionFactory() as db:
            stale = (
                await db.execute(
                    select(EvaluationReport.id).where(
                        EvaluationReport.status.in_(
                            [ReportStatus.PENDING, ReportStatus.GENERATING]
                        )
                    )
                )
            ).scalars().all()

            if not stale:
                return 0

            await db.execute(
                update(EvaluationReport)
                .where(EvaluationReport.id.in_(stale))
                .values(status=ReportStatus.FAILED)
            )
            await db.commit()
            logger.warning(
                "Marked %d report(s) failed: left mid-evaluation by a restart.",
                len(stale),
            )
            return len(stale)
    except Exception:
        # Startup must not depend on the database being reachable.
        logger.exception("Could not recover stale evaluation reports.")
        return 0
