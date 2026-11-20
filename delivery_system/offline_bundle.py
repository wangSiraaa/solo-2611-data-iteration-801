"""Offline version exported from a frozen ``translation-package/1``.

The exporter reads the **actual frozen chunks** and produces a self-contained,
file-based bundle that a reader can install without any publisher database:

* ``manifest.json`` -- object identities, dependencies, byte sizes and source
  digests.  This is control-channel metadata; it is *not* counted against the
  device's download or space budget.
* one object file per dictionary (``dict-<object_id>.json``) and per chunk
  representation (``chunk-<object_id>.json``).  Their ``body`` payloads are the
  counted objects.

Each chunk gets ``1 + len(dictionaries)`` representations: the pure-literal
shortest body, plus the shortest body for *each* accepted dictionary.  The
device chooses one representation per block.

The bundle is plain files; nothing here requires a live process's in-memory
objects to decode.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Sequence

from .offline_codec import (
    MAX_DICTIONARY_ITEMS,
    PhraseDictionary,
    decode_body,
    parse_dictionary_body,
    shortest_body,
    sha256_hex,
)

MANIFEST_FORMAT = "offline-bundle/1"
OBJECT_FORMAT = "offline-object/1"
MAX_DICTIONARIES = 4
MAX_BLOCKS = 12


class BundleError(Exception):
    """A bundle / manifest / object is malformed or internally inconsistent."""

    def __init__(self, message: str, reason: str = "invalid_bundle") -> None:
        super().__init__(message)
        self.reason = reason


# ----------------------------------------------------------------------
# export
# ----------------------------------------------------------------------
def _dict_document(dictionary: PhraseDictionary) -> dict[str, object]:
    body = dictionary.body
    return {
        "format": OBJECT_FORMAT,
        "kind": "dictionary",
        "dictionary_id": dictionary.dictionary_id,
        "object_id": dictionary.object_id,
        "dependency": None,
        "size": len(body),
        "content_sha256": sha256_hex(body),
        "source_sha256": sha256_hex(body),
        "body_hex": body.hex(),
    }


def _chunk_document(
    object_id: str,
    body: bytes,
    text_sha256: str,
    dictionary_id: Optional[str],
    dependency: Optional[str],
) -> dict[str, object]:
    return {
        "format": OBJECT_FORMAT,
        "kind": "chunk",
        "object_id": object_id,
        "dictionary_id": dictionary_id,
        "dependency": dependency,
        "size": len(body),
        "content_sha256": sha256_hex(body),
        # The text this body must decode to; verified on the device.
        "source_sha256": text_sha256,
        "body_hex": body.hex(),
    }


def _atomic_write_json(path: str, document: dict[str, object]) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def build_bundle(
    package: dict[str, object],
    dictionaries: Sequence[PhraseDictionary],
    out_dir: str,
    *,
    bundle_id: Optional[str] = None,
) -> dict[str, object]:
    """Export ``package`` into ``out_dir``; return the manifest."""

    if len(dictionaries) > MAX_DICTIONARIES:
        raise BundleError(
            f"at most {MAX_DICTIONARIES} dictionaries are accepted, got {len(dictionaries)}"
        )
    for dictionary in dictionaries:
        if not (1 <= len(dictionary.items) <= MAX_DICTIONARY_ITEMS):
            raise BundleError("dictionary item count out of range")

    chunks = package.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise BundleError("package has no chunks", reason="empty_package")
    if len(chunks) > MAX_BLOCKS:
        raise BundleError(
            f"at most {MAX_BLOCKS} target blocks are supported, got {len(chunks)}"
        )

    os.makedirs(out_dir, exist_ok=True)

    # Dictionaries: unique by object identity.  Two labels for the same content
    # denote the same object and are stored once.
    dict_entries: dict[str, dict[str, object]] = {}
    dict_by_oid: dict[str, PhraseDictionary] = {}
    for dictionary in dictionaries:
        oid = dictionary.object_id
        if oid in dict_entries:
            aliases = dict_entries[oid].setdefault("aliases", [])
            assert isinstance(aliases, list)
            if dictionary.dictionary_id not in aliases:
                aliases.append(dictionary.dictionary_id)
            continue
        dict_entries[oid] = dictionary.to_manifest_entry()
        dict_by_oid[oid] = dictionary
        _atomic_write_json(
            os.path.join(out_dir, f"dict-{oid}.json"), _dict_document(dictionary)
        )

    blocks: list[dict[str, object]] = []
    chunk_docs: dict[str, dict[str, object]] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            raise BundleError("malformed chunk")
        block_id = str(chunk["translation_id"])
        text = chunk["text"]
        if not isinstance(text, str):
            raise BundleError(f"chunk {block_id!r} text is not a string")
        text_bytes = text.encode("utf-8")
        text_sha = sha256_hex(text_bytes)

        representations: list[dict[str, object]] = []
        literal_body = shortest_body(text_bytes, None)
        literal_oid = _chunk_object_id(None, literal_body)
        representations.append(
            {
                "object_id": literal_oid,
                "kind": "literal",
                "dictionary_id": None,
                "dictionary_object_id": None,
                "dependencies": [],
                "size": len(literal_body),
                "source_sha256": text_sha,
            }
        )
        chunk_docs[literal_oid] = _chunk_document(
            literal_oid, literal_body, text_sha, None, None
        )
        for dictionary in dictionaries:
            body = shortest_body(text_bytes, dictionary.items)
            oid = _chunk_object_id(dictionary.object_id, body)
            representations.append(
                {
                    "object_id": oid,
                    "kind": "dictionary",
                    "dictionary_id": dictionary.dictionary_id,
                    "dictionary_object_id": dictionary.object_id,
                    "dependencies": [dictionary.object_id],
                    "size": len(body),
                    "source_sha256": text_sha,
                }
            )
            chunk_docs[oid] = _chunk_document(
                oid,
                body,
                text_sha,
                dictionary.dictionary_id,
                dictionary.object_id,
            )

        blocks.append(
            {
                "block_id": block_id,
                "source_ids": list(chunk.get("source_ids", [])),
                "source_revisions": list(chunk.get("source_revisions", [])),
                "text_sha256": text_sha,
                "text_size": len(text_bytes),
                "representations": representations,
            }
        )

    for oid, document in chunk_docs.items():
        _atomic_write_json(os.path.join(out_dir, f"chunk-{oid}.json"), document)

    if bundle_id is None:
        seed = json.dumps(
            {
                "package_id": package.get("package_id"),
                "package_sha256": package.get("content_sha256"),
                "dictionaries": sorted(dict_entries),
                "blocks": [b["block_id"] for b in blocks],
            },
            sort_keys=True,
        )
        bundle_id = sha256_hex(seed.encode("utf-8"))[:16]

    manifest: dict[str, object] = {
        "format": MANIFEST_FORMAT,
        "bundle_id": bundle_id,
        "book_id": str(package.get("package_id", "")),
        "document_id": str(package.get("document_id", "")),
        "language": str(package.get("language", "")),
        "source_snapshot": list(package.get("source_snapshot", [])),
        "dictionaries": [dict_entries[oid] for oid in dict_entries],
        "blocks": blocks,
        "package_sha256": package.get("content_sha256"),
    }
    _atomic_write_json(os.path.join(out_dir, "manifest.json"), manifest)
    return manifest


def _chunk_object_id(dependency: Optional[str], body: bytes) -> str:
    from .offline_codec import chunk_object_id

    return chunk_object_id(dependency, body)


# ----------------------------------------------------------------------
# loading / verification (device side, no publisher database)
# ----------------------------------------------------------------------
def read_manifest(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as fh:
        manifest = json.load(fh)
    validate_manifest(manifest)
    return manifest


def validate_manifest(manifest: dict[str, object]) -> None:
    if not isinstance(manifest, dict) or manifest.get("format") != MANIFEST_FORMAT:
        raise BundleError("not an offline/1 manifest")
    blocks = manifest.get("blocks")
    dictionaries = manifest.get("dictionaries")
    if not isinstance(blocks, list) or not blocks:
        raise BundleError("manifest has no blocks")
    if len(blocks) > MAX_BLOCKS:
        raise BundleError("manifest has too many blocks")
    if not isinstance(dictionaries, list) or len(dictionaries) > MAX_DICTIONARIES:
        raise BundleError("manifest has too many dictionaries")
    dict_oids = set()
    for entry in dictionaries:
        oid = str(entry.get("object_id"))
        if oid in dict_oids:
            raise BundleError(f"duplicate dictionary object {oid!r}")
        dict_oids.add(oid)
    block_ids = set()
    for block in blocks:
        bid = str(block.get("block_id"))
        if bid in block_ids:
            raise BundleError(f"duplicate block id {bid!r}")
        block_ids.add(bid)
        reps = block.get("representations")
        if not isinstance(reps, list) or not reps:
            raise BundleError(f"block {bid!r} has no representations")
        for rep in reps:
            if rep.get("kind") == "dictionary":
                dep = rep.get("dictionary_object_id")
                if dep not in dict_oids:
                    raise BundleError(
                        f"block {bid!r} references unknown dictionary object {dep!r}"
                    )


def _read_object_document(path: str) -> dict[str, object]:
    with open(path, encoding="utf-8") as fh:
        document = json.load(fh)
    if not isinstance(document, dict) or document.get("format") != OBJECT_FORMAT:
        raise BundleError(f"{path}: not an offline object document")
    body_hex = document.get("body_hex")
    if not isinstance(body_hex, str):
        raise BundleError(f"{path}: missing body_hex")
    try:
        body = bytes.fromhex(body_hex)
    except ValueError as exc:
        raise BundleError(f"{path}: body_hex is not valid hex") from exc
    if document.get("size") != len(body):
        raise BundleError(f"{path}: declared size does not match body")
    if document.get("content_sha256") != sha256_hex(body):
        raise BundleError(f"{path}: content_sha256 does not match body")
    document["_body"] = body
    return document


def load_dictionary_object(path: str) -> tuple[str, tuple[bytes, ...]]:
    """Verify a dictionary object file and return ``(object_id, items)``."""

    document = _read_object_document(path)
    if document.get("kind") != "dictionary":
        raise BundleError(f"{path}: not a dictionary object")
    body = document["_body"]
    assert isinstance(body, bytes)
    items = parse_dictionary_body(body)
    from .offline_codec import dictionary_object_id

    oid = dictionary_object_id(body)
    if document.get("object_id") != oid:
        raise BundleError(f"{path}: dictionary object identity mismatch")
    return oid, items


def load_chunk_object(
    path: str, expected_oid: str, expected_dependency: Optional[str]
) -> bytes:
    """Verify a chunk object file against its expected identity; return body."""

    document = _read_object_document(path)
    if document.get("kind") != "chunk":
        raise BundleError(f"{path}: not a chunk object")
    if document.get("object_id") != expected_oid:
        raise BundleError(f"{path}: chunk object identity mismatch")
    if document.get("dependency") != expected_dependency:
        raise BundleError(f"{path}: chunk dependency mismatch")
    body = document["_body"]
    assert isinstance(body, bytes)
    from .offline_codec import chunk_object_id

    if chunk_object_id(expected_dependency, body) != expected_oid:
        raise BundleError(f"{path}: chunk object identity does not match its body")
    return body


def iter_required_object_ids(
    manifest: dict[str, object],
    choices: dict[str, dict[str, object]],
) -> list[str]:
    """Dictionary objects first, then chunk objects (dependency order)."""

    oids: list[str] = []
    seen: set[str] = set()
    for block_id in choices:
        rep = choices[block_id]
        dep = rep.get("dictionary_object_id")
        if dep is not None and dep not in seen:
            seen.add(dep)
            oids.append(str(dep))
    for block_id in choices:
        rep = choices[block_id]
        oid = str(rep["object_id"])
        if oid not in seen:
            seen.add(oid)
            oids.append(oid)
    return oids


def materialize(
    manifest: dict[str, object],
    choices: dict[str, dict[str, object]],
    load_dict_items,
    load_chunk_body,
) -> dict[str, object]:
    """Decode every chosen representation; verify exact text bytes.

    ``load_dict_items(object_id) -> items`` and
    ``load_chunk_body(object_id) -> bytes`` are supplied by the caller so this
    works for a bundle directory or an installed device store.
    """

    texts: list[str] = []
    for block in manifest["blocks"]:
        block_id = str(block["block_id"])
        rep = choices.get(block_id)
        if rep is None:
            raise BundleError(f"no representation chosen for block {block_id!r}")
        body = load_chunk_body(str(rep["object_id"]))
        dep = rep.get("dictionary_object_id")
        items = load_dict_items(str(dep)) if dep is not None else ()
        decoded = decode_body(body, items)
        if sha256_hex(decoded) != block["text_sha256"]:
            raise BundleError(
                f"block {block_id!r} decoded bytes do not match source digest",
                reason="bad_block",
            )
        texts.append(decoded.decode("utf-8"))
    return {
        "book_id": manifest.get("book_id"),
        "document_id": manifest.get("document_id"),
        "language": manifest.get("language"),
        "blocks": texts,
        "rendered_text": "\n".join(texts),
    }
