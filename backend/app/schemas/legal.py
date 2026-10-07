"""Wire schemas for current legal policy status and acceptance (c438)."""
from datetime import datetime
from typing import Literal
import uuid
from pydantic import Field, model_validator
from app.schemas.base import _Schema

PolicyKey = Literal["terms", "privacy"]

class PolicyOut(_Schema):
    key: PolicyKey
    version: str
    effective_at: datetime

class LegalStatusOut(_Schema):
    required: bool
    policies: list[PolicyOut]
    accepted_policy_ids: list[uuid.UUID] = []

class LegalAcceptanceCreate(_Schema):
    terms_version: str = Field(min_length=1, max_length=40)
    privacy_version: str = Field(min_length=1, max_length=40)
    age_declaration: Literal[17, 18]
    guardian_permission_confirmed: bool = False
    source: Literal["mobile", "web", "bootstrap"] = "mobile"

    @model_validator(mode="after")
    def guardian_for_minor(self) -> "LegalAcceptanceCreate":
        if self.age_declaration == 17 and not self.guardian_permission_confirmed:
            raise ValueError("guardian_permission_required")
        return self
