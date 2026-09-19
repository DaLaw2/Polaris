"""What a read returns: a work as resolved through the claim layer."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class FolderMetadata:
    """Structured metadata parsed from a folder name."""

    raw_name: str
    artist: Optional[str] = None
    title: Optional[str] = None
    series: Optional[str] = None
    language: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    source_path: Optional[str] = None


@dataclass
class WorkAnalysis:
    """Complete analysis result for one work.

    Every field resolved through the claim layer; see
    `works_effective` in derivation/schema.py.
    """

    folder_path: str
    folder_name: str

    metadata: FolderMetadata

    collection: str | None = None

    general_tags: dict[str, float] = field(default_factory=dict)
    character_tags: dict[str, float] = field(default_factory=dict)
    copyright_tags: dict[str, float] = field(default_factory=dict)
    artist_tags: dict[str, float] = field(default_factory=dict)

    rating: str = "general"
    rating_scores: dict[str, float] = field(default_factory=dict)

    color_mode: str = "grayscale"

    work_type: str = "comic"

    total_images: int = 0
    has_video: bool = False
    duration_s: float | None = None

    analyzed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class SearchResult:
    """A search result entry with relevance scoring."""

    work: WorkAnalysis
    score: float = 0.0
    matched_tags: list[str] = field(default_factory=list)
