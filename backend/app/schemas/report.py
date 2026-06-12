import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.evaluation_report import ReportStatus


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
