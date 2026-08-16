"""Alumni schemas: alumni profiles, directory entries, job posts."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ---- alumni profile (GET/PUT /alumni/profile, GET /alumni/directory) ----


class AlumniProfileUpdate(_Schema):
    """Body for PUT /alumni/profile — upserts the caller's profile."""

    grad_year: int | None = None
    company: str | None = None
    title: str | None = None
    industry: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    open_to_mentoring: bool = False


class AlumniProfileOut(_Schema):
    user_id: uuid.UUID
    grad_year: int | None = None
    company: str | None = None
    title: str | None = None
    industry: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    open_to_mentoring: bool = False
    display_name: str | None = None  # joined from users for directory views
    email: str | None = None  # joined from users — contact for directory


# ---- job posts (/jobs CRUD) ----


class JobPostCreate(_Schema):
    chapter_id: uuid.UUID | None = None  # NULL = network-wide
    title: str = Field(min_length=1)
    company: str = Field(min_length=1)
    location: str = Field(min_length=1)  # city, metro, or "Remote"
    description: str = Field(min_length=1)
    apply_url: str | None = None
    expires_at: datetime | None = None


class JobPostUpdate(_Schema):
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    apply_url: str | None = None
    expires_at: datetime | None = None


class JobPostOut(_Schema):
    id: uuid.UUID
    posted_by: uuid.UUID
    chapter_id: uuid.UUID | None = None
    title: str
    company: str
    location: str | None = None
    description: str
    apply_url: str | None = None
    created_at: datetime
    expires_at: datetime | None = None
    # Resolved from users.display_name by the list route's join, mirroring how
    # AlumniProfileOut.display_name is populated. There is no GET /users/{id},
    # so without this the client has only a bare UUID and cannot show who
    # posted a job. Optional because the create/delete routes return a job
    # without running the join.
    posted_by_name: str | None = None
