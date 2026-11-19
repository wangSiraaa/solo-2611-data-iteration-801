"""Exact maximum-coverage selection over current, contiguous translation entries.

A translation entry may only cover one *non-empty contiguous interval* of the
current source order.  An exact cover for a language is an ordered set of
entries covering every source segment exactly once: no gap, no overlap, and no
entry split in half.

Checking only the union of covered segments is not enough, because overlapping
entries can cover the union while making an ordered, non-overlapping assembly
impossible.  The dynamic program below therefore works on intervals, not sets.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Optional

from .models import (
    CoverChunk,
    CoverDiagnosis,
    CoverResult,
    SourceProduct,
    TranslationAssessment,
    TranslationEntry,
)

# Only current_usable entries can be selected; every other state is
# historical / diagnostic material.


def assess_entry(
    entry: TranslationEntry,
    product: SourceProduct,
    language: str,
    index: dict[str, object],
) -> TranslationAssessment:
    if entry.language != language:
        return TranslationAssessment(
            entry=entry,
            state="other_language",
            issues=(f"language is {entry.language!r}, expected {language!r}",),
        )

    issues: list[str] = []
    refs = entry.source_refs
    if not refs:
        return TranslationAssessment(
            entry=entry, state="malformed", issues=("source_refs is empty",)
        )

    ref_ids = [ref.segment_id for ref in refs]
    duplicate = len(set(ref_ids)) != len(ref_ids)
    if duplicate:
        issues.append("duplicate segment reference")
    unknown = [sid for sid in ref_ids if sid not in index]
    if unknown:
        issues.append("unknown segment(s): " + ",".join(unknown))
    stale = [
        f"{ref.segment_id}@{ref.revision}!={index[ref.segment_id].revision}"
        for ref in refs
        if ref.segment_id in index and index[ref.segment_id].revision != ref.revision
    ]
    if stale:
        issues.append("stale revision(s): " + ",".join(stale))
    empty_text = not entry.text.strip()
    if empty_text:
        issues.append("text is empty")

    positions: Optional[list[int]] = None
    contiguous = False
    if not duplicate and not unknown:
        positions = [index[sid].position for sid in ref_ids]
        expected = list(range(positions[0], positions[0] + len(positions)))
        contiguous = positions == expected
        if not contiguous:
            issues.append(
                "source_refs do not form a contiguous in-order interval: "
                + ",".join(
                    f"{sid}@{pos}" for sid, pos in zip(ref_ids, positions)
                )
            )

    if duplicate:
        state = "malformed"
    elif unknown:
        state = "unknown_segment"
    elif stale:
        state = "stale_revision"
    elif empty_text:
        state = "empty_text"
    elif not contiguous:
        state = "non_contiguous"
    else:
        state = "current_usable"

    if state == "current_usable":
        assert positions is not None
        return TranslationAssessment(
            entry=entry,
            state=state,
            issues=(),
            start=positions[0],
            end=positions[0] + len(positions),
        )
    return TranslationAssessment(entry=entry, state=state, issues=tuple(issues))


def assess_entries(
    entries: Iterable[TranslationEntry], product: SourceProduct, language: str
) -> list[TranslationAssessment]:
    index = product.index_by_segment_id()
    return [
        assess_entry(entry, product, language, index)
        for entry in sorted(entries, key=lambda e: e.translation_id)
    ]


def _diagnose(
    product: SourceProduct,
    language: str,
    assessments: list[TranslationAssessment],
    usable: list[TranslationAssessment],
) -> CoverDiagnosis:
    n = len(product.segments)
    ids = product.segment_ids

    if n == 0:
        return CoverDiagnosis(
            reason="empty_source",
            detail=(
                "the source document has no segments; refusing to publish an "
                "empty delivery package"
            ),
            blocked_boundary=None,
            uncovered_segment_ids=(),
            crossing_translation_ids=(),
            excluded_translations=tuple(
                (item.entry.translation_id, item.state)
                for item in assessments
                if not item.usable
            ),
            union_covers_all=False,
        )

    covered: set[int] = set()
    for item in usable:
        covered.update(range(item.start, item.end))
    gaps = [pos for pos in range(n) if pos not in covered]
    uncovered_ids = tuple(ids[pos] for pos in gaps)

    # Reachability frontier: a dead end is a reachable prefix position with no
    # usable entry starting there, which is where any assembly would stop.
    starts: dict[int, list[TranslationAssessment]] = defaultdict(list)
    for item in usable:
        starts[item.start].append(item)
    reachable = [False] * (n + 1)
    reachable[0] = True
    for pos in range(n):
        if reachable[pos]:
            for item in starts.get(pos, ()):
                reachable[item.end] = True

    dead_end: Optional[int] = None
    for pos in range(n):
        if reachable[pos] and not starts.get(pos):
            dead_end = pos
            break
    if dead_end is None:
        dead_end = next((pos for pos in range(n + 1) if not reachable[pos]), None)

    crossing = tuple(
        sorted(
            {
                item.entry.translation_id
                for item in usable
                if dead_end is not None and item.start < dead_end < item.end
            }
        )
    )

    excluded = tuple(
        (item.entry.translation_id, item.state)
        for item in assessments
        if not item.usable
    )

    if gaps:
        reason = "no_current_cover"
        gap_desc = ", ".join(
            f"{ids[pos]} (position {pos})" for pos in gaps[:12]
        )
        if len(gaps) > 12:
            gap_desc += f", … (+{len(gaps) - 12} more)"
        detail = (
            f"no current {language!r} translation covers source segment(s): {gap_desc}. "
            "The remaining entries cannot form a complete, non-overlapping cover."
        )
    else:
        reason = "no_exact_cover"
        detail = (
            f"the union of current {language!r} translations covers all "
            f"{n} source segments, but no ordered set covers them exactly once: "
            "assembling from the start reaches a blocked boundary at position "
            f"{dead_end}, where only entries that cross the boundary exist. "
            "Entries cannot be split, so no exact cover exists."
        )

    return CoverDiagnosis(
        reason=reason,
        detail=detail,
        blocked_boundary=dead_end,
        uncovered_segment_ids=uncovered_ids,
        crossing_translation_ids=crossing,
        excluded_translations=excluded,
        union_covers_all=not gaps,
    )


def solve_cover(
    product: SourceProduct,
    language: str,
    entries: Iterable[TranslationEntry],
) -> CoverResult:
    assessments = assess_entries(entries, product, language)
    usable = [item for item in assessments if item.usable]
    n = len(product.segments)

    starts: dict[int, list[TranslationAssessment]] = defaultdict(list)
    for item in usable:
        starts[item.start].append(item)
    for bucket in starts.values():
        bucket.sort(key=lambda item: item.entry.translation_id)

    # Reachability DP over source positions.  Any legal exact cover is fine;
    # this picks the lexicographically smallest-by-id path deterministically.
    reachable = [False] * (n + 1)
    parent: list[Optional[TranslationAssessment]] = [None] * (n + 1)
    reachable[0] = True
    for pos in range(n):
        if not reachable[pos]:
            continue
        for item in starts.get(pos, ()):
            if item.end <= n and not reachable[item.end]:
                reachable[item.end] = True
                parent[item.end] = item

    if n > 0 and reachable[n]:
        chosen: list[TranslationAssessment] = []
        cursor = n
        while cursor > 0:
            item = parent[cursor]
            if item is None:  # pragma: no cover - defensive
                raise AssertionError("broken parent chain in cover reconstruction")
            chosen.append(item)
            cursor = item.start
        chosen.reverse()

        product_ids = product.segment_ids
        revisions = product.revision_map
        chunks: tuple[CoverChunk, ...] = tuple(
            CoverChunk(
                translation_id=item.entry.translation_id,
                source_ids=tuple(item.entry.source_refs[j].segment_id for j in range(
                    item.end - item.start
                )),
                source_revisions=tuple(
                    revisions[
                        item.entry.source_refs[j].segment_id
                    ]
                    for j in range(item.end - item.start)
                ),
                text=item.entry.text,
                start=item.start,
                end=item.end,
            )
            for item in chosen
        )
        # Defensive invariant checks: exact, ordered, non-empty.
        flat = [sid for chunk in chunks for sid in chunk.source_ids]
        if tuple(flat) != product_ids:
            raise AssertionError("cover did not reproduce the source order")
        if any(not chunk.text.strip() for chunk in chunks):
            raise AssertionError("refusing to build a package with empty text")
        rendered = "\n".join(chunk.text for chunk in chunks)
        if not rendered.strip():
            raise AssertionError("refusing to build an empty-text package")
        return CoverResult(
            ok=True,
            product=product,
            language=language,
            chunks=chunks,
            rendered_text=rendered,
            diagnosis=None,
        )

    return CoverResult(
        ok=False,
        product=product,
        language=language,
        chunks=(),
        rendered_text=None,
        diagnosis=_diagnose(product, language, assessments, usable),
    )
