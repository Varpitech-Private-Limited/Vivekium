"""Request and response shapes for background verification.

WHAT NEVER CROSSES THIS BOUNDARY
----------------------------------
An HR contact's email address reaches the recruiter who is running the
verification and nobody else. It is not on any list endpoint and it never
reaches another tenant: it is a third party's personal contact detail that a
candidate handed over for one purpose.

The candidate-level status is a WORD from a closed set, never a count and never
a percentage, for the same reason a grade is a word: a number invites somebody
to treat "5 of 7" as a score.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.models.employment import BACKGROUND_EXPERIENCED, BACKGROUND_FRESHER

#: Generous, and a limit nonetheless. The brief's worked example is seven; a
#: real senior candidate can have more. What this refuses is a submission with
#: hundreds of rows, which is an abuse surface rather than a career.
MAX_EMPLOYERS = 40


class EmploymentIn(BaseModel):
    """One previous employer, as the candidate enters it."""

    employer_name: str = Field(min_length=1, max_length=200)
    designation: str = Field(min_length=1, max_length=200)
    started_on: date
    ended_on: date
    hr_name: str = Field(min_length=1, max_length=200)
    hr_email: EmailStr

    @model_validator(mode="after")
    def _dates_make_sense(self) -> "EmploymentIn":
        if self.ended_on < self.started_on:
            raise ValueError(
                "The end date cannot be before the start date. An employer "
                "asked to confirm that period would not know what to say."
            )
        # A future end date on a PREVIOUS employer is a typo or a current job.
        # Either way the verification email would ask about employment that has
        # not finished.
        if self.ended_on > date.today():
            raise ValueError(
                "The end date is in the future. This section is for previous "
                "employers only."
            )
        return self


class EmploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employer_name: str
    designation: str
    started_on: date
    ended_on: date
    hr_name: str
    #: Present for the CANDIDATE (it is their own entry) and for the recruiter
    #: running the verification. Never on a cross-tenant response.
    hr_email: str | None = None


class OwnEmploymentOut(EmploymentOut):
    """One employer on the CANDIDATE's own history, and nowhere else.

    `correction_needed` is True while a request to this employer bounced and
    was never answered (`bgv_delivery.employments_needing_correction`, the same
    predicate `PUT /bgv/me/employers/{id}/hr-email` accepts a correction on),
    so the screen offers the correction exactly where the route would take it.

    A subclass rather than a field on `EmploymentOut`: that shape is also the
    recruiter's, and a flag that is always False on their side would read as a
    statement that nothing bounced. It is required, never defaulted, so a
    candidate response that forgot to compute it fails validation instead of
    saying "nothing to fix".
    """

    correction_needed: bool


class EmploymentHistoryIn(BaseModel):
    """The candidate's whole declaration, submitted in one act.

    `finalize` is the one-way door. It is a field rather than a separate
    endpoint so the warning the candidate reads and the flag the server acts on
    are the same decision, made once.
    """

    background: str
    employments: list[EmploymentIn] = Field(default_factory=list)
    finalize: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> "EmploymentHistoryIn":
        if self.background not in {BACKGROUND_FRESHER, BACKGROUND_EXPERIENCED}:
            raise ValueError("Choose either fresher or experienced.")
        if self.background == BACKGROUND_FRESHER and self.employments:
            raise ValueError(
                "A fresher has no previous employers to verify. Choose "
                "experienced if you have worked before."
            )
        if len(self.employments) > MAX_EMPLOYERS:
            raise ValueError(f"At most {MAX_EMPLOYERS} previous employers.")
        if (
            self.finalize
            and self.background == BACKGROUND_EXPERIENCED
            and not self.employments
        ):
            raise ValueError(
                "Add at least one previous employer before submitting, or "
                "choose fresher."
            )
        return self


class EmploymentHistoryOut(BaseModel):
    background: str | None
    finalized: bool
    finalized_at: datetime | None
    employments: list[OwnEmploymentOut]
    #: What the candidate is told BEFORE they submit. Served from the server so
    #: the warning and the rule it describes cannot drift apart.
    submission_warning: str


class VerificationOut(BaseModel):
    """One employer's verification, as the recruiter sees it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    employment: EmploymentOut
    status: str
    conversation_id: uuid.UUID | None
    first_sent_at: datetime | None
    responded_at: datetime | None
    decided_at: datetime | None
    decided_by_name: str | None
    decision_note: str | None
    #: DID IT ARRIVE, which is a different question from what the employer
    #: said. `pending` with `delivery_status="bounced"` is a request nobody
    #: received, and without this the screen shows it as an HR team who has
    #: not got round to it yet.
    delivery_status: str = "not_sent"
    #: When the employer's three days actually began. NULL under a transport
    #: that reports no delivery events, which is honest rather than absent:
    #: `bgv_delivery.clock_start` says what the product falls back to.
    delivered_at: datetime | None = None
    bounced_at: datetime | None = None
    #: The provider's short reason for a refusal, for the recruiter's screen.
    delivery_detail: str | None = None


class CandidateBGVOut(BaseModel):
    """The whole picture for one candidate, for one tenant."""

    candidate_id: uuid.UUID
    candidate_name: str | None
    background: str | None
    required: bool
    status: str
    employer_count: int
    verified_count: int
    verifications: list[VerificationOut]
    #: None when nothing is blocked. The exact sentence the pipeline would
    #: refuse an offer with, so the screen and the server never disagree.
    offer_blocked_reason: str | None


class HREmailCorrectionIn(BaseModel):
    """The candidate's replacement address for an employer who could not be reached.

    ONE FIELD, and that is the security property rather than a convenience.
    The employment facts (employer, title, dates) are immutable by database
    trigger and stay that way; a schema that carried them would be a schema
    somebody later wires through, and a candidate who could edit the dates
    could rewrite the claim an employer is being asked to confirm.
    """

    hr_email: EmailStr


class BGVDocumentOut(BaseModel):
    """One of a fresher's own verification documents.

    NO OBJECT KEY. The row names a file the candidate uploaded; where the bytes
    live is not something an API boundary has any reason to carry
    (claude.md, 2026-07-26: never name a storage vendor in user-facing copy,
    and a key is the address of a bucket by another route).
    """

    id: uuid.UUID
    document_type: str
    document_label: str
    original_filename: str
    mime_type: str
    size_bytes: int
    uploaded_at: datetime


class BGVDocumentsOut(BaseModel):
    """A candidate's legacy documents (upload is retired; list and withdraw only)."""

    documents: list[BGVDocumentOut]


class DraftOut(BaseModel):
    """A generated verification email, before anybody sends it."""

    verification_id: uuid.UUID
    subject: str
    body: str
    #: FALSE when the deterministic template was used. Shown to the recruiter,
    #: because template output presented as generation is a lie about how the
    #: text was produced.
    generated_by_ai: bool


class SendIn(BaseModel):
    """What the recruiter actually sends, after editing.

    The subject and body are taken from the request, not regenerated: the whole
    point of the review step is that the recruiter's version is the one that
    goes out.
    """

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20_000)


class DecisionIn(BaseModel):
    """The recruiter's manual verdict on one employer.

    A person decides this after reading the reply. Nothing infers it from the
    email text: the brief says so, and an inferred "verified" would be an
    automated hiring decision wearing a convenience feature's clothes.
    """

    verified: bool
    note: str | None = Field(default=None, max_length=4000)
