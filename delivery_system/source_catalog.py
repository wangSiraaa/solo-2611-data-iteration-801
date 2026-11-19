"""Independent source-segment catalog.

Identity is ``segment_id``; ``position`` is only the current ordinal.  The
catalog publishes immutable :class:`SourceProduct` snapshots and records every
mutation as a numbered release so historical source state stays queryable.

This side knows nothing about translations, covers or packages.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Sequence

from .errors import (
    DocumentExists,
    DocumentNotFound,
    DuplicateSegment,
    InvalidPosition,
    RevisionConflict,
    SegmentNotFound,
)
from .models import SourceProduct, SourceSegment

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id      TEXT PRIMARY KEY,
    revision         INTEGER NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    document_id      TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    segment_id       TEXT NOT NULL,
    position         INTEGER NOT NULL,
    revision         INTEGER NOT NULL,
    text             TEXT NOT NULL,
    PRIMARY KEY (document_id, segment_id),
    UNIQUE (document_id, position)
);
CREATE TABLE IF NOT EXISTS source_releases (
    release_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id      TEXT NOT NULL,
    document_revision INTEGER NOT NULL,
    snapshot_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_segments_doc_position
    ON segments (document_id, position);
CREATE INDEX IF NOT EXISTS idx_releases_doc
    ON source_releases (document_id, release_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(db_path: str, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        uri = f"file:{os.fspath(db_path)}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(os.fspath(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class SourceCatalog:
    """Read/write source catalog backed by one SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = os.fspath(db_path)
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = _connect(self.db_path)
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # creation / structural mutations
    # ------------------------------------------------------------------
    def create_document(
        self,
        document_id: str,
        segments: Sequence[dict[str, object]],
        *,
        document_revision: int = 1,
    ) -> SourceProduct:
        if not document_id:
            raise DocumentNotFound("document_id must be non-empty")
        if document_revision < 1:
            raise RevisionConflict("document_revision must be >= 1")
        seen: set[str] = set()
        rows: list[tuple[str, str, int, int, str]] = []
        for position, seg in enumerate(segments):
            sid = str(seg["segment_id"])
            if sid in seen:
                raise DuplicateSegment(f"duplicate segment_id {sid!r}")
            seen.add(sid)
            revision = int(seg.get("revision", 1))
            if revision < 1:
                raise RevisionConflict(f"segment {sid!r} revision must be >= 1")
            rows.append((document_id, sid, position, revision, str(seg["text"])))

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                existing = conn.execute(
                    "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
                ).fetchone()
                if existing:
                    raise DocumentExists(f"document {document_id!r} already exists")
                conn.execute(
                    "INSERT INTO documents (document_id, revision, created_at) VALUES (?, ?, ?)",
                    (document_id, document_revision, _now()),
                )
                conn.executemany(
                    "INSERT INTO segments (document_id, segment_id, position, revision, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                snapshot = [
                    {"segment_id": r[1], "position": r[2], "revision": r[3], "text": r[4]}
                    for r in rows
                ]
                self._record_release(conn, document_id, document_revision, snapshot)
        finally:
            conn.close()
        return self.published_product(document_id)

    def add_segment(
        self,
        document_id: str,
        segment_id: str,
        text: str,
        *,
        position: Optional[int] = None,
        revision: int = 1,
    ) -> SourceProduct:
        if not segment_id:
            raise SegmentNotFound("segment_id must be non-empty")
        if revision < 1:
            raise RevisionConflict("revision must be >= 1")
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                exists = conn.execute(
                    "SELECT 1 FROM segments WHERE document_id = ? AND segment_id = ?",
                    (document_id, segment_id),
                ).fetchone()
                if exists:
                    raise DuplicateSegment(f"segment {segment_id!r} already exists")
                ids = self._ordered_ids(conn, document_id)
                if position is None:
                    position = len(ids)
                if position < 0 or position > len(ids):
                    raise InvalidPosition(
                        f"position {position} outside 0..{len(ids)}"
                    )
                conn.execute(
                    "INSERT INTO segments (document_id, segment_id, position, revision, text)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (document_id, segment_id, -(len(ids) + 1), revision, text),
                )
                ids.insert(position, segment_id)
                self._set_order(conn, document_id, ids)
                self._bump_and_release(conn, document_id)
        finally:
            conn.close()
        return self.published_product(document_id)

    def revise_segment(
        self,
        document_id: str,
        segment_id: str,
        text: str,
        *,
        revision: Optional[int] = None,
    ) -> SourceProduct:
        """Publish new text and a new per-segment revision.

        The old revision is never re-labelled: translations that referenced it
        become stale immediately.
        """

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                row = conn.execute(
                    "SELECT revision FROM segments WHERE document_id = ? AND segment_id = ?",
                    (document_id, segment_id),
                ).fetchone()
                if row is None:
                    raise SegmentNotFound(
                        f"segment {segment_id!r} not found in {document_id!r}"
                    )
                current = int(row["revision"])
                new_revision = current + 1 if revision is None else int(revision)
                if new_revision <= current:
                    raise RevisionConflict(
                        f"new revision {new_revision} must be greater than current {current}"
                    )
                conn.execute(
                    "UPDATE segments SET text = ?, revision = ?"
                    " WHERE document_id = ? AND segment_id = ?",
                    (text, new_revision, document_id, segment_id),
                )
                self._bump_and_release(conn, document_id)
        finally:
            conn.close()
        return self.published_product(document_id)

    def move_segment(
        self, document_id: str, segment_id: str, new_position: int
    ) -> SourceProduct:
        """Reorder by stable identity; identities and revisions are untouched."""

        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                ids = self._ordered_ids(conn, document_id)
                if segment_id not in ids:
                    raise SegmentNotFound(
                        f"segment {segment_id!r} not found in {document_id!r}"
                    )
                if new_position < 0 or new_position >= len(ids):
                    raise InvalidPosition(
                        f"position {new_position} outside 0..{len(ids) - 1}"
                    )
                ids.remove(segment_id)
                ids.insert(new_position, segment_id)
                self._set_order(conn, document_id, ids)
                self._bump_and_release(conn, document_id)
        finally:
            conn.close()
        return self.published_product(document_id)

    def remove_segment(self, document_id: str, segment_id: str) -> SourceProduct:
        conn = _connect(self.db_path)
        try:
            with _transaction(conn):
                self._require_document(conn, document_id)
                cur = conn.execute(
                    "DELETE FROM segments WHERE document_id = ? AND segment_id = ?",
                    (document_id, segment_id),
                )
                if cur.rowcount == 0:
                    raise SegmentNotFound(
                        f"segment {segment_id!r} not found in {document_id!r}"
                    )
                ids = self._ordered_ids(conn, document_id)
                self._set_order(conn, document_id, ids)
                self._bump_and_release(conn, document_id)
        finally:
            conn.close()
        return self.published_product(document_id)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def published_product(self, document_id: str) -> SourceProduct:
        conn = _connect(self.db_path, readonly=True)
        try:
            return self._read_product(conn, document_id)
        finally:
            conn.close()

    # alias used by the delivery side's source-reader protocol
    def get_product(self, document_id: str) -> SourceProduct:
        return self.published_product(document_id)

    def latest_release_id(self, document_id: str) -> Optional[int]:
        conn = _connect(self.db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT MAX(release_id) AS r FROM source_releases WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return None if row is None or row["r"] is None else int(row["r"])
        finally:
            conn.close()

    def product_at_release(self, release_id: int) -> SourceProduct:
        conn = _connect(self.db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT document_id, document_revision, snapshot_json"
                " FROM source_releases WHERE release_id = ?",
                (release_id,),
            ).fetchone()
            if row is None:
                raise DocumentNotFound(f"release {release_id} not found")
            data = json.loads(row["snapshot_json"])
            segments = tuple(
                SourceSegment(
                    segment_id=s["segment_id"],
                    position=int(s["position"]),
                    revision=int(s["revision"]),
                    text=s["text"],
                )
                for s in data
            )
            return SourceProduct(
                document_id=row["document_id"],
                document_revision=int(row["document_revision"]),
                segments=segments,
            )
        finally:
            conn.close()

    def get_segment(self, document_id: str, segment_id: str) -> SourceSegment:
        product = self.published_product(document_id)
        for seg in product.segments:
            if seg.segment_id == segment_id:
                return seg
        raise SegmentNotFound(f"segment {segment_id!r} not found in {document_id!r}")

    def list_documents(self) -> list[dict[str, object]]:
        conn = _connect(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT document_id, revision FROM documents ORDER BY document_id"
            ).fetchall()
            counts = dict(
                conn.execute(
                    "SELECT document_id, COUNT(*) FROM segments GROUP BY document_id"
                ).fetchall()
            )
            return [
                {
                    "document_id": r["document_id"],
                    "revision": int(r["revision"]),
                    "segments": int(counts.get(r["document_id"], 0)),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_releases(self, document_id: str) -> list[dict[str, object]]:
        conn = _connect(self.db_path, readonly=True)
        try:
            rows = conn.execute(
                "SELECT release_id, document_revision, created_at"
                " FROM source_releases WHERE document_id = ? ORDER BY release_id",
                (document_id,),
            ).fetchall()
            return [
                {
                    "release_id": int(r["release_id"]),
                    "document_revision": int(r["document_revision"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    @staticmethod
    def _read_product(conn: sqlite3.Connection, document_id: str) -> SourceProduct:
        doc = conn.execute(
            "SELECT revision FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if doc is None:
            raise DocumentNotFound(f"document {document_id!r} not found")
        rows = conn.execute(
            "SELECT segment_id, position, revision, text FROM segments"
            " WHERE document_id = ? ORDER BY position",
            (document_id,),
        ).fetchall()
        segments = tuple(
            SourceSegment(
                segment_id=r["segment_id"],
                position=int(r["position"]),
                revision=int(r["revision"]),
                text=r["text"],
            )
            for r in rows
        )
        return SourceProduct(
            document_id=document_id,
            document_revision=int(doc["revision"]),
            segments=segments,
        )

    @staticmethod
    def _require_document(conn: sqlite3.Connection, document_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise DocumentNotFound(f"document {document_id!r} not found")

    @staticmethod
    def _ordered_ids(conn: sqlite3.Connection, document_id: str) -> list[str]:
        return [
            r["segment_id"]
            for r in conn.execute(
                "SELECT segment_id FROM segments WHERE document_id = ? ORDER BY position",
                (document_id,),
            ).fetchall()
        ]

    @staticmethod
    def _set_order(
        conn: sqlite3.Connection, document_id: str, ordered_ids: Iterable[str]
    ) -> None:
        ids = list(ordered_ids)
        # Move through unique negative positions, then assign final positions.
        for index, sid in enumerate(ids):
            conn.execute(
                "UPDATE segments SET position = ? WHERE document_id = ? AND segment_id = ?",
                (-(index + 1), document_id, sid),
            )
        for index, sid in enumerate(ids):
            conn.execute(
                "UPDATE segments SET position = ? WHERE document_id = ? AND segment_id = ?",
                (index, document_id, sid),
            )

    def _bump_and_release(self, conn: sqlite3.Connection, document_id: str) -> None:
        conn.execute(
            "UPDATE documents SET revision = revision + 1 WHERE document_id = ?",
            (document_id,),
        )
        revision = int(
            conn.execute(
                "SELECT revision FROM documents WHERE document_id = ?", (document_id,)
            ).fetchone()["revision"]
        )
        rows = conn.execute(
            "SELECT segment_id, position, revision, text FROM segments"
            " WHERE document_id = ? ORDER BY position",
            (document_id,),
        ).fetchall()
        snapshot = [
            {
                "segment_id": r["segment_id"],
                "position": int(r["position"]),
                "revision": int(r["revision"]),
                "text": r["text"],
            }
            for r in rows
        ]
        self._record_release(conn, document_id, revision, snapshot)

    @staticmethod
    def _record_release(
        conn: sqlite3.Connection,
        document_id: str,
        document_revision: int,
        snapshot: Sequence[dict[str, object]],
    ) -> None:
        conn.execute(
            "INSERT INTO source_releases (document_id, document_revision, snapshot_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (
                document_id,
                document_revision,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                _now(),
            ),
        )


class PublishedSourceReader:
    """Read-only handle used by the delivery side.

    It opens its own SQLite connection in ``mode=ro`` and re-reads the
    catalog's published product on every call; it never caches source state.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = os.fspath(db_path)
        if not os.path.exists(self.db_path):
            raise DocumentNotFound(f"source database {self.db_path!r} does not exist")

    def published_product(self, document_id: str) -> SourceProduct:
        conn = _connect(self.db_path, readonly=True)
        try:
            return SourceCatalog._read_product(conn, document_id)
        finally:
            conn.close()

    def latest_release_id(self, document_id: str) -> Optional[int]:
        conn = _connect(self.db_path, readonly=True)
        try:
            row = conn.execute(
                "SELECT MAX(release_id) AS r FROM source_releases WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            return None if row is None or row["r"] is None else int(row["r"])
        finally:
            conn.close()
