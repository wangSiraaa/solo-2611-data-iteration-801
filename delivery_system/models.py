"""Immutable value objects shared by the catalog and the delivery side."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SourceSegment:
    """One segment of a source document.

    ``segment_id`` is the identity; ``position`` is only the current ordinal
    in the document and may change on reorder.  ``revision`` is per-segment.
    """

    segment_id: str
    position: int
    revision: int
    text: str


@dataclass(frozen=True)
class SourceProduct:
    """Immutable, ordered view of a source document as published by the catalog."""

    document_id: str
    document_revision: int
    segments: tuple[SourceSegment, ...]

    def __post_init__(self) -> None:
        positions = [seg.position for seg in self.segments]
        if positions != list(range(len(self.segments))):
            raise ValueError(f"source product positions are not 0..n-1: {positions}")
        ids = [seg.segment_id for seg in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("source product contains duplicate segment ids")

    def index_by_segment_id(self) -> dict[str, SourceSegment]:
        return {seg.segment_id: seg for seg in self.segments}

    @property
    def segment_ids(self) -> tuple[str, ...]:
        return tuple(seg.segment_id for seg in self.segments)

    @property
    def revision_map(self) -> dict[str, int]:
        return {seg.segment_id: seg.revision for seg in self.segments}

    def snapshot(self) -> list[dict[str, object]]:
        """The package-format source snapshot: identity + revision, in order."""

        return [
            {"segment_id": seg.segment_id, "revision": seg.revision}
            for seg in self.segments
        ]


@dataclass(frozen=True)
class SourceRef:
    """One ``(segment_id, revision)`` reference inside a translation entry."""

    segment_id: str
    revision: int


@dataclass(frozen=True)
class TranslationEntry:
    "Store exactly the revision that was translated; never re-label it."""

    translation_id: str
    document_id: str
    language: str
    text: str
    source_refs: tuple[SourceRef, ...]


@dataclass(frozen=True)
class TranslationAssessment:
    """Result of checking one entry against the *current* source product.

    ``state`` is one of: ``current_usable``, ``stale_revision``,
    ``non_contiguous``, ``unknown_segment``, ``malformed``, ``empty_text``.
    ``start``/``end`` are zero-based half-open positions and are populated
    only for ``current_usable`` entries.
    """

    entry: TranslationEntry
    state: str
    issues: tuple[str, ...]
    start: Optional[int] = None
    end: Optional[int] = None

    @property
    def usable(self) -> bool:
        return self.state == "current_usable"


@dataclass(frozen=True)
class CoverChunk:
    """One selected translation entry, positioned in the source order."""

    translation_id: str
    source_ids: tuple[str, ...]
    source_revisions: tuple[int, ...]
    text: str
    start: int
    end: int  # half-open

    def to_package_chunk(self) -> dict[str, object]:
        return {
            "translation_id": self.translation_id,
            "source_ids": list(self.source_ids),
            "source_revisions": list(self.source_revisions),
            "text": self.text,
        }


@dataclass(frozen=True)
class CoverDiagnosis:
    """Why a complete delivery could not be produced."""

    reason: str
    detail: str
    blocked_boundary: Optional[int]
    uncovered_segment_ids: tuple[str, ...]
    crossing_translation_ids: tuple[str, ...]
    excluded_translations: tuple[tuple[str, str], ...]
    union_covers_all: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "blocked_boundary": self.blocked_boundary,
            "uncovered_segment_ids": list(self.uncovered_segment_ids),
            "crossing_translation_ids": list(self.crossing_translation_ids),
            "excluded_translations": [list(x) for x in self.excluded_translations],
            "union_covers_all": self.union_covers_all,
        }


@dataclass(frozen=True)
class CoverResult:
    ok: bool
    product: SourceProduct
    language: str
    chunks: tuple[CoverChunk, ...]
    rendered_text: Optional[str]
    diagnosis: Optional[CoverDiagnosis]
