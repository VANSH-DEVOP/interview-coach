import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.evaluation_report import ReportStatus


class ProgressPoint(BaseModel):
    """One scored session in the user's history, oldest first."""

    session_id: uuid.UUID
    title: str
    target_role: str | None
    interview_type: str
    difficulty: str
    score: float
    scored_at: datetime


class ProgressSummary(BaseModel):
    """Score trend across a user's completed interviews."""

    points: list[ProgressPoint]
    total_scored: int
    average_score: float | None
    best_score: float | None
    latest_score: float | None
    # Mean of the most recent half minus the mean of the earlier half. Null
    # until there are enough sessions for the comparison to mean anything --
    # a "trend" drawn from two interviews is noise, not progress.
    improvement: float | None
    # Average score per interview type, for spotting a weak area.
    average_by_type: dict[str, float]


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    overall_score: Decimal | None
    strengths: list[Any] | None
    weaknesses: list[Any] | None
    detailed_feedback: dict[str, Any] | None
    status: ReportStatus
    created_at: datetime
