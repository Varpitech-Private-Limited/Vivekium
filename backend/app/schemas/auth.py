"""Auth request/response schemas (API_CONTRACT.md `/auth`, rev 2).

The one-time-code login schemas (request, verify, candidate self-registration)
were deleted on 2026-09-24 with the unrouted handlers that were their only
users. The session response they shared was renamed `SessionOut`, because it
is what every live sign-in route answers and it never had anything to do with
a code. Its JSON is unchanged except that the pending-channel list is gone: it only
ever carried the retired dual-channel gate, and no screen ever read it.
"""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import Role


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: Role
    tenant_id: uuid.UUID | None
    full_name: str | None
    # Optional: a phone-only candidate (Firebase phone provider) has no email.
    email: str | None
    email_verified: bool
    phone_verified: bool
    password_enabled: bool = False
    # Rendered by every authenticated shell so a legitimate multi-tenant user
    # can always see which workspace owns the current session cookie.
    workspace_name: str


class ContextOut(BaseModel):
    """One selectable workspace when an identifier matches multiple users
    (three portals, ONE login, contract rev 2)."""
    user_id: uuid.UUID
    role: Role
    tenant_id: uuid.UUID | None
    tenant_name: str | None
    portal: Literal["owner", "org", "candidate"]


class SessionOut(BaseModel):
    """What `/auth/firebase/session`, `/auth/workspaces` and
    `/auth/select-context` answer."""

    # Exactly one matching user: `user` + `capabilities` (cookies set).
    user: UserOut | None = None
    capabilities: list[str] | None = None
    # Multiple matching users: workspace chooser, no cookies until
    # /auth/select-context.
    contexts: list[ContextOut] | None = None
    context_token: str | None = None


class SelectContextIn(BaseModel):
    context_token: str = Field(min_length=10)
    user_id: uuid.UUID


class MeOut(BaseModel):
    user: UserOut
    capabilities: list[str] = []


#: Sign-in surfaces. The session exchange reads `captcha_purpose` as the portal
#: filter (A-24, 2026-10-30). The wire name is unchanged so a page that still
#: sends it keeps working. It is not a challenge, and nothing checks a proof.
CaptchaPurpose = Literal[
    "candidate_login",
    "candidate_register",
    "company_login",
    "company_register",
    "invite_join",
    "provider_login",
    "bd_login",
    "password_change",
    "password_reset",
]

#: The purposes a SESSION EXCHANGE may carry. Each names the surface the
#: person signed in on, and the surface narrows which workspaces the proven
#: identity may enter (api/auth.EXCHANGE_PURPOSE_PORTAL). The two password
#: purposes and company registration never mint a session through here.
ExchangePurpose = Literal[
    "candidate_login",
    "candidate_register",
    "company_login",
    "invite_join",
    "provider_login",
    "bd_login",
]


class DemoSessionIn(BaseModel):
    """Public demo sign-in. The password is accepted and not stored.

    Firebase remains the real sign-in. This body is refused with 404 when
    DEMO_MODE is off, and it never binds a row that is not public_demo.
    """

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    portal: Literal["candidate", "company", "provider", "bd"]
    full_name: str | None = Field(default=None, max_length=200)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a password.")
        return value


class DemoStatusOut(BaseModel):
    demo_mode: bool


class FirebaseSessionIn(BaseModel):
    """A verified Firebase identity for one sign-in surface.

    The surface (`captcha_purpose`) is the portal filter: a filter over the
    database's own roles, never a grant. `captcha_proof` is accepted and not
    read, so a page that still sends one is not refused (A-24). Nothing on
    this body is a challenge.
    """

    model_config = ConfigDict(extra="forbid")

    id_token: str = Field(min_length=20)
    captcha_purpose: ExchangePurpose
    captcha_proof: str | None = Field(default=None, max_length=128)


class SecurityCodeSentOut(BaseModel):
    """The same answer whether or not an account exists (enumeration safe)."""
    sent: bool = True
    message: str


class PasswordResetRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    #: Accepted and not read (A-24). An older page may still send it.
    captcha_proof: str | None = Field(default=None, max_length=128)


class PasswordResetVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    code: str = Field(min_length=6, max_length=12)


class PasswordResetVerifyOut(BaseModel):
    reset_token: str


class PasswordResetCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reset_token: str = Field(min_length=16, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: Accepted and not read (A-24). An older page may still send it.
    captcha_proof: str | None = Field(default=None, max_length=128)


class PasswordChangeVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=12)


class PasswordChangeVerifyOut(BaseModel):
    change_token: str


class PasswordChangeCompleteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_token: str = Field(min_length=16, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class InviteSetupPasswordIn(BaseModel):
    """`POST /companies/invites/{token}/setup-password`. There is no email
    field on purpose: the address is the invitation's, never the browser's."""

    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=256)
    #: Accepted and not read (A-24). An older page may still send it.
    captcha_proof: str | None = Field(default=None, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)


class PasswordChangedOut(BaseModel):
    password_changed: bool = True
    #: Sentence the screen shows as it stands.
    message: str
