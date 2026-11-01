"""Candidate verification-document upload is retired (production-fix pass, 2026-11).

The list and withdraw routes and the erasure hook stay (see
`docs/operations/BGV_LEGACY_DOCUMENTS.md`); the upload path, its settings and
the profile card are gone and nothing may name them again.
"""
from __future__ import annotations

import re

from app.main import app
from tests.removal_sweep import BACKEND, REPO, sweep

THIS_FILE = BACKEND / "tests" / "test_bgv_documents_retired.py"

GONE = re.compile(
    r"bgv_documents.store_document|read_validated_upload|upload_limits_hint|bgv_document_max_bytes"
    r"|bgv_documents_max_per_type|bgv_document_upload|BgvDocumentsCard|bgv-documents-card"
)


def test_the_sweep_pattern_matches_a_sample() -> None:
    assert GONE.search("BgvDocumentsCard")


def test_nothing_names_the_retired_upload() -> None:
    assert not sweep(GONE, exempt=(THIS_FILE,))


def test_the_card_file_is_gone() -> None:
    assert not (REPO / "frontend" / "components" / "bgv-documents-card.tsx").exists()


def test_no_staff_route_reaches_documents() -> None:
    """Companies see readiness only: no non-candidate route serves a document."""
    documents = [
        path for path in app.openapi()["paths"] if "document" in path and "/bgv" in path
    ]
    assert documents  # positive control: the candidate routes exist
    assert all(path.startswith("/api/v1/bgv/me/documents") for path in documents), documents
