"""Request and response shapes for `/api/v1/company-onboarding` (owner spec 5).

Every input model forbids unknown fields, so a caller cannot slip a role, a
tenant, a status or a From address into a registration. The industry is the
ONE industry vocabulary (`schemas.admin.Industry`), the list the Provider's
onboarding form and the customer edit modal already use.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.schemas.admin import Industry
from app.services import company_onboarding

__all__ = [
    "ActivateIn",
    "CodeResendIn",
    "CodeVerifyIn",
    "OnboardingPurchaseIn",
    "OnboardingStateOut",
    "DemoCompanyIn",
    "RegistrationAcceptedOut",
    "RegistrationIn",
]

Stage = Literal["details", "verify_email", "choose_pack", "set_password", "done"]


def _trimmed(value: str, label: str) -> str:
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise ValueError(f"{label} is required")
    return cleaned


class RegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(max_length=100)
    last_name: str = Field(max_length=100)
    email: EmailStr
    phone: str = Field(max_length=32)
    company_name: str = Field(max_length=255)
    industry: Industry
    industry_other: str | None = Field(default=None, max_length=100)
    captcha_proof: str = Field(min_length=1, max_length=128)

    @field_validator("first_name")
    @classmethod
    def _first(cls, value: str) -> str:
        return _trimmed(value, "First name")

    @field_validator("last_name")
    @classmethod
    def _last(cls, value: str) -> str:
        return _trimmed(value, "Last name")

    @field_validator("company_name")
    @classmethod
    def _company(cls, value: str) -> str:
        return _trimmed(value, "Company name")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return company_onboarding.normal_email(str(value))

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return company_onboarding.canonical_phone(value)

    @model_validator(mode="after")
    def _other_industry(self) -> "RegistrationIn":
        other = " ".join((self.industry_other or "").split()) or None
        if self.industry == "Other":
            if other is None:
                raise ValueError("Tell us your industry when you choose Other")
            self.industry_other = other
        else:
            # A value typed and then abandoned by switching the dropdown back
            # is not the company's industry.
            self.industry_other = None
        return self


class DemoCompanyIn(BaseModel):
    """Public demo registration. No CAPTCHA. The password is not stored."""

    model_config = ConfigDict(extra="forbid")

    first_name: str = Field(max_length=100)
    last_name: str = Field(max_length=100)
    email: EmailStr
    phone: str = Field(max_length=32)
    company_name: str = Field(max_length=255)
    industry: Industry
    industry_other: str | None = Field(default=None, max_length=100)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("first_name")
    @classmethod
    def _first(cls, value: str) -> str:
        return _trimmed(value, "First name")

    @field_validator("last_name")
    @classmethod
    def _last(cls, value: str) -> str:
        return _trimmed(value, "Last name")

    @field_validator("company_name")
    @classmethod
    def _company(cls, value: str) -> str:
        return _trimmed(value, "Company name")

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return company_onboarding.normal_email(str(value))

    @field_validator("phone")
    @classmethod
    def _phone(cls, value: str) -> str:
        return company_onboarding.canonical_phone(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a password.")
        return value

    @model_validator(mode="after")
    def _other_industry(self) -> "DemoCompanyIn":
        other = " ".join((self.industry_other or "").split()) or None
        if self.industry == "Other":
            if other is None:
                raise ValueError("Tell us your industry when you choose Other")
            self.industry_other = other
        else:
            self.industry_other = None
        return self


class CodeVerifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    code: str = Field(min_length=1, max_length=12)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return company_onboarding.normal_email(str(value))


class CodeResendIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return company_onboarding.normal_email(str(value))


class RegistrationAcceptedOut(BaseModel):
    """The SAME answer whether or not the address can register (5.3,
    enumeration safe)."""

    message: str


class OnboardingStateOut(BaseModel):
    """Where this browser's registration stands. DERIVED on every read."""

    stage: Stage
    email: str | None = None
    first_name: str | None = None
    company_name: str | None = None


class OnboardingPurchaseIn(BaseModel):
    """A named pack. The custom amount is Enterprise, by conversation and never
    self-serve (the billing page's rule), so it is not offered here either."""

    model_config = ConfigDict(extra="forbid")

    pack_slug: str = Field(min_length=1, max_length=30)


class ActivateIn(BaseModel):
    """Exactly one of: a NEW password, or the Firebase ID token of an identity
    this address already has (proven by signing in with its password)."""

    model_config = ConfigDict(extra="forbid")

    password: str | None = Field(default=None, max_length=128)
    id_token: str | None = Field(default=None, min_length=10, max_length=8192)

    @model_validator(mode="after")
    def _one(self) -> "ActivateIn":
        if (self.password is None) == (self.id_token is None):
            raise ValueError("Send a new password or an existing sign-in, not both")
        return self
