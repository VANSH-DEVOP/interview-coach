"""Resume endpoints over HTTP, against a real database and real storage."""

import io
import uuid

import pytest
from pypdf import PdfWriter

PDF_TYPE = "application/pdf"
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture
async def other_user(api):
    email = f"other-{uuid.uuid4().hex[:8]}@example.com"
    password = "correct-horse-battery"
    await api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Other"},
    )
    login = await api.post("/api/v1/auth/login", json={"email": email, "password": password})
    return {"headers": {"Authorization": f"Bearer {login.json()['access_token']}"}}


async def _upload(api, headers, *, content=None, name="cv.pdf", content_type=PDF_TYPE):
    files = {"file": (name, content if content is not None else _pdf_bytes(), content_type)}
    return await api.post("/api/v1/resumes", files=files, headers=headers)


# -- Upload --------------------------------------------------------------------


async def test_upload_stores_a_pdf(api, registered_user, storage_root):
    response = await _upload(api, registered_user["headers"])

    assert response.status_code == 201
    body = response.json()
    assert body["file_name"] == "cv.pdf"
    assert body["content_type"] == PDF_TYPE
    assert body["size_bytes"] > 0


async def test_upload_writes_under_the_configured_storage_root(
    api, registered_user, storage_root
):
    """Guards the fixture itself: if the redirect silently failed, these tests
    would be writing into the real storage directory."""
    assert list(storage_root.rglob("*.pdf")) == []

    response = await _upload(api, registered_user["headers"])

    assert response.status_code == 201
    written = list(storage_root.rglob("*.pdf"))
    assert len(written) == 1
    # Opaque key, not the client filename.
    assert written[0].name != "cv.pdf"


async def test_upload_never_exposes_the_storage_key(api, registered_user, storage_root):
    """The key is an internal path; leaking it invites probing at the blob layer."""
    response = await _upload(api, registered_user["headers"])
    assert "storage_key" not in response.json()


async def test_upload_strips_a_client_supplied_path(api, registered_user, storage_root):
    response = await _upload(
        api, registered_user["headers"], name="../../../etc/passwd.pdf"
    )

    assert response.status_code == 201
    assert response.json()["file_name"] == "passwd.pdf"


async def test_upload_rejects_an_unsupported_content_type(api, registered_user, storage_root):
    response = await _upload(
        api, registered_user["headers"], content=b"hello", name="notes.txt",
        content_type="text/plain",
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_upload_rejects_an_empty_file(api, registered_user, storage_root):
    response = await _upload(api, registered_user["headers"], content=b"")

    assert response.status_code == 422


async def test_upload_rejects_an_oversized_file(api, registered_user, storage_root):
    from app.core.config import get_settings

    oversized = b"x" * (get_settings().MAX_UPLOAD_SIZE_BYTES + 1)
    response = await _upload(api, registered_user["headers"], content=oversized)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_upload_requires_authentication(api, storage_root):
    files = {"file": ("cv.pdf", _pdf_bytes(), PDF_TYPE)}
    assert (await api.post("/api/v1/resumes", files=files)).status_code == 401


# -- Read / download -----------------------------------------------------------


async def test_get_and_list_return_the_upload(api, registered_user, storage_root):
    headers = registered_user["headers"]
    resume = (await _upload(api, headers)).json()

    single = await api.get(f"/api/v1/resumes/{resume['id']}", headers=headers)
    listed = await api.get("/api/v1/resumes?page=1&size=50", headers=headers)

    assert single.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["id"] == resume["id"]


async def test_download_returns_the_original_bytes(api, registered_user, storage_root):
    headers = registered_user["headers"]
    content = _pdf_bytes()
    resume = (await _upload(api, headers, content=content)).json()

    response = await api.get(f"/api/v1/resumes/{resume['id']}/download", headers=headers)

    assert response.status_code == 200
    assert response.content == content


async def test_get_unknown_resume_is_404(api, registered_user, storage_root):
    response = await api.get(
        f"/api/v1/resumes/{uuid.uuid4()}", headers=registered_user["headers"]
    )
    assert response.status_code == 404


# -- Preview -------------------------------------------------------------------


async def test_preview_returns_the_extracted_text(api, registered_user, storage_root, db_session):
    from app.models.resume import Resume

    headers = registered_user["headers"]
    resume = (await _upload(api, headers)).json()

    # A blank PDF extracts to nothing, so give it text the way parsing would.
    row = await db_session.get(Resume, uuid.UUID(resume["id"]))
    row.parsed_text = "Senior Backend Engineer. Python, FastAPI, PostgreSQL."
    await db_session.commit()

    response = await api.get(f"/api/v1/resumes/{resume['id']}/preview", headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["parsed_text"] == "Senior Backend Engineer. Python, FastAPI, PostgreSQL."
    assert body["word_count"] == 6  # whitespace-separated tokens
    assert body["character_count"] == len(body["parsed_text"])


async def test_preview_reports_empty_extraction_rather_than_pretending(
    api, registered_user, storage_root
):
    """A scanned PDF parses to nothing. The user needs to see that, because it
    is the difference between a personalised interview and a generic one."""
    headers = registered_user["headers"]
    resume = (await _upload(api, headers)).json()

    body = (await api.get(f"/api/v1/resumes/{resume['id']}/preview", headers=headers)).json()

    assert not body["parsed_text"]
    assert body["word_count"] == 0
    assert body["character_count"] == 0


async def test_preview_is_not_included_in_the_list_response(
    api, registered_user, storage_root
):
    """Parsed text can run to thousands of characters; it stays off the list."""
    headers = registered_user["headers"]
    await _upload(api, headers)

    listed = await api.get("/api/v1/resumes?page=1&size=50", headers=headers)

    assert "parsed_text" not in listed.json()["items"][0]


async def test_another_user_cannot_preview_a_resume(
    api, registered_user, other_user, storage_root
):
    resume = (await _upload(api, registered_user["headers"])).json()

    response = await api.get(
        f"/api/v1/resumes/{resume['id']}/preview", headers=other_user["headers"]
    )

    assert response.status_code == 404


async def test_preview_unknown_resume_is_404(api, registered_user, storage_root):
    response = await api.get(
        f"/api/v1/resumes/{uuid.uuid4()}/preview", headers=registered_user["headers"]
    )
    assert response.status_code == 404


# -- Reprocess -----------------------------------------------------------------


async def test_reprocess_returns_the_resume(api, registered_user, storage_root):
    headers = registered_user["headers"]
    resume = (await _upload(api, headers)).json()

    response = await api.post(f"/api/v1/resumes/{resume['id']}/reprocess", headers=headers)

    assert response.status_code == 200
    assert response.json()["id"] == resume["id"]


async def test_reprocess_unknown_resume_is_404(api, registered_user, storage_root):
    response = await api.post(
        f"/api/v1/resumes/{uuid.uuid4()}/reprocess", headers=registered_user["headers"]
    )
    assert response.status_code == 404


# -- Delete --------------------------------------------------------------------


async def test_delete_removes_the_resume(api, registered_user, storage_root):
    headers = registered_user["headers"]
    resume = (await _upload(api, headers)).json()

    assert (await api.delete(f"/api/v1/resumes/{resume['id']}", headers=headers)).status_code == 204
    assert (await api.get(f"/api/v1/resumes/{resume['id']}", headers=headers)).status_code == 404


# -- Ownership -----------------------------------------------------------------


async def test_another_user_cannot_read_a_resume(api, registered_user, other_user, storage_root):
    resume = (await _upload(api, registered_user["headers"])).json()

    assert (
        await api.get(f"/api/v1/resumes/{resume['id']}", headers=other_user["headers"])
    ).status_code == 404
    assert (
        await api.get(f"/api/v1/resumes/{resume['id']}/download", headers=other_user["headers"])
    ).status_code == 404


async def test_another_user_cannot_delete_or_reprocess_a_resume(
    api, registered_user, other_user, storage_root
):
    resume = (await _upload(api, registered_user["headers"])).json()

    assert (
        await api.delete(f"/api/v1/resumes/{resume['id']}", headers=other_user["headers"])
    ).status_code == 404
    assert (
        await api.post(
            f"/api/v1/resumes/{resume['id']}/reprocess", headers=other_user["headers"]
        )
    ).status_code == 404
    # Untouched for its owner.
    assert (
        await api.get(f"/api/v1/resumes/{resume['id']}", headers=registered_user["headers"])
    ).status_code == 200


async def test_resume_listing_is_scoped_to_the_user(api, registered_user, other_user, storage_root):
    await _upload(api, registered_user["headers"])

    listed = await api.get("/api/v1/resumes?page=1&size=50", headers=other_user["headers"])

    assert listed.json()["total"] == 0
