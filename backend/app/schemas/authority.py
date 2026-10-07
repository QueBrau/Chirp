"""Explicit organization authority declarations, separate from account consent."""
import uuid

from pydantic import ConfigDict, Field, StrictBool, model_validator

from app.schemas.base import _Schema


class OrganizationAuthorityInput(_Schema):
    model_config = ConfigDict(extra="forbid")
    policy_version: str = Field(min_length=1, max_length=40)
    confirmed: StrictBool

    @model_validator(mode="after")
    def explicit_confirmation(self):
        if not self.confirmed:
            raise ValueError("organization_authority_confirmation_required")
        return self


class PaymentAuthorityInput(OrganizationAuthorityInput):
    membership_id: uuid.UUID
    role_term_id: uuid.UUID
    role: str = Field(min_length=1, max_length=40)
    stripe_account_id: str | None = Field(max_length=255)


class PaymentAuthorityContext(_Schema):
    policy_version: str
    chapter_id: uuid.UUID
    org_name: str
    membership_id: uuid.UUID
    role_term_id: uuid.UUID
    role: str
    stripe_account_id: str | None
