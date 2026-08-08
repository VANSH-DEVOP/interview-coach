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


class SkillTheme(BaseModel):
    """A recurring theme in feedback, with the user's own wording as evidence."""

    theme: str
    count: int
    examples: list[str]


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
    # Recurring themes across all feedback, most frequent first. The weaknesses
    # are the actionable half: "you have been told to quantify impact in five
    # interviews" is worth more than five separately-worded reminders.
    recurring_strengths: list[SkillTheme]
    recurring_weaknesses: list[SkillTheme]


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
