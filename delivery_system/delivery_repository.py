"""Multilingual delivery repository: translations, covers and immutable packages.

The repository stores translation entries and packages in its *own* SQLite
database.  Source identity, order and revisions are read through a
:class:`~delivery_system.source_catalog.PublishedSourceReader`, which opens the
catalog database read-only.  The delivery side never writes source state and
never treats a position number as identity.

Publishing freezes the current source snapshot and the selected translation
entries.  Packages are immutable: reusing an id with different frozen content
is rejected, and old packages keep the source text and ids they were built
with even after the source is revised.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator, Optional, Protocol

from .coverage import assess_entries, solve_cover
from .errors import (
    DocumentNotFound,
    InvalidTranslation,
    NoCoverError,
    PackageExistsImmutable,
    TranslationExists,
)
from .models import (
    CoverResult,
    SourceProduct,
    SourceRef,
    TranslationAssessment,
    TranslationEntry,
)
from .source_catalog import PublishedSourceReader

PACKAGE_FORMAT = "translation-package/1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS translations (
    translation_id  TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    language        TEXT NOT NULL,
    text            TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translations_doc_lang
    ON translations (document_id, language);
CREATE TABLE IF NOT EXISTS packages (
    package_id       TEXT PRIMARY KEY,
    document_id      TEXT NOT NULL,
    language         TEXT NOT NULL,
    content_json     TEXT NOT NULL,
    content_sha256   TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_packages_doc_lang
    ON packages (document_id, language);
"""


class SourceProductReader(Protocol):
    def published_product(self, document_id: str) -> SourceProduct: ...

    def latest_release_id(self, document_id: str) -> Optional[int]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _refs_to_json(refs: Iterable[SourceRef]) -> str:
    return json.dumps(
        [{"segment_id": r.segment_id, "revision": r.revision} for r in refs],
        ensure_ascii=False,
        sort_keys=True,
    )


def _refs_from_json(raw: str) -> tuple[SourceRef, ...]:
    return tuple(
        SourceRef(segment_id=str(r["segment_id"]), revision=int(r["revision"]))
        for r in json.loads(raw)
    )


def _entry_from_row(row: sqlite3.Row) -> TranslationEntry:
    return TranslationEntry(
        translation_id=row["translation_id"],
        document_id=row["document_id"],
        language=row["language"],
        text=row["text"],
        source_refs=_refs_from_json(row["source_refs_json"]),
    )


class DeliveryRepository:
    def __init__(self, db_path: str, source: SourceProductReader) -> None:
        self.db_path = os.fspath(db_path)
        self.source = source
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------
    # translations
    # ------------------------------------------------------------------
    def add_translation(
        self,
        translation_id: str,
        document_id: str,
        language: str,
        text: str,
        source_refs: Iterable[dict[str, object] | SourceRef],
    ) -> TranslationEntry:
        if not translation_id:
            raise InvalidTranslation("translation_id must be non-empty")
        if not language:
            raise InvalidTranslation("language must be non-empty")
        refs: list[SourceRef] = []
        for ref in source_refs:
            if isinstance(ref, SourceRef):
                refs.append(ref)
            else:
                refs.append(
                    SourceRef(
                        segment_id=str(ref["segment_id"]),
                        revision=int(ref["revision"]),
                    )
                )
        if not refs:
            raise InvalidTranslation("source_refs must not be empty")

        entry = TranslationEntry(
            translation_id=translation_id,
            document_id=document_id,
            language=language,
            text=text,
            source_refs=tuple(refs),
        )
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT language, text, source_refs_json FROM translations"
                    " WHERE translation_id = ?",
                    (translation_id,),
                ).fetchone()
                if existing is not None:
                    same = (
                        existing["language"] == language
                        and existing["text"] == text
                        and existing["source_refs_json"] == _refs_to_json(refs)
                    )
                    if not same:
                        raise TranslationExists(
                            f"translation {translation_id!r} already exists with different content"
                        )
                    # Idempotent re-add of an identical entry.
                    return entry
                # Make sure the document exists on the source side so we never
                # accumulate translations for unknown documents.
                try:
                    self.source.published_product(document_id)
                except DocumentNotFound:
                    raise InvalidTranslation(
                        f"document {document_id!r} does not exist in the source catalog"
                    )
                conn.execute(
                    "INSERT INTO translations (translation_id, document_id, language, text,"
                    " source_refs_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        translation_id,
                        document_id,
                        language,
                        text,
                        _refs_to_json(refs),
                        _now(),
                    ),
                )
        finally:
            conn.close()
        return entry

    def get_translation(self, translation_id: str) -> TranslationEntry:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM translations WHERE translation_id = ?",
                (translation_id,),
            ).fetchone()
            if row is None:
                raise InvalidTranslation(f"translation {translation_id!r} not found")
            return _entry_from_row(row)
        finally:
            conn.close()

    def list_translations(
        self, document_id: Optional[str] = None, language: Optional[str] = None
    ) -> list[TranslationEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if language is not None:
            clauses.append("language = ?")
            params.append(language)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM translations" + where + " ORDER BY translation_id",
                params,
            ).fetchall()
            return [_entry_from_row(row) for row in rows]
        finally:
            conn.close()

    def languages(self, document_id: str) -> list[str]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT language FROM translations WHERE document_id = ?"
                " ORDER BY language",
                (document_id,),
            ).fetchall()
            return [row["language"] for row in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # assessment / cover
    # ------------------------------------------------------------------
    def assess(
        self, document_id: str, language: str
    ) -> list[TranslationAssessment]:
        product = self.source.published_product(document_id)
        entries = self.list_translations(document_id, language)
        return assess_entries(entries, product, language)

    def plan_cover(self, document_id: str, language: str) -> CoverResult:
        product = self.source.published_product(document_id)
        entries = self.list_translations(document_id, language)
        return solve_cover(product, language, entries)

    # ------------------------------------------------------------------
    # packages
    # ------------------------------------------------------------------
    def publish_language(
        self, document_id: str, language: str, package_id: str
    ) -> dict[str, object]:
        result = self.plan_cover(document_id, language)
        if not result.ok:
            raise NoCoverError(
                f"cannot publish {language!r} for {document_id!r}: "
                f"{result.diagnosis.detail}",
                result=result,
            )
        try:
            source_release = self.source.latest_release_id(document_id)
        except Exception:  # pragma: no cover - optional reader capability
            source_release = None
        return self._freeze_package(document_id, language, package_id, result, source_release)

    def _build_content(
        self,
        document_id: str,
        language: str,
        package_id: str,
        result: CoverResult,
        source_release: Optional[int],
    ) -> dict[str, object]:
        product = result.product
        source_snapshot = product.snapshot()
        chunks = [chunk.to_package_chunk() for chunk in result.chunks]
        identity = json.dumps(
            {
                "document_id": document_id,
                "language": language,
                "source_snapshot": source_snapshot,
                "chunks": chunks,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        content_sha = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        content: dict[str, object] = {
            "format": PACKAGE_FORMAT,
            "package_id": package_id,
            "document_id": document_id,
            "language": language,
            "source_revision": product.document_revision,
            "source_release": source_release,
            "source_snapshot": source_snapshot,
            "chunks": chunks,
            "rendered_text": result.rendered_text,
            "content_sha256": content_sha,
            "created_at": _now(),
        }
        return content

    def _freeze_package(
        self,
        document_id: str,
        language: str,
        package_id: str,
        result: CoverResult,
        source_release: Optional[int],
    ) -> dict[str, object]:
        content = self._build_content(
            document_id, language, package_id, result, source_release
        )
        sha = str(content["content_sha256"])
        conn = self._connect()
        try:
            with self._transaction(conn):
                existing = conn.execute(
                    "SELECT content_sha256, content_json FROM packages WHERE package_id = ?",
                    (package_id,),
                ).fetchone()
                if existing is not None:
                    if existing["content_sha256"] != sha:
                        raise PackageExistsImmutable(
                            f"package {package_id!r} already exists with different frozen content"
                        )
                    return json.loads(existing["content_json"])
                conn.execute(
                    "INSERT INTO packages (package_id, document_id, language, content_json,"
                    " content_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        package_id,
                        document_id,
                        language,
                        json.dumps(content, ensure_ascii=False, sort_keys=True),
                        sha,
                        str(content["created_at"]),
                    ),
                )
        finally:
            conn.close()
        return content

    def get_package(self, package_id: str) -> dict[str, object]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT content_json FROM packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
            if row is None:
                raise InvalidTranslation(f"package {package_id!r} not found")
            return json.loads(row["content_json"])
        finally:
            conn.close()

    def list_packages(
        self, document_id: Optional[str] = None, language: Optional[str] = None
    ) -> list[dict[str, object]]:
        clauses: list[str] = []
        params: list[object] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if language is not None:
            clauses.append("language = ?")
            params.append(language)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT package_id, document_id, language, content_sha256, created_at"
                " FROM packages" + where + " ORDER BY created_at, package_id",
                params,
            ).fetchall()
            return [
                {
                    "package_id": row["package_id"],
                    "document_id": row["document_id"],
                    "language": row["language"],
                    "content_sha256": row["content_sha256"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()


def open_repository(
    delivery_db: str, source_db: str
) -> tuple[DeliveryRepository, PublishedSourceReader]:
    reader = PublishedSourceReader(source_db)
    repo = DeliveryRepository(delivery_db, reader)
    return repo, reader
