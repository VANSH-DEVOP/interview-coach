"""ResumeService reprocessing.

Two states had no way out before this: a resume whose parse failed (marked
FAILED forever, though the blob was still in storage), and a resume uploaded
while indexing was broken (present in the database, absent from the vector
store, so retrieval silently returns nothing for it).
"""

import uuid

import pytest

from app.core.exceptions import NotFoundError
from app.models.resume import Resume, ResumeStatus
from app.services import resume_service as resume_service_module
from app.services.ai import degradation
from app.services.resume_service import ResumeService


@pytest.fixture(autouse=True)
def _clean_degradation():
    degradation.reset()
    yield
    degradation.reset()


class _FakeSession:
    async def flush(self) -> None:
        return None


class _FakeResumeRepository:
    def __init__(self, resumes=None) -> None:
        self.session = _FakeSession()
        self._resumes = resumes or {}

    async def get_owned(self, resume_id, user_id):
        resume = self._resumes.get(resume_id)
        if resume is None or resume.user_id != user_id:
            return None
        return resume


class _FakeStorage:
    def __init__(self, content: bytes = b"%PDF-fake") -> None:
        self.content = content
        self.reads: list[str] = []

    async def read(self, key: str) -> bytes:
        self.reads.append(key)
        return self.content


class _FakeRag:
    def __init__(self, *, index_error: Exception | None = None) -> None:
        self.calls: list[tuple[str, uuid.UUID]] = []
        self.redactors: list[object] = []
        self._index_error = index_error

    async def index_resume(self, resume_id, user_id, text, *, redactor=None):
        self.calls.append(("index", resume_id))
        self.redactors.append(redactor)
        if self._index_error is not None:
            raise self._index_error
        return 3

    async def delete_index(self, resume_id):
        self.calls.append(("delete", resume_id))


def _make(status=ResumeStatus.FAILED, parsed_text=None, user_id=None):
    user_id = user_id or uuid.uuid4()
    resume = Resume(
        user_id=user_id,
        file_name="cv.pdf",
        storage_key="resumes/u/abc.pdf",
        content_type="application/pdf",
        size_bytes=100,
        parsed_text=parsed_text,
        status=status,
    )
    resume.id = uuid.uuid4()
    return resume, user_id


def _service(resume, rag=None, storage=None):
    return ResumeService(
        _FakeResumeRepository({resume.id: resume}),
        storage or _FakeStorage(),
        rag,
    )


@pytest.fixture
def parse_ok(monkeypatch):
    monkeypatch.setattr(
        resume_service_module.ResumeParser, "parse", lambda content, ct: "Parsed resume text."
    )


@pytest.fixture
def parse_fails(monkeypatch):
    def _boom(content, ct):
        raise ValueError("unreadable PDF")

    monkeypatch.setattr(resume_service_module.ResumeParser, "parse", _boom)


async def test_reprocess_recovers_a_failed_parse(parse_ok):
    resume, user_id = _make(status=ResumeStatus.FAILED)
    service = _service(resume)

    result = await service.reprocess(resume.id, user_id)

    assert result.status is ResumeStatus.PARSED
    assert result.parsed_text == "Parsed resume text."


async def test_reprocess_rereads_the_stored_blob(parse_ok):
    resume, user_id = _make()
    storage = _FakeStorage()
    service = _service(resume, storage=storage)

    await service.reprocess(resume.id, user_id)

    assert storage.reads == ["resumes/u/abc.pdf"]


async def test_reprocess_that_fails_again_stays_failed(parse_fails):
    resume, user_id = _make(status=ResumeStatus.PARSED, parsed_text="stale text")
    service = _service(resume)

    result = await service.reprocess(resume.id, user_id)

    assert result.status is ResumeStatus.FAILED
    # Stale text must not survive a failed re-parse.
    assert result.parsed_text is None


async def test_reprocess_clears_the_old_index_before_rebuilding(parse_ok):
    resume, user_id = _make()
    rag = _FakeRag()
    service = _service(resume, rag=rag)

    await service.reprocess(resume.id, user_id)

    # Order matters: chunk ids are positional, so indexing over a longer
    # previous run would strand stale trailing chunks.
    assert rag.calls == [("delete", resume.id), ("index", resume.id)]


async def test_reprocess_indexes_a_resume_that_was_never_indexed(parse_ok):
    resume, user_id = _make(status=ResumeStatus.PARSED, parsed_text="already parsed")
    rag = _FakeRag()
    service = _service(resume, rag=rag)

    await service.reprocess(resume.id, user_id)

    assert ("index", resume.id) in rag.calls


async def test_failed_reparse_does_not_index(parse_fails):
    resume, user_id = _make()
    rag = _FakeRag()
    service = _service(resume, rag=rag)

    await service.reprocess(resume.id, user_id)

    assert ("index", resume.id) not in rag.calls


async def test_indexing_failure_does_not_fail_the_request(parse_ok):
    resume, user_id = _make()
    rag = _FakeRag(index_error=RuntimeError("chroma down"))
    service = _service(resume, rag=rag)

    result = await service.reprocess(resume.id, user_id)

    # Still succeeds, but the degradation is visible on /health.
    assert result.status is ResumeStatus.PARSED
    snap = degradation.snapshot()
    assert snap["fallbacks"] == 1
    assert snap["last_operation"] == "index_resume"


async def test_reprocess_works_without_a_rag_service(parse_ok):
    resume, user_id = _make()
    service = _service(resume, rag=None)

    result = await service.reprocess(resume.id, user_id)

    assert result.status is ResumeStatus.PARSED


async def test_reprocess_another_users_resume_raises_not_found(parse_ok):
    resume, _ = _make()
    service = _service(resume)

    with pytest.raises(NotFoundError):
        await service.reprocess(resume.id, uuid.uuid4())


async def test_reprocess_unknown_resume_raises_not_found(parse_ok):
    resume, user_id = _make()
    service = _service(resume)

    with pytest.raises(NotFoundError):
        await service.reprocess(uuid.uuid4(), user_id)
