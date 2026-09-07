"""Authenticated deployment evidence; deliberately separate from DB-free liveness."""
import os
from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.middleware.auth import get_current_user
from app.models.identity import User

router = APIRouter(tags=["deployment"])


class DeploymentOut(BaseModel):
    service: str
    revision: str
    code_schema_heads: list[str]
    database_schema_heads: list[str]


@lru_cache(maxsize=1)
def packaged_schema_heads() -> tuple[str, ...]:
    # /srv/alembic in the image; backend/alembic locally. Never depend on cwd.
    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[2] / "alembic"))
    return tuple(sorted(scripts.get_heads()))


@router.get("/_deployment", response_model=DeploymentOut)
async def deployment_evidence(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> DeploymentOut:
    heads = (await session.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num"))).scalars().all()
    return DeploymentOut(
        service=os.environ.get("K_SERVICE", "local"),
        revision=os.environ.get("K_REVISION", "local"),
        code_schema_heads=list(packaged_schema_heads()),
        database_schema_heads=list(heads),
    )
