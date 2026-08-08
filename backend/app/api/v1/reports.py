import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUser, get_report_repository, get_report_service
from app.core.exceptions import NotFoundError
from app.repositories.report_repository import ReportRepository
from app.schemas.common import Page, PageParams
from app.schemas.report import ProgressSummary, ReportRead
from app.services.report_service import ReportService

router = APIRouter(prefix="/reports", tags=["reports"])

ReportRepo = Annotated[ReportRepository, Depends(get_report_repository)]


@router.get("", response_model=Page[ReportRead])
async def list_reports(
    current_user: CurrentUser,
    reports: ReportRepo,
    params: Annotated[PageParams, Depends()],
) -> Page[ReportRead]:
    items, total = await reports.list_for_user(
        current_user.id, offset=params.offset, limit=params.size
    )
    return Page(
        items=[ReportRead.model_validate(r) for r in items],
        total=total,
        page=params.page,
        size=params.size,
    )


# Registered before /{report_id}: FastAPI matches in declaration order, and
# "progress" would otherwise be parsed as a report UUID and 422.
@router.get("/progress", response_model=ProgressSummary)
async def get_progress(
    current_user: CurrentUser,
    reports: Annotated[ReportService, Depends(get_report_service)],
) -> ProgressSummary:
    """Score trend and aggregates across the user's completed interviews."""
    return await reports.progress(current_user.id)


@router.get("/by-session/{session_id}", response_model=ReportRead)
async def get_report_by_session(
    session_id: uuid.UUID, current_user: CurrentUser, reports: ReportRepo
) -> ReportRead:
    report = await reports.get_owned_by_session(session_id, current_user.id)
    if report is None:
        raise NotFoundError("No report exists for this session yet.")
    return ReportRead.model_validate(report)


@router.get("/{report_id}", response_model=ReportRead)
async def get_report(
    report_id: uuid.UUID, current_user: CurrentUser, reports: ReportRepo
) -> ReportRead:
    report = await reports.get_owned(report_id, current_user.id)
    if report is None:
        raise NotFoundError("Report not found.")
    return ReportRead.model_validate(report)
