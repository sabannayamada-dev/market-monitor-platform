from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PaperRecord:
    source: str
    source_id: str
    title: str
    abstract: str = ""
    authors: tuple[str, ...] = ()
    published_at: str = ""
    updated_at: str = ""
    doi: str = ""
    arxiv_id: str = ""
    journal: str = ""
    categories: tuple[str, ...] = ()
    landing_url: str = ""
    pdf_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoredPaper:
    paper_id: str
    profile_id: str
    profile_label: str
    score: float
    reasons: tuple[str, ...]
