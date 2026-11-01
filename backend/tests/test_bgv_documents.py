"""Legacy verification documents: upload is retired, listing/withdrawal and erasure remain.

The candidate profile no longer collects academic certificates or address
proof (production-fix pass, 2026-11). These tests pin:

* **There is no upload route.** `POST /bgv/me/documents` is gone, not hidden.
* **The key never crosses the boundary.** A response that carried the object
  key would hand out the address of a private store.
* **Erasure reaches the BYTES.** Deleting the row deletes nothing in the object
  store: the assertion is a HEAD against the store, never a row count.
* **Nothing is gated** on a fresher having or lacking a document.

Rows are seeded directly, because the feature that wrote them no longer exists.
"""
from __future__ import annotations

import asyncio
import io
import os
import uuid
import zlib
from typing import Iterator

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import CurrentUser, get_candidate_db, get_current_candidate
from app.core.config import get_settings
from app.core.db import superadmin_scope
from app.core.security import AUDIENCE_CANDIDATE
from app.main import app
from app.models.enums import Role
from app.services import bgv_documents, bgv_workflow, object_storage

BGV = "/api/v1/bgv"
BUCKET = os.environ.get("S3_TEST_BUCKET", "readypick-test-private")
REGION = "ap-south-1"
LIVE_ENDPOINT = os.environ.get("S3_TEST_ENDPOINT_URL", "").strip()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _sessions():
    return async_sessionmaker(
        create_async_engine(get_settings().database_url, poolclass=NullPool),
        expire_on_commit=False,
    )


async def _reachable() -> bool:
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(
                sa.text("SELECT object_key FROM candidate_bgv_documents LIMIT 0")
            )
        return True
    except Exception:  # noqa: BLE001 -- no database, or migration 0114 unapplied
        return False
    finally:
        await engine.dispose()


class _Settings:
    """What `object_storage.get_settings()` returns for these tests."""

    s3_bucket = BUCKET
    aws_region = REGION
    s3_endpoint_url = LIVE_ENDPOINT
    #: MinIO's static KMS key (`docker-compose.test.yml`); moto accepts it
    #: too. Every bucket write names it, as the pilot bucket policy requires
    #: (`object_storage.sse_arguments`).
    s3_kms_key_id = "readypick-test-key"


@pytest.fixture
def storage(monkeypatch):
    """MinIO when the stack names one, moto otherwise.

    DELIBERATELY DOES NOT EMPTY THE BUCKET, unlike `test_object_storage`'s own
    fixture: several agents share this stack, and a sweep here would delete
    objects another run is mid-assertion about. Every object below is content
    addressed off a unique payload, so nothing collides without emptying it.

    It DOES create the bucket when it is missing, which is the one thing a
    shared stack routinely loses: the compose volume is recreated and the
    bucket goes with it. Creating it is what the stack declares anyway, and it
    is additive, so it cannot disturb another run.
    """
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    monkeypatch.setattr(object_storage, "get_settings", lambda: _Settings())
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    # See test_object_storage: boto3's process-wide DEFAULT_SESSION resolves
    # credentials once, so an earlier test in the same process can leave this
    # one signing with the machine's ambient key.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)

    if LIVE_ENDPOINT:
        monkeypatch.setenv(
            "AWS_ACCESS_KEY_ID", os.environ.get("S3_TEST_ACCESS_KEY", "readypick_test")
        )
        monkeypatch.setenv(
            "AWS_SECRET_ACCESS_KEY",
            os.environ.get("S3_TEST_SECRET_KEY", "readypick_test"),
        )
        probe = boto3.client(
            "s3",
            region_name=REGION,
            endpoint_url=LIVE_ENDPOINT,
            # Short and retry-free FOR THE PROBE ONLY: botocore's defaults
            # spend roughly twenty seconds retrying a refused connection.
            config=Config(
                connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}
            ),
        )
        try:
            probe.head_bucket(Bucket=BUCKET)
        except ClientError:
            probe.create_bucket(
                Bucket=BUCKET,
                CreateBucketConfiguration={"LocationConstraint": REGION},
            )
        except BotoCoreError as exc:
            raise AssertionError(
                f"S3_TEST_ENDPOINT_URL is {LIVE_ENDPOINT!r} but the endpoint "
                "could not be reached. Start the test stack with: "
                "docker compose -f docker-compose.test.yml up -d --wait. "
                f"Underlying error: {exc}"
            ) from exc
        object_storage.reset_client()
        yield
        object_storage.reset_client()
        return

    import moto

    with moto.mock_aws():
        object_storage.reset_client()
        boto3.client("s3", region_name=REGION).create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield
        object_storage.reset_client()


class World:
    def __init__(self) -> None:
        self.candidate = uuid.uuid4()
        self.user = uuid.uuid4()
        self.email = f"fresher-{self.candidate.hex[:10]}@bgvdocs.test"


@pytest.fixture
def world() -> Iterator[World]:
    if not _run(_reachable()):
        pytest.skip("no database with migration 0114 -- skipping BGV document test")
    w = World()
    sessions = _sessions()

    async def _seed() -> None:
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        sa.text(
                            "INSERT INTO users (id, tenant_id, email, full_name, "
                            " role, status) "
                            "VALUES (:id, NULL, :email, 'Meera Iyer', "
                            " 'candidate', 'active')"
                        ),
                        {"id": str(w.user), "email": w.email},
                    )
                    await session.execute(
                        sa.text(
                            "INSERT INTO candidates (id, user_id, full_name, email, "
                            " consent_databank, employment_background, created_at) "
                            "VALUES (:id, :uid, 'Meera Iyer', :email, false, "
                            " 'fresher', now())"
                        ),
                        # LINKED by user_id: the only thing a request resolves
                        # a candidate by (`candidate_identity`).
                        {"id": str(w.candidate), "uid": str(w.user), "email": w.email},
                    )

    async def _teardown() -> None:
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        sa.text("DELETE FROM candidates WHERE id = :id"),
                        {"id": str(w.candidate)},
                    )
                    await session.execute(
                        sa.text("DELETE FROM users WHERE id = :id"),
                        {"id": str(w.user)},
                    )

    _run(_seed())
    try:
        yield w
    finally:
        _run(_teardown())


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    sessions = _sessions()

    async def _candidate_db():
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    yield session

    # A candidate-audience principal, the only kind a candidate's cookie can
    # produce. `test_bgv_real_candidate_token.py` makes these calls with a real
    # session and no overrides.
    async def _current_candidate() -> CurrentUser:
        return CurrentUser(
            user_id=world.user,
            tenant_id=None,
            role=Role.candidate,
            audience=AUDIENCE_CANDIDATE,
        )

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_candidate_db] = _candidate_db
    app.dependency_overrides[get_current_candidate] = _current_candidate
    try:
        with TestClient(app) as http:
            yield http
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


#: The eight bytes every PNG opens with. Named once, because the validator
#: reads them and a test that mistyped them would be testing the typo.
_PNG_MAGIC = bytes([0x89]) + b"PNG\r\n" + bytes([0x1A, 0x0A])


def _png(seed: bytes) -> bytes:
    """A byte-valid PNG whose content is unique per `seed`.

    Real magic bytes, because the validator reads them; a unique tail, because
    the object key is a content address and two tests sharing a payload would
    share an object and make one test's delete the other's failure.
    """
    return _PNG_MAGIC + zlib.compress(seed + os.urandom(16))


def _seed_document(world: "World", kind: str, payload: bytes) -> str:
    """Put one object and record its row, the way the retired upload did."""
    import hashlib

    sha = hashlib.sha256(payload).hexdigest()
    stored = object_storage.put_if_absent(
        key=f"bgv-documents/{sha}",
        data=payload,
        content_type="image/png",
        metadata={"sha256": sha},
    )

    async def _insert() -> None:
        sessions = _sessions()
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    await session.execute(
                        sa.text(
                            "INSERT INTO candidate_bgv_documents (id, candidate_id, "
                            " document_type, object_key, original_filename, "
                            " mime_type, size_bytes, sha256, uploaded_at, created_at) "
                            "VALUES (:id, :cid, :kind, :key, 'degree.png', "
                            " 'image/png', :size, :sha, now(), now())"
                        ),
                        {
                            "id": str(uuid.uuid4()),
                            "cid": str(world.candidate),
                            "kind": kind,
                            "key": stored.key,
                            "size": len(payload),
                            "sha": sha,
                        },
                    )

    _run(_insert())
    return stored.key


# -- The upload route is retired -------------------------------------------


def test_there_is_no_upload_route(world: World, client: TestClient, storage):
    """Retired, not hidden: the method does not exist on the path."""
    response = client.post(
        f"{BGV}/me/documents",
        data={"document_type": "address_proof"},
        files={"file": ("proof.png", io.BytesIO(_png(b"x")), "image/png")},
    )
    assert response.status_code == 405
    openapi = app.openapi()["paths"]["/api/v1/bgv/me/documents"]
    assert "post" not in openapi
    assert set(openapi) == {"get"}


# -- Listing and withdrawal of legacy files --------------------------------


def test_a_legacy_document_is_listed_and_carries_no_object_key(
    world: World, client: TestClient, storage
):
    _seed_document(world, "academic_certificate", _png(b"degree-certificate"))
    listed = client.get(f"{BGV}/me/documents")
    assert listed.status_code == 200, listed.text
    [document] = listed.json()["documents"]
    assert document["original_filename"] == "degree.png"
    assert document["document_label"] == "Academic certificate"
    assert "object_key" not in document
    assert "bgv-documents" not in listed.text
    # No upload hints: nothing here invites a new upload.
    assert set(listed.json()) == {"documents"}


def test_a_candidate_can_withdraw_their_own_document(
    world: World, client: TestClient, storage
):
    key = _seed_document(world, "address_proof", _png(b"withdraw me"))
    document_id = client.get(f"{BGV}/me/documents").json()["documents"][0]["id"]
    removed = client.delete(f"{BGV}/me/documents/{document_id}")
    assert removed.status_code == 200
    assert removed.json()["documents"] == []
    assert object_storage.exists(key) is False
    assert client.delete(f"{BGV}/me/documents/{document_id}").status_code == 404


# -- The erasure hook -------------------------------------------------------


def test_erasure_enumerates_and_then_deletes_the_bytes(
    world: World, client: TestClient, storage
):
    """The assertion is a HEAD against the store, never a row count."""
    _seed_document(world, "academic_certificate", _png(b"erase-a"))
    _seed_document(world, "address_proof", _png(b"erase-b"))

    async def _enumerate() -> tuple[dict[str, str], ...]:
        sessions = _sessions()
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    return await bgv_documents.candidate_document_keys(
                        session, world.candidate
                    )

    keys = _run(_enumerate())
    assert len(keys) == 2
    assert {entry["kind"] for entry in keys} == {bgv_documents.KIND_BGV_DOCUMENT}
    for entry in keys:
        assert object_storage.exists(entry["key"]) is True

    async def _erase() -> int:
        sessions = _sessions()
        async with sessions() as session:
            async with session.begin():
                async with superadmin_scope(session):
                    return await bgv_documents.delete_candidate_documents(
                        session, world.candidate
                    )

    assert _run(_erase()) == 2
    for entry in keys:
        assert object_storage.exists(entry["key"]) is False
    # Enumerating afterwards is honestly empty, which is exactly why it has to
    # run BEFORE the cascade rather than after it.
    assert _run(_enumerate()) == ()


def test_documents_gate_nothing_for_a_fresher(world: World, client: TestClient, storage):
    """The offer gate's `not_required` path does not depend on any document."""
    empty = bgv_workflow.derive_status(
        background="fresher", employer_count=0, statuses=[]
    )
    _seed_document(world, "academic_certificate", _png(b"gates-nothing"))
    still = bgv_workflow.derive_status(
        background="fresher", employer_count=0, statuses=[]
    )
    assert empty == still == "not_required"
