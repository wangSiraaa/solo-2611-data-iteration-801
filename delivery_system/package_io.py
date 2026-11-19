"""Shared helpers for writing and validating translation-package/1 products."""

from __future__ import annotations

import hashlib
import json
from typing import Any

PACKAGE_FORMAT = "translation-package/1"


def identity_sha256(content: dict[str, Any]) -> str:
    identity = {
        "document_id": content["document_id"],
        "language": content["language"],
        "source_snapshot": content["source_snapshot"],
        "chunks": content["chunks"],
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def validate_package(content: dict[str, Any]) -> list[str]:
    """Structural + exact-cover validation of a frozen package.

    Returns a list of problems; empty means the package is a real, complete,
    non-overlapping, non-empty delivery for its recorded source snapshot.
    """

    problems: list[str] = []
    if content.get("format") != PACKAGE_FORMAT:
        problems.append(f"unsupported format {content.get('format')!r}")
    for key in ("package_id", "document_id", "language"):
        if not isinstance(content.get(key), str) or not content.get(key):
            problems.append(f"missing or empty {key}")

    snapshot = content.get("source_snapshot")
    chunks = content.get("chunks")
    if not isinstance(snapshot, list) or not snapshot:
        problems.append("source_snapshot missing or empty")
        snapshot = []
    if not isinstance(chunks, list) or not chunks:
        problems.append("chunks missing or empty (empty-text packages are invalid)")
        chunks = []

    snapshot_ids: list[str] = []
    for item in snapshot:
        if not isinstance(item, dict) or "segment_id" not in item or "revision" not in item:
            problems.append("malformed source_snapshot entry")
            continue
        snapshot_ids.append(str(item["segment_id"]))

    covered: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            problems.append("malformed chunk")
            continue
        tid = chunk.get("translation_id")
        ids = chunk.get("source_ids")
        text = chunk.get("text")
        if not isinstance(ids, list) or not ids:
            problems.append(f"chunk {tid!r} has no source_ids")
            continue
        if not isinstance(text, str) or not text.strip():
            problems.append(f"chunk {tid!r} has empty text")
        covered.extend(str(x) for x in ids)

    if covered != snapshot_ids:
        problems.append(
            "chunks do not reproduce the source snapshot exactly once in order "
            f"(covered={covered!r}, snapshot={snapshot_ids!r})"
        )
    rendered = content.get("rendered_text")
    if not isinstance(rendered, str) or not rendered.strip():
        problems.append("rendered_text missing or empty")
    stored_sha = content.get("content_sha256")
    if isinstance(stored_sha, str) and stored_sha != identity_sha256(content):
        problems.append("content_sha256 does not match frozen content")
    return problems


def write_package(content: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(content, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def read_package(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
