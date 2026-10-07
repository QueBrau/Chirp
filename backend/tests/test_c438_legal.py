"""c438 legal acceptance invariants that do not require a provider or database."""
import pytest
from pydantic import ValidationError

from app.schemas.legal import LegalAcceptanceCreate


def test_seventeen_requires_explicit_guardian_confirmation() -> None:
    with pytest.raises(ValidationError, match="guardian_permission_required"):
        LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=17)


def test_eighteen_does_not_require_guardian_confirmation() -> None:
    value = LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=18)
    assert value.guardian_permission_confirmed is False


def test_seventeen_with_confirmation_is_valid() -> None:
    value = LegalAcceptanceCreate(terms_version="2026-10-06", privacy_version="2026-10-06", age_declaration=17, guardian_permission_confirmed=True)
    assert value.age_declaration == 17
