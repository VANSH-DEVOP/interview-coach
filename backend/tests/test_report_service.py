"""Progress aggregation.

The dashboard's score trend is derived, not stored, so the arithmetic and the
refusal to over-claim a trend from too little data are worth pinning.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from app.repositories.report_repository import ScoreHistoryRow
from app.services.report_service import MIN_SESSIONS_FOR_TREND, ReportService

BASE = datetime(2026, 1, 1)


class _FakeReportRepository:
    def __init__(self, rows, strengths=None, weaknesses=None) -> None:
        self._rows = rows
        self._strengths = strengths or []
        self._weaknesses = weaknesses or []
        self.limit_used: int | None = None

    async def score_history(self, user_id, *, limit=50):
        self.limit_used = limit
        return self._rows

    async def feedback_history(self, user_id, *, limit=50):
        return self._strengths, self._weaknesses


def _row(score: float, *, day: int = 0, itype: str = "mixed", difficulty: str = "mid"):
    return ScoreHistoryRow(
        session_id=uuid.uuid4(),
        title=f"Session {day}",
        target_role="Backend",
        interview_type=itype,
        difficulty=difficulty,
        score=score,
        scored_at=BASE + timedelta(days=day),
    )


def _service(rows, strengths=None, weaknesses=None):
    return ReportService(_FakeReportRepository(rows, strengths, weaknesses))


async def test_no_history_returns_an_empty_summary():
    summary = await _service([]).progress(uuid.uuid4())

    assert summary.points == []
    assert summary.total_scored == 0
    # Null, not zero: "no interviews yet" is not "you scored zero".
    assert summary.average_score is None
    assert summary.best_score is None
    assert summary.latest_score is None
    assert summary.improvement is None
    assert summary.average_by_type == {}


async def test_aggregates_are_computed_over_the_history():
    rows = [_row(4.0, day=0), _row(6.0, day=1), _row(8.0, day=2)]
    summary = await _service(rows).progress(uuid.uuid4())

    assert summary.total_scored == 3
    assert summary.average_score == 6.0
    assert summary.best_score == 8.0
    # Rows arrive chronologically, so the last one is the latest.
    assert summary.latest_score == 8.0


async def test_latest_score_is_the_most_recent_not_the_highest():
    rows = [_row(9.5, day=0), _row(3.0, day=1)]
    summary = await _service(rows).progress(uuid.uuid4())

    assert summary.latest_score == 3.0
    assert summary.best_score == 9.5


@pytest.mark.parametrize("count", range(MIN_SESSIONS_FOR_TREND))
async def test_improvement_is_withheld_until_there_is_enough_history(count):
    rows = [_row(5.0, day=i) for i in range(count)]
    summary = await _service(rows).progress(uuid.uuid4())

    # A "trend" from one or two interviews is noise, not progress.
    assert summary.improvement is None


async def test_improvement_compares_recent_half_to_earlier_half():
    # Earlier half mean 3.0, recent half mean 7.0.
    rows = [_row(2.0, day=0), _row(4.0, day=1), _row(6.0, day=2), _row(8.0, day=3)]
    summary = await _service(rows).progress(uuid.uuid4())

    assert summary.improvement == 4.0


async def test_improvement_is_negative_when_getting_worse():
    rows = [_row(8.0, day=0), _row(8.0, day=1), _row(4.0, day=2), _row(4.0, day=3)]
    summary = await _service(rows).progress(uuid.uuid4())

    assert summary.improvement == -4.0


async def test_improvement_resists_a_single_outlier():
    # One bad interview among four good ones must not read as a collapse.
    rows = [_row(8.0, day=0), _row(8.0, day=1), _row(8.0, day=2), _row(0.0, day=3)]
    summary = await _service(rows).progress(uuid.uuid4())

    # -4.0 rather than the -8.0 a first-vs-last comparison would report.
    assert summary.improvement == -4.0


async def test_odd_history_splits_without_losing_a_session():
    rows = [_row(2.0, day=0), _row(4.0, day=1), _row(6.0, day=2), _row(8.0, day=3), _row(10.0, day=4)]
    summary = await _service(rows).progress(uuid.uuid4())

    # 5 points: earlier [2,4] = 3.0, recent [6,8,10] = 8.0.
    assert summary.improvement == 5.0


async def test_average_by_type_groups_scores():
    rows = [
        _row(6.0, day=0, itype="behavioral"),
        _row(8.0, day=1, itype="behavioral"),
        _row(2.0, day=2, itype="system_design"),
    ]
    summary = await _service(rows).progress(uuid.uuid4())

    assert summary.average_by_type == {"behavioral": 7.0, "system_design": 2.0}


async def test_points_carry_enough_context_to_label_a_chart():
    rows = [_row(7.0, day=0, itype="technical", difficulty="senior")]
    summary = await _service(rows).progress(uuid.uuid4())

    point = summary.points[0]
    assert point.title == "Session 0"
    assert point.target_role == "Backend"
    assert point.interview_type == "technical"
    assert point.difficulty == "senior"
    assert point.scored_at == BASE
