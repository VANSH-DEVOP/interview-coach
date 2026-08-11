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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.time import utcnow
from app.db.session import AsyncSessionFactory
from app.models.evaluation_report import EvaluationReport, ReportStatus
from app.models.interview_session import InterviewSession
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
                    duration_seconds=(
                        question.answer.duration_seconds if question.answer else None
                    ),
                    transcript_source=(
                        question.answer.transcript_source if question.answer else "typed"
                    ),
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
    in app/main.py gates the call accordingly. `reconcile_stale_reports` is the
    queued equivalent: it waits out an age threshold and re-queues rather than
    failing, because there the work is recoverable.
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


# -- Queued reconciliation -----------------------------------------------------

# What the sweep needs from the queue: hand this session back to a worker. Kept
# as a callable so this module stays free of arq and of app.services.job_queue,
# which imports *from here* -- app/worker.py owns both ends and passes the
# binding in.
Enqueue = Callable[[uuid.UUID, uuid.UUID], Awaitable[object]]


@dataclass(frozen=True)
class Reconciliation:
    """What one sweep did. `abandoned` above zero is worth an alert."""

    requeued: int = 0
    abandoned: int = 0

    def __bool__(self) -> bool:
        return bool(self.requeued or self.abandoned)


async def reconcile_stale_reports(enqueue: Enqueue) -> Reconciliation:
    """Re-queue evaluations whose job disappeared. Never raises.

    The durable path is only durable while Redis holds the job. A Redis restart
    without persistence drops the queue, and the API's own recovery pass is
    gated off precisely when a queue exists -- so those reports stay PENDING
    forever and the UI polls a spinner that will never resolve.

    Staleness is measured from `updated_at`, which moves every time the
    evaluation changes the row (PENDING -> GENERATING -> COMPLETED). A job that
    is merely slow has therefore touched the row recently and is left alone;
    EVALUATION_STALE_AFTER_SECONDS is set well above the worst-case run so the
    sweep never races live work and double-evaluates a session.

    Past EVALUATION_STALE_GIVE_UP_SECONDS the report is failed instead. That
    bound is what stops a session which can never be evaluated from being
    re-queued on every sweep for the rest of time.
    """
    settings = get_settings()
    now = utcnow()
    stale_before = now - timedelta(seconds=settings.EVALUATION_STALE_AFTER_SECONDS)
    give_up_before = now - timedelta(seconds=settings.EVALUATION_STALE_GIVE_UP_SECONDS)

    requeued = abandoned = 0
    try:
        async with AsyncSessionFactory() as db:
            # The user id comes from the session: the worker needs it to load
            # the interview through get_owned, and the report does not carry it.
            # The join also drops reports whose session is gone.
            rows = (
                await db.execute(
                    select(EvaluationReport, InterviewSession.user_id)
                    .join(
                        InterviewSession,
                        InterviewSession.id == EvaluationReport.session_id,
                    )
                    .where(
                        EvaluationReport.status.in_(
                            [ReportStatus.PENDING, ReportStatus.GENERATING]
                        ),
                        EvaluationReport.updated_at < stale_before,
                    )
                )
            ).all()

            for report, user_id in rows:
                if report.created_at < give_up_before:
                    report.status = ReportStatus.FAILED
                    abandoned += 1
                    continue

                try:
                    await enqueue(report.session_id, user_id)
                except Exception:
                    # Leave the row exactly as it is. Its updated_at stays old,
                    # so the next sweep retries immediately instead of waiting
                    # out the window again.
                    logger.exception(
                        "Could not re-queue the evaluation for session %s.",
                        report.session_id,
                    )
                    continue

                report.status = ReportStatus.PENDING
                # Explicit, because PENDING -> PENDING is not a change and
                # SQLAlchemy would emit no UPDATE at all -- leaving the row
                # stale and re-queued on every sweep from here on.
                report.updated_at = now
                requeued += 1

            await db.commit()
    except Exception:
        # A sweep that raises kills the cron job, and the next tick is the only
        # thing that would have fixed the transient failure.
        logger.exception("Could not reconcile stale evaluation reports.")
        return Reconciliation()

    if requeued or abandoned:
        logger.warning(
            "Reconciled orphaned reports: %d re-queued, %d abandoned after %ds.",
            requeued,
            abandoned,
            settings.EVALUATION_STALE_GIVE_UP_SECONDS,
        )
    return Reconciliation(requeued=requeued, abandoned=abandoned)
