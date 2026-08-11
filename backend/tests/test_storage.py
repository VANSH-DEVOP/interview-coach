"""One contract, run against every provider that claims to implement it.

`StorageService` exists so that `ResumeService` cannot tell local disk from S3.
That is only true if both providers behave identically at the edges, and the
edges are where they naturally differ: a missing object is `FileNotFoundError`
on one side and an HTTP 404 with a provider-specific error code on the other,
and "delete something that is not there" is a no-op on one and a request that
must not be treated as failure on the other.

So these tests are parameterised over the providers rather than written twice.
Adding a third backend means adding a fixture, not a file — and the day one of
them stops matching, the failure names which contract it broke.

The S3 provider runs against **MinIO**, which speaks the same API as AWS S3,
Cloudflare R2 and Backblaze B2 and needs no account, no card and no network.
Same bargain as Postgres and Redis elsewhere in this suite: skip when it is not
reachable so `pytest` stays useful on a laptop with nothing running, and set
`REQUIRE_TEST_S3=1` to turn that skip into a failure, which is what CI does.

    docker compose --profile s3 up -d minio
"""

import os
import uuid

import pytest

from app.core.exceptions import StorageError
from app.services.storage.local import LocalStorageService

MINIO_ENDPOINT = os.getenv("TEST_S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_KEY = os.getenv("TEST_S3_ACCESS_KEY_ID", "minioadmin")
MINIO_SECRET = os.getenv("TEST_S3_SECRET_ACCESS_KEY", "minioadmin")


def _s3_storage():
    """An S3 provider against a throwaway bucket, or a skip."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError:  # pragma: no cover
        pytest.skip("boto3 is not installed.")

    from app.services.storage.s3 import S3StorageService

    bucket = f"test-{uuid.uuid4().hex[:16]}"
    client = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        region_name="auto",
        aws_access_key_id=MINIO_KEY,
        aws_secret_access_key=MINIO_SECRET,
        config=Config(
            retries={"max_attempts": 1},
            s3={"addressing_style": "path"},
            connect_timeout=2,
            read_timeout=5,
        ),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except Exception as exc:  # noqa: BLE001 - any failure means "no MinIO here"
        message = (
            f"No test S3 reachable ({type(exc).__name__}). Start MinIO with "
            f"`docker compose --profile s3 up -d minio` or set "
            f"TEST_S3_ENDPOINT_URL (currently {MINIO_ENDPOINT})."
        )
        # CI sets REQUIRE_TEST_S3. Skipping there would let a broken container
        # produce a green build -- the same trap the Postgres and Redis
        # fixtures avoid.
        if os.getenv("REQUIRE_TEST_S3"):
            pytest.fail(message)
        pytest.skip(message)

    service = S3StorageService(
        bucket=bucket,
        endpoint_url=MINIO_ENDPOINT,
        region="auto",
        access_key_id=MINIO_KEY,
        secret_access_key=MINIO_SECRET,
    )
    yield service

    # Empty then remove: S3 refuses to delete a bucket with objects in it, and
    # leaving one behind per test run would accumulate silently.
    try:
        listing = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        for item in listing:
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)
    except Exception:  # noqa: BLE001 - cleanup must not fail a passing test
        pass


@pytest.fixture(params=["local", "s3"])
def storage(request, tmp_path):
    # Both branches yield: this is a generator fixture because the S3 half needs
    # teardown, and a bare `return value` in a generator yields nothing at all.
    if request.param == "local":
        yield LocalStorageService(root=tmp_path / "objects")
        return
    yield from _s3_storage()


# -- The contract ---------------------------------------------------------------


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
    """The failure that differs most between providers underneath: a
    FileNotFoundError on one side, an HTTP 404 carrying a provider-specific
    error code on the other. Callers see neither."""
    with pytest.raises(StorageError):
        await storage.read("resumes/u1/missing.pdf")


async def test_exists_is_false_rather_than_an_error(storage):
    assert await storage.exists("resumes/u1/never-written.pdf") is False


@pytest.mark.parametrize("key", ["../escape.pdf", "/absolute.pdf", "a/../../b.pdf"])
async def test_path_traversal_rejected(storage, key):
    """S3 has no filesystem to escape, so this is parity rather than a
    traversal guard there -- a key with `..` in it means something upstream is
    wrong, and both providers should refuse it rather than one quietly storing
    an object literally named `../escape.pdf`."""
    with pytest.raises(StorageError):
        await storage.save(key, b"data", "application/pdf")


async def test_overwriting_a_key_replaces_it(storage):
    """`save` is documented as overwriting. Local does write-then-rename; S3
    does a plain PUT."""
    await storage.save("resumes/u1/cv.pdf", b"first", "application/pdf")
    await storage.save("resumes/u1/cv.pdf", b"second", "application/pdf")

    assert await storage.read("resumes/u1/cv.pdf") == b"second"


async def test_bytes_survive_unchanged(storage):
    """A PDF is not text. Anything that decodes, re-encodes or normalises on the
    way through corrupts the upload, and the parser failure that follows would
    look like a bad resume rather than a bad provider."""
    payload = bytes(range(256)) * 8

    await storage.save("resumes/u1/binary.pdf", payload, "application/pdf")

    assert await storage.read("resumes/u1/binary.pdf") == payload


# -- Configuration --------------------------------------------------------------


def test_s3_requires_a_bucket() -> None:
    from app.services.storage.s3 import S3StorageService

    with pytest.raises(StorageError, match="bucket"):
        S3StorageService(bucket="")


def test_the_factory_refuses_s3_without_a_bucket(monkeypatch) -> None:
    """At boot, not on the first upload. The alternative is a clean start and a
    502 an hour later, which is the same failure and much harder to attribute."""
    from app.core.config import get_settings
    from app.services.storage import get_storage_service

    monkeypatch.setattr(get_settings(), "STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr(get_settings(), "S3_BUCKET", None, raising=False)
    get_storage_service.cache_clear()

    with pytest.raises(StorageError, match="S3_BUCKET"):
        get_storage_service()

    get_storage_service.cache_clear()
