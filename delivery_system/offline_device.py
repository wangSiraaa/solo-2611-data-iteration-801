"""Reader device: its own object cache, download log and active-book switch.

The device process holds **only** device cache and the download manifest.  It
has no publisher database and never decodes another process's in-memory
objects: objects are verified from bytes, written to a file cache, and the
active book is published by atomically replacing a small ``active.json``.

Crash model
-----------
* Objects are written to a temporary file, fsynced and ``os.replace``-d into
  the cache.  An object is only *verified and durable* once its file exists and
  its digest matches; a partially written temp file is never a cache object.
* ``active.json`` is replaced atomically, only after every object the new book
  needs is verified.  Until then the previous ``active.json`` (old book) keeps
  being readable.
* ``_write_active(..., simulate_crash=True)`` simulates power loss after the
  objects were written but before the switch: the old book must still read.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Optional

from .offline_bundle import (
    BundleError,
    iter_required_object_ids,
    load_chunk_object,
    materialize,
    read_manifest,
)
from .offline_codec import CodecError, DictionaryError, sha256_hex
from .offline_planner import Plan, PlanError, plan_install

DEFAULT_TRAFFIC_LIMIT = 1_000_000


class DeviceError(Exception):
    def __init__(self, message: str, reason: str = "device_error") -> None:
        super().__init__(message)
        self.reason = reason


def _atomic_write_json(path: str, document: object) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(document, fh, ensure_ascii=False, sort_keys=True, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ReaderDevice:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.objects_dir = os.path.join(self.root, "objects")
        self.manifest_path = os.path.join(self.root, "manifest.json")
        self.active_path = os.path.join(self.root, "active.json")
        self.log_path = os.path.join(self.root, "download_log.json")
        os.makedirs(self.objects_dir, exist_ok=True)
        if not os.path.exists(self.active_path):
            _atomic_write_json(self.active_path, None)

    # ------------------------------------------------------------------
    # cache inspection
    # ------------------------------------------------------------------
    def _object_path(self, object_id: str) -> str:
        return os.path.join(self.objects_dir, f"{object_id}.obj")

    def _load_verified(self, object_id: str) -> Optional[dict[str, object]]:
        path = self._object_path(object_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                document = json.load(fh)
            body = bytes.fromhex(document["body_hex"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if sha256_hex(body) != document.get("content_sha256"):
            return None
        if document.get("object_id") != object_id:
            return None
        return {"verified": True, "size": document.get("size"), "document": document}

    def cached_index(self, object_ids) -> dict[str, dict[str, object]]:
        index: dict[str, dict[str, object]] = {}
        for oid in object_ids:
            entry = self._load_verified(oid)
            if entry is not None:
                index[oid] = entry
        return index

    def store_object(self, document: dict[str, object]) -> None:
        """Persist one already-verified object document durably."""

        oid = str(document["object_id"])
        body = bytes.fromhex(document["body_hex"])
        if sha256_hex(body) != document.get("content_sha256"):
            raise DeviceError(
                f"refusing to store object {oid!r} with a bad digest", "bad_block"
            )
        path = self._object_path(oid)
        if self._load_verified(oid) is not None:
            return
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(document, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def object_documents(self, bundle_dir: str, object_ids) -> dict[str, dict[str, object]]:
        """Reader side of object transfer (control channel, not counted)."""

        documents: dict[str, dict[str, object]] = {}
        for oid in object_ids:
            for prefix in ("dict", "chunk"):
                candidate = os.path.join(bundle_dir, f"{prefix}-{oid}.json")
                if os.path.exists(candidate):
                    with open(candidate, encoding="utf-8") as fh:
                        documents[oid] = json.load(fh)
                    break
            else:
                raise DeviceError(f"bundle lacks object {oid!r}", "missing_object")
        return documents

    # ------------------------------------------------------------------
    # active book
    # ------------------------------------------------------------------
    def read_active(self) -> Optional[dict[str, object]]:
        with open(self.active_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _protected_objects(self) -> set[str]:
        active = self.read_active()
        if not active:
            return set()
        protected = set(active.get("objects", []))
        for rep in active.get("choices", {}).values():
            dep = rep.get("dictionary_object_id")
            if dep:
                protected.add(str(dep))
        return protected

    def _protected_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for oid in self._protected_objects():
            entry = self._load_verified(oid)
            if entry is not None and entry.get("size") is not None:
                sizes[oid] = int(entry["size"])
        return sizes

    def _read_bytes(self) -> int:
        """Total object-body bytes in the cache (the budget unit)."""

        total = 0
        if os.path.isdir(self.objects_dir):
            for name in os.listdir(self.objects_dir):
                if not name.endswith(".obj"):
                    continue
                oid = name[:-4]
                entry = self._load_verified(oid)
                if entry is not None and entry.get("size") is not None:
                    total += int(entry["size"])
        return total

    def space_report(self) -> dict[str, object]:
        active = self.read_active()
        protected_sizes = self._protected_sizes()
        return {
            "cache_bytes": self._read_bytes(),
            "protected_bytes": sum(protected_sizes.values()),
            "active_book": None if not active else active.get("book_id"),
            "active_bundle": None if not active else active.get("bundle_id"),
        }

    # ------------------------------------------------------------------
    # planning helpers
    # ------------------------------------------------------------------
    def plan(
        self,
        manifest: dict[str, object],
        *,
        total_capacity: int,
        traffic_limit: Optional[int] = None,
        max_bytes_for_planning: Optional[int] = None,
    ) -> Plan:
        cached = self.cached_index(_all_object_ids(manifest))
        return plan_install(
            manifest,
            cached=cached,
            total_capacity=total_capacity,
            traffic_limit=traffic_limit,
            protected=self._protected_sizes(),
            max_bytes_for_planning=max_bytes_for_planning,
        )

    def seed_object_from_bundle(self, bundle_dir: str, object_id: str) -> None:
        """Persist one object from a bundle as if it had been installed before.

        Used to model a device that already holds a dictionary from an earlier
        download.  The object is still verified from bytes on the way in.
        """

        documents = self.object_documents(bundle_dir, [object_id])
        self.store_object(documents[object_id])

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------
    def install(
        self,
        manifest: dict[str, object],
        bundle_dir: str,
        *,
        total_capacity: int,
        traffic_limit: Optional[int] = None,
        simulate_crash: bool = False,
        manifest_source: Optional[str] = None,
    ) -> dict[str, object]:
        """Fetch, verify and atomically activate the new book.

        Returns a report.  ``simulate_crash=True`` stops after objects are
        written but before ``active.json`` is switched -- old book still reads.
        """

        try:
            plan = self.plan(
                manifest, total_capacity=total_capacity, traffic_limit=traffic_limit
            )
        except PlanError as exc:
            raise DeviceError(str(exc), exc.reason) from exc
        if traffic_limit is not None and plan.download_bytes > traffic_limit:
            raise DeviceError(
                f"download needs {plan.download_bytes} bytes > limit {traffic_limit}",
                "traffic_limit_exceeded",
            )

        # Fetch + verify every object first.  Nothing is activated yet; a block
        # that fails verification aborts the whole install before the switch.
        try:
            pairs = self.object_documents(bundle_dir, plan.download_objects)
        except BundleError as exc:
            raise DeviceError(str(exc), "bad_block") from exc
        for oid in plan.download_objects:
            document = pairs[oid]
            kind = document.get("kind")
            try:
                if kind == "dictionary":
                    # Recompute identity from body; never trust the label.
                    from .offline_codec import (
                        dictionary_object_id,
                        parse_dictionary_body,
                    )

                    body = bytes.fromhex(document["body_hex"])
                    if dictionary_object_id(body) != oid:
                        raise DeviceError(
                            f"dictionary {oid!r} identity mismatch", "bad_block"
                        )
                    parse_dictionary_body(body)
                else:
                    dep = document.get("dependency")
                    load_chunk_object(
                        os.path.join(bundle_dir, f"chunk-{oid}.json"), oid, dep
                    )
                self.store_object(document)
            except DeviceError:
                raise
            except (BundleError, CodecError, DictionaryError, ValueError, KeyError) as exc:
                raise DeviceError(f"block verification failed: {exc}", "bad_block") from exc

        # Keep the switch manifest next to the device so reads need no bundle.
        if manifest_source is not None:
            stored_manifest = read_manifest(manifest_source)
        else:
            stored_manifest = manifest

        # Verify every chosen representation decodes to the exact source bytes.
        render = self._render(stored_manifest, plan.choices)

        needed = iter_required_object_ids(stored_manifest, plan.choices)

        # Unprotected cache may be dropped to make room; protected objects and
        # the objects this plan needs are kept.
        keep = set(needed) | self._protected_objects()
        self._prune_unprotected(keep)

        active = {
            "bundle_id": stored_manifest.get("bundle_id"),
            "book_id": stored_manifest.get("book_id"),
            "document_id": stored_manifest.get("document_id"),
            "language": stored_manifest.get("language"),
            "choices": plan.to_dict()["choices"],
            "objects": needed,
            "manifest": stored_manifest,
            "rendered_text": render["rendered_text"],
        }

        if simulate_crash:
            # Objects are durable; active.json is still the old one.
            report = {
                "installed": False,
                "simulated_crash": True,
                "download_bytes": plan.download_bytes,
                "download_objects": plan.download_objects,
                "reusable_objects": plan.reusable_objects,
                "total_bytes": plan.total_bytes,
                "used_dictionaries": plan.used_dictionaries,
                "rendered_text": None,
            }
            self._log(report, activate=False)
            return report

        _atomic_write_json(self.active_path, active)
        report = {
            "installed": True,
            "simulated_crash": False,
            "bundle_id": stored_manifest.get("bundle_id"),
            "book_id": stored_manifest.get("book_id"),
            "download_bytes": plan.download_bytes,
            "download_objects": plan.download_objects,
            "reusable_objects": plan.reusable_objects,
            "total_bytes": plan.total_bytes,
            "used_dictionaries": plan.used_dictionaries,
            "rendered_text": render["rendered_text"],
        }
        self._log(report, activate=True)
        return report

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _load_items(self, object_id: str) -> tuple[bytes, ...]:
        entry = self._load_verified(object_id)
        if entry is None:
            raise DeviceError(f"missing or corrupt dictionary {object_id!r}", "missing_object")
        from .offline_codec import parse_dictionary_body

        document = entry["document"]
        assert isinstance(document, dict)
        return parse_dictionary_body(bytes.fromhex(document["body_hex"]))

    def _load_body(self, object_id: str) -> bytes:
        entry = self._load_verified(object_id)
        if entry is None:
            raise DeviceError(f"missing or corrupt object {object_id!r}", "missing_object")
        document = entry["document"]
        assert isinstance(document, dict)
        return bytes.fromhex(document["body_hex"])

    def _render(self, manifest: dict[str, object], choices: dict[str, dict[str, object]]) -> dict[str, object]:
        return materialize(manifest, choices, self._load_items, self._load_body)

    def read(self) -> dict[str, object]:
        active = self.read_active()
        if not active:
            raise DeviceError("no book is active on this device", "no_active_book")
        manifest = active["manifest"]
        choices = active["choices"]
        result = self._render(manifest, choices)
        result["bundle_id"] = active.get("bundle_id")
        result["book_id"] = active.get("book_id")
        return result

    # ------------------------------------------------------------------
    # logging / cache management
    # ------------------------------------------------------------------
    def _log(self, report: dict[str, object], *, activate: bool) -> None:
        history: list[dict[str, object]] = []
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, encoding="utf-8") as fh:
                    history = json.load(fh)
            except (OSError, ValueError):
                history = []
        history.append(
            {
                "bundle_id": report.get("bundle_id"),
                "download_bytes": report.get("download_bytes"),
                "downloaded": report.get("download_objects"),
                "reused": report.get("reusable_objects"),
                "activated": activate,
            }
        )
        _atomic_write_json(self.log_path, history)

    def download_total(self) -> int:
        if not os.path.exists(self.log_path):
            return 0
        with open(self.log_path, encoding="utf-8") as fh:
            history = json.load(fh)
        return sum(int(item.get("download_bytes") or 0) for item in history)

    def _prune_unprotected(self, keep: set[str]) -> int:
        removed = 0
        if not os.path.isdir(self.objects_dir):
            return 0
        for name in os.listdir(self.objects_dir):
            path = os.path.join(self.objects_dir, name)
            if not name.endswith(".obj"):
                if ".tmp-" in name:
                    os.remove(path)
                continue
            oid = name[:-4]
            if oid in keep:
                continue
            os.remove(path)
            removed += 1
        return removed

    def clear_cache(self) -> None:
        """Delete unprotected cache; never touch protected/active objects."""

        protected = self._protected_objects()
        for name in os.listdir(self.objects_dir):
            path = os.path.join(self.objects_dir, name)
            oid = name[:-4] if name.endswith(".obj") else None
            if ".tmp-" in name or not name.endswith(".obj"):
                os.remove(path)
                continue
            if oid in protected:
                continue
            if oid is not None:
                os.remove(path)

    def reset(self) -> None:
        if os.path.isdir(self.root):
            shutil.rmtree(self.root)
        os.makedirs(self.objects_dir, exist_ok=True)


def _all_object_ids(manifest: dict[str, object]) -> set[str]:
    ids = {str(d["object_id"]) for d in manifest["dictionaries"]}
    for block in manifest["blocks"]:
        for rep in block["representations"]:
            ids.add(str(rep["object_id"]))
    return ids
