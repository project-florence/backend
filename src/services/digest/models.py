"""Pydantic models for the market digest feature.

Mirrors the style of ``src/finance/models.py`` (Pydantic v2, ``BaseModel`` +
``Field``). The ``Digest`` is the persisted/cached wire format: a generated
daily summary with its metadata, sections and language.
"""

import uuid
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class DigestSection(BaseModel):
    heading: str
    body: str


class Digest(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    date: date
    slot: Literal["morning", "noon", "evening"]
    title: str
    content: str
    sections: list[DigestSection] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    language: str = "tr"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
