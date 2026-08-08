"""Report aggregation.

Reads are served straight from the repository elsewhere; this service exists
for the derived numbers behind progress tracking, which are genuinely business
logic rather than a query.
"""

import uuid
from collections import defaultdict

from app.repositories.report_repository import ReportRepository
from app.schemas.report import ProgressPoint, ProgressSummary, SkillTheme
from app.services import skill_themes

# Below this many scored sessions, an "improvement" figure is noise dressed up
# as a trend. Four is the smallest history that splits into two halves of two.
MIN_SESSIONS_FOR_TREND = 4

# How far back the trend goes. Enough to show a real arc without turning the
# dashboard into an unreadable smear.
HISTORY_LIMIT = 50


class ReportService:
    def __init__(self, reports: ReportRepository) -> None:
        self.reports = reports

    async def progress(self, user_id: uuid.UUID) -> ProgressSummary:
        rows = await self.reports.score_history(user_id, limit=HISTORY_LIMIT)
        raw_strengths, raw_weaknesses = await self.reports.feedback_history(
            user_id, limit=HISTORY_LIMIT
        )
        recurring_strengths = [
            SkillTheme(**theme) for theme in skill_themes.summarise(raw_strengths)
        ]
        recurring_weaknesses = [
            SkillTheme(**theme) for theme in skill_themes.summarise(raw_weaknesses)
        ]

        points = [
            ProgressPoint(
                session_id=row.session_id,
                title=row.title,
                target_role=row.target_role,
                interview_type=row.interview_type,
                difficulty=row.difficulty,
                score=row.score,
                scored_at=row.scored_at,
            )
            for row in rows
        ]

        if not points:
            return ProgressSummary(
                points=[],
                total_scored=0,
                average_score=None,
                best_score=None,
                latest_score=None,
                improvement=None,
                average_by_type={},
                # Feedback can exist without a score (a report that failed
                # after partially writing), so these are still worth returning.
                recurring_strengths=recurring_strengths,
                recurring_weaknesses=recurring_weaknesses,
            )

        scores = [p.score for p in points]

        by_type: dict[str, list[float]] = defaultdict(list)
        for point in points:
            by_type[point.interview_type].append(point.score)

        return ProgressSummary(
            points=points,
            total_scored=len(points),
            average_score=round(sum(scores) / len(scores), 2),
            best_score=max(scores),
            latest_score=scores[-1],  # points are chronological
            improvement=_improvement(scores),
            average_by_type={
                name: round(sum(values) / len(values), 2)
                for name, values in sorted(by_type.items())
            },
            recurring_strengths=recurring_strengths,
            recurring_weaknesses=recurring_weaknesses,
        )


def _improvement(scores: list[float]) -> float | None:
    """Recent-half mean minus earlier-half mean, or None if too few sessions.

    Comparing halves rather than first-vs-last keeps one unusually good or bad
    interview from dominating the headline number.
    """
    if len(scores) < MIN_SESSIONS_FOR_TREND:
        return None
    midpoint = len(scores) // 2
    earlier = scores[:midpoint]
    recent = scores[midpoint:]
    return round(sum(recent) / len(recent) - sum(earlier) / len(earlier), 2)
