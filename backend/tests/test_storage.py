"""LocalStorageService contract tests against the StorageService interface."""

import pytest

from app.core.exceptions import StorageError
from app.services.storage.local import LocalStorageService


@pytest.fixture
def storage(tmp_path):
    return LocalStorageService(root=tmp_path / "objects")


async def test_save_and_read_roundtrip(storage):
    stored = await storage.save("resumes/u1/abc.pdf", b"%PDF-1.7", "application/pdf")
    assert stored.key == "resumes/u1/abc.pdf"
    assert stored.size_bytes == 8
    assert await storage.read("resumes/u1/abc.pdf") == b"%PDF-1.7"


async def test_exists_and_delete_idempotent(storage):
    await storage.save("resumes/u1/x.pdf", b"data", "application/pdf")
    assert await storage.exists("resumes/u1/x.pdf") is True
    await storage.delete("resumes/u1/x.pdf")
    assert await storage.exists("resumes/u1/x.pdf") is False
    await storage.delete("resumes/u1/x.pdf")  # second delete must not raise


async def test_read_missing_raises_storage_error(storage):
    with pytest.raises(StorageError):
        await storage.read("resumes/u1/missing.pdf")


@pytest.mark.parametrize("key", ["../escape.pdf", "/absolute.pdf", "a/../../b.pdf"])
async def test_path_traversal_rejected(storage, key):
    with pytest.raises(StorageError):
        await storage.save(key, b"data", "application/pdf")
