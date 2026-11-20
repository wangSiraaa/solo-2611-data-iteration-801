"""End-to-end offline delivery demo.

Exports offline bundles from the **actually frozen** PACK1/PACK2 chunks, then
installs them through a *separate process* (:mod:`delivery_system.offline_worker`)
that only sees a device cache directory and a download manifest.  The
publisher-side scripts (publishing, reading old packages) keep working exactly
as before and are re-checked here.

Scenarios
---------
``phrase_codec``      single block ``abcdef`` with dictionary ``abcd, abc, def``.
``atomic_update``     old book in service, target book fits alone but the
                      atomic update (old protected + new) does not.
``crash_and_bad``     power loss before switch, and a corrupted block.
``dictionary_choice`` the same dictionary shared across blocks is charged once.

Every byte figure below is an object *body* byte count; manifest/control
metadata is not budgeted.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from typing import Any

from .delivery_repository import DeliveryRepository
from .offline_bundle import build_bundle, read_manifest
from .offline_codec import PhraseDictionary, chunk_object_id
from .offline_device import DeviceError, ReaderDevice
from .package_io import read_package, validate_package, write_package
from .source_catalog import PublishedSourceReader, SourceCatalog

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MATERIALS = os.path.join(os.path.dirname(HERE), "materials", "753")


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------
def _worker(*args: str) -> dict[str, Any]:
    """Run the device worker in its own interpreter; return parsed JSON."""

    proc = subprocess.run(
        [sys.executable, "-m", "delivery_system.offline_worker", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=os.path.dirname(HERE),
    )
    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {"ok": False, "reason": "worker_protocol", "stdout": proc.stdout}
    payload["_returncode"] = proc.returncode
    if proc.stderr.strip():
        payload["_stderr"] = proc.stderr.strip()
    return payload


def _fake_package(package_id: str, text: str) -> dict[str, object]:
    """A one-block package used by the pure codec scenarios."""

    return {
        "format": "translation-package/1",
        "package_id": package_id,
        "document_id": "CODEC",
        "language": "EN",
        "source_snapshot": [{"segment_id": "S1", "revision": 1}],
        "chunks": [
            {
                "translation_id": "T1",
                "source_ids": ["S1"],
                "source_revisions": [1],
                "text": text,
            }
        ],
        "rendered_text": text,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


class _Report:
    def __init__(self) -> None:
        self.ok = True
        self.steps: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    def step(self, name: str, **payload: Any) -> None:
        self.steps.append({"name": name, **payload})

    def check(self, name: str, condition: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            self.ok = False

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "steps": self.steps, "checks": self.checks}


# ----------------------------------------------------------------------
# scenario: phrase dictionary codec (abcdef / abcdeg)
# ----------------------------------------------------------------------
def _scenario_phrase_codec(root: str, report: _Report) -> None:
    dictionary = PhraseDictionary.from_strings("PH", ["abcd", "abc", "def"])
    report.step(
        "phrase_dictionary",
        dictionary_id=dictionary.dictionary_id,
        items=[item.decode("utf-8") for item in dictionary.items],
        size=dictionary.size,
    )
    good = _fake_package("CODEC_ABCDEF", "abcdef")
    good_dir = os.path.join(root, "codec_good")
    build_bundle(good, [dictionary], good_dir)
    good_manifest = read_manifest(os.path.join(good_dir, "manifest.json"))
    reps = {r["kind"]: r["size"] for r in good_manifest["blocks"][0]["representations"]}
    report.step("abcdef_representations", **reps)
    report.check("dict_body_is_2_bytes", reps["dictionary"] == 2, reps["dictionary"])
    report.check("literal_body_is_7_bytes", reps["literal"] == 7, reps["literal"])

    # Space is ample: a device that already holds the dictionary needs 2 bytes.
    installed_dir = os.path.join(root, "codec_installed")
    installed = ReaderDevice(installed_dir)
    installed.seed_object_from_bundle(
        good_dir, good_manifest["dictionaries"][0]["object_id"]
    )
    installed_result = _worker(
        "install", "--device", installed_dir, "--bundle", good_dir,
        "--capacity", "1000", "--traffic", "2",
    )
    read_back = _worker("read", "--device", installed_dir)
    report.step(
        "installed_device_abcdef",
        install=installed_result,
        read=read_back.get("book"),
    )
    report.check("installed_device_uses_2_bytes", installed_result.get("ok") and installed_result["report"]["download_bytes"] == 2, installed_result)
    report.check(
        "installed_device_restores_abcdef",
        read_back.get("book", {}).get("rendered_text") == "abcdef",
        read_back,
    )

    # Empty device, 7-byte traffic budget: pure literal must be chosen (7 bytes).
    empty_dir = os.path.join(root, "codec_empty")
    empty_result = _worker(
        "install", "--device", empty_dir, "--bundle", good_dir,
        "--capacity", "1000", "--traffic", "7",
    )
    empty_read = _worker("read", "--device", empty_dir)
    report.step("empty_device_abcdef", install=empty_result, read=empty_read.get("book"))
    report.check("empty_device_uses_7_bytes", empty_result.get("ok") and empty_result["report"]["download_bytes"] == 7, empty_result)
    report.check(
        "empty_device_picks_literal",
        empty_result.get("ok") and empty_result["report"]["used_dictionaries"] == [],
        empty_result,
    )
    report.check(
        "empty_device_restores_abcdef",
        empty_read.get("book", {}).get("rendered_text") == "abcdef",
        empty_read,
    )

    # abcdeg: no two dictionary instructions reproduce it, so a 2-byte budget
    # must be refused; a 4-byte dictionary or 7-byte literal is the alternative.
    bad = _fake_package("CODEC_ABCDEG", "abcdeg")
    bad_dir = os.path.join(root, "codec_bad")
    build_bundle(bad, [dictionary], bad_dir)
    bad_manifest = read_manifest(os.path.join(bad_dir, "manifest.json"))
    bad_reps = {r["kind"]: r["size"] for r in bad_manifest["blocks"][0]["representations"]}
    report.step("abcdeg_representations", **bad_reps)
    report.check("abcdeg_has_no_2_byte_dictionary_body", bad_reps["dictionary"] == 4, bad_reps)
    # Prove it directly: no ordered pair of dictionary items equals abcdeg.
    two_instruction = any(
        dictionary.items[i] + dictionary.items[j] == b"abcdeg"
        for i in range(len(dictionary.items))
        for j in range(len(dictionary.items))
    )
    report.check("abcdeg_no_two_dictionary_spelling", not two_instruction, two_instruction)

    tight_dir = os.path.join(root, "codec_bad_tight")
    tight_device = ReaderDevice(tight_dir)
    tight_device.seed_object_from_bundle(
        bad_dir, bad_manifest["dictionaries"][0]["object_id"]
    )
    tight_result = _worker(
        "install", "--device", tight_dir, "--bundle", bad_dir,
        "--capacity", "1000", "--traffic", "2",
    )
    report.step("abcdeg_tight_budget", result=tight_result)
    report.check(
        "abcdeg_2_byte_budget_rejected",
        tight_result.get("ok") is False
        and tight_result.get("reason") == "traffic_limit_exceeded",
        tight_result,
    )


# ----------------------------------------------------------------------
# scenario: publish -> export -> atomic update
# ----------------------------------------------------------------------
def _load_publisher(root: str, materials: str) -> tuple[str, DeliveryRepository, SourceCatalog]:
    source_db = os.path.join(root, "source.db")
    delivery_db = os.path.join(root, "delivery.db")
    with open(os.path.join(materials, "source.json"), encoding="utf-8") as fh:
        source_doc = json.load(fh)
    document_id = source_doc["document_id"]
    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))
    catalog.create_document(
        document_id,
        [
            {
                "segment_id": seg["segment_id"],
                "revision": int(seg.get("revision", 1)),
                "text": seg["text"],
            }
            for seg in source_doc["segments"]
        ],
        document_revision=int(source_doc.get("revision", 1)),
    )
    with open(os.path.join(materials, "translations.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            repo.add_translation(
                item["translation_id"],
                document_id,
                item["language"],
                item["text"],
                item["source_refs"],
            )
    return document_id, repo, catalog


def _publish_frozen_packages(
    root: str, materials: str, report: _Report
) -> tuple[str, dict[str, Any], dict[str, Any], str, str, DeliveryRepository, SourceCatalog]:
    document_id, repo, catalog = _load_publisher(root, materials)
    packages_dir = os.path.join(root, "packages")
    os.makedirs(packages_dir, exist_ok=True)

    pack1 = repo.publish_language(document_id, "EN", "PACK1")
    pack1_path = os.path.join(packages_dir, "PACK1.json")
    write_package(pack1, pack1_path)

    catalog.revise_segment(document_id, "S2", "Beta revised.", revision=2)
    repo.add_translation(
        "T2_NEW",
        document_id,
        "EN",
        "Beta revised.",
        [{"segment_id": "S2", "revision": 2}],
    )
    pack2 = repo.publish_language(document_id, "EN", "PACK2")
    pack2_path = os.path.join(packages_dir, "PACK2.json")
    write_package(pack2, pack2_path)

    # The frozen files are the source of truth for the export.
    pack1_frozen = read_package(pack1_path)
    pack2_frozen = read_package(pack2_path)
    report.check("PACK1_valid", not validate_package(pack1_frozen), validate_package(pack1_frozen))
    report.check("PACK2_valid", not validate_package(pack2_frozen), validate_package(pack2_frozen))
    report.check(
        "PACK1_frozen_rev1",
        all(s["revision"] == 1 for s in pack1_frozen["source_snapshot"]),
        pack1_frozen["source_snapshot"],
    )
    report.check(
        "PACK2_frozen_S2_rev2",
        any(s["segment_id"] == "S2" and s["revision"] == 2 for s in pack2_frozen["source_snapshot"]),
        pack2_frozen["source_snapshot"],
    )
    return (
        document_id,
        pack1_frozen,
        pack2_frozen,
        pack1_path,
        pack2_path,
        repo,
        catalog,
    )


def _publish_atomic_document(root: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish a real old/new pair whose blocks share one long phrase.

    All four blocks in both versions contain ``COMMON``; the old and new
    versions otherwise have distinct text, so their chunk objects are disjoint
    while a single phrase dictionary is shared by every block.
    """

    common = "COMMONPHR"
    source_db = os.path.join(root, "source.db")
    delivery_db = os.path.join(root, "delivery.db")
    catalog = SourceCatalog(source_db)
    repo = DeliveryRepository(delivery_db, PublishedSourceReader(source_db))
    doc = "ATOMIC"
    catalog.create_document(
        doc,
        [{"segment_id": f"A{i}", "text": f"source {i}"} for i in range(1, 5)],
    )
    for i in range(1, 5):
        repo.add_translation(
            f"OLD{i}", doc, "EN", f"{common}old{i:04d}",
            [{"segment_id": f"A{i}", "revision": 1}],
        )
    old = repo.publish_language(doc, "EN", "PACK_OLD")

    for i in range(1, 5):
        catalog.revise_segment(doc, f"A{i}", f"source {i} revised", revision=2)
    for i in range(1, 5):
        repo.add_translation(
            f"NEW{i}", doc, "EN", f"{common}new{i:04d}",
            [{"segment_id": f"A{i}", "revision": 2}],
        )
    new = repo.publish_language(doc, "EN", "PACK_NEW")
    return old, new


def _scenario_atomic_update(root: str, report: _Report) -> None:
    old_pack, new_pack = _publish_atomic_document(root)
    # The dictionary holds the phrase every block shares, plus nothing else.
    dictionary = PhraseDictionary.from_strings("SHARED", ["COMMONPHR"])
    old_dir = os.path.join(root, "bundles", "OLD")
    new_dir = os.path.join(root, "bundles", "NEW")
    build_bundle(old_pack, [dictionary], old_dir)
    build_bundle(new_pack, [dictionary], new_dir)
    old_manifest = read_manifest(os.path.join(old_dir, "manifest.json"))
    new_manifest = read_manifest(os.path.join(new_dir, "manifest.json"))
    report.step(
        "atomic_exported_from_frozen_chunks",
        dictionary_size=dictionary.size,
        old_blocks=[b["block_id"] for b in old_manifest["blocks"]],
        new_blocks=[b["block_id"] for b in new_manifest["blocks"]],
        old_text=old_pack["rendered_text"],
        new_text=new_pack["rendered_text"],
    )
    report.check(
        "atomic_export_uses_frozen_ids",
        [b["block_id"] for b in new_manifest["blocks"]]
        == [c["translation_id"] for c in new_pack["chunks"]],
        [b["block_id"] for b in new_manifest["blocks"]],
    )

    # The dictionary is a real win because it is shared, not because any single
    # block is huge.  Charging its full size to each block would pick literals;
    # charging it once (as the planner does) picks the dictionary.
    per_block_literal = sum(
        {r["kind"]: r["size"] for r in b["representations"]}["literal"]
        for b in new_manifest["blocks"]
    )
    per_block_dict = sum(
        {r["kind"]: r["size"] for r in b["representations"]}["dictionary"]
        for b in new_manifest["blocks"]
    )
    dict_size = dictionary.size
    n_blocks = len(new_manifest["blocks"])
    literal_each_block = per_block_literal // n_blocks
    dict_body_each_block = per_block_dict // n_blocks
    # A planner that charges the shared dictionary to *every* block would see
    # (dict body + full dictionary) per block and prefer literals.
    naive_charge_each = (
        per_block_literal
        if literal_each_block < dict_body_each_block + dict_size
        else per_block_dict
    )
    shared_charge_once = dict_size + per_block_dict
    report.step(
        "dictionary_sharing",
        literal_bodies=per_block_literal,
        dictionary_bodies=per_block_dict,
        dictionary_object=dict_size,
        naive_per_block_choice=naive_charge_each,
        shared_choice=shared_charge_once,
    )
    report.check(
        "naive_per_block_would_pick_literal",
        naive_charge_each == per_block_literal,
        {"literal_each": literal_each_block, "dict_each_with_full_dict": dict_body_each_block + dict_size},
    )
    report.check(
        "shared_dictionary_is_cheaper",
        shared_charge_once < naive_charge_each,
        {"shared": shared_charge_once, "naive": naive_charge_each},
    )

    # Measure exact body-byte footprints with the planner.
    probe_old = ReaderDevice(os.path.join(root, "probe_old"))
    old_plan = probe_old.plan(old_manifest, total_capacity=10_000)
    probe_new = ReaderDevice(os.path.join(root, "probe_new"))
    new_plan = probe_new.plan(new_manifest, total_capacity=10_000)
    old_footprint = old_plan.total_bytes
    new_alone = new_plan.total_bytes
    report.step(
        "footprints",
        old_total_bytes=old_footprint,
        new_alone_bytes=new_alone,
        new_used_dictionaries=len(new_plan.used_dictionaries),
    )
    report.check(
        "new_book_uses_dictionary",
        len(new_plan.used_dictionaries) == 1,
        new_plan.used_dictionaries,
    )
    report.check(
        "shared_choice_matches_planner",
        new_alone == shared_charge_once,
        {"planner": new_alone, "expected": shared_charge_once},
    )

    # The target book fits alone but old-protected + new cannot coexist: the
    # atomic update does not fit at the target book's own footprint.
    tight_capacity = new_alone
    tight_dir = os.path.join(root, "device_tight")
    report.check(
        "target_book_fits_alone",
        tight_capacity >= new_alone,
        {"capacity": tight_capacity, "new_alone": new_alone},
    )
    report.check(
        "atomic_update_does_not_fit",
        old_footprint + new_alone > tight_capacity and old_footprint <= tight_capacity,
        {"capacity": tight_capacity, "old": old_footprint, "new": new_alone},
    )

    # Install the old book with room, then note its protected footprint.
    _worker(
        "install", "--device", tight_dir, "--bundle", old_dir,
        "--capacity", str(old_footprint), "--traffic", "1000",
    )
    old_read = _worker("read", "--device", tight_dir)
    report.check(
        "tight_device_has_old_book",
        old_read.get("book", {}).get("rendered_text") == old_pack["rendered_text"],
        old_read,
    )

    tight_device = ReaderDevice(tight_dir)
    refused_reason = None
    try:
        tight_device.install(new_manifest, new_dir, total_capacity=tight_capacity)
    except DeviceError as exc:
        refused_reason = exc.reason
    report.step("atomic_update_attempt", reason=refused_reason)
    report.check(
        "atomic_update_refused_capacity",
        refused_reason == "capacity_exceeded",
        refused_reason,
    )

    still_old = _worker("read", "--device", tight_dir)
    report.check(
        "old_still_serves_after_refusal",
        still_old.get("book", {}).get("rendered_text") == old_pack["rendered_text"],
        still_old,
    )

    # A fresh device with only room for the target book installs it alone and
    # the reader restores the full new translation.
    fresh_dir = os.path.join(root, "device_fresh")
    fresh = _worker(
        "install", "--device", fresh_dir, "--bundle", new_dir,
        "--capacity", str(new_alone), "--traffic", "1000",
    )
    fresh_read = _worker("read", "--device", fresh_dir)
    report.step("fresh_device_target_alone", install=fresh, read=fresh_read.get("book"))
    report.check("fresh_device_installs_target", fresh.get("ok") is True, fresh)
    report.check(
        "fresh_device_restores_full_translation",
        fresh_read.get("book", {}).get("rendered_text") == new_pack["rendered_text"],
        {"got": fresh_read.get("book", {}).get("rendered_text"), "want": new_pack["rendered_text"]},
    )

    # A roomy device completes the atomic update; old stays readable throughout,
    # then the reader restores the full new translation.  The shared dictionary
    # object is stored exactly once.
    roomy_dir = os.path.join(root, "device_roomy")
    roomy_capacity = old_footprint + new_alone + 512
    _worker(
        "install", "--device", roomy_dir, "--bundle", old_dir,
        "--capacity", str(roomy_capacity), "--traffic", "1000",
    )
    roomy_update = _worker(
        "install", "--device", roomy_dir, "--bundle", new_dir,
        "--capacity", str(roomy_capacity), "--traffic", "1000",
    )
    roomy_read = _worker("read", "--device", roomy_dir)
    report.step("roomy_atomic_update", install=roomy_update, read=roomy_read.get("book"))
    report.check("roomy_update_installs", roomy_update.get("ok") is True, roomy_update)
    report.check(
        "roomy_reader_restores_full_translation",
        roomy_read.get("book", {}).get("rendered_text") == new_pack["rendered_text"],
        {"got": roomy_read.get("book", {}).get("rendered_text"), "want": new_pack["rendered_text"]},
    )
    dict_object_id = dictionary.object_id
    stored = [
        name
        for name in os.listdir(os.path.join(roomy_dir, "objects"))
        if name == f"{dict_object_id}.obj"
    ]
    report.check("shared_dictionary_stored_once", len(stored) == 1, stored)

    # Re-planning the same book on the roomy device downloads nothing.
    again = ReaderDevice(roomy_dir).plan(new_manifest, total_capacity=roomy_capacity)
    report.check(
        "verified_objects_reused",
        again.download_bytes == 0,
        {"download_bytes": again.download_bytes},
    )


def _scenario_materials_export(root: str, materials: str, report: _Report) -> None:
    """Export the *actual* frozen PACK1/PACK2 chunks and read them offline."""

    (
        document_id,
        pack1,
        pack2,
        pack1_path,
        pack2_path,
        repo,
        catalog,
    ) = _publish_frozen_packages(root, materials, report)
    items: list[str] = []
    seen: set[str] = set()
    for package in (pack1, pack2):
        for chunk in package["chunks"]:
            if chunk["text"] not in seen:
                seen.add(chunk["text"])
                items.append(chunk["text"])
    dictionary = PhraseDictionary.from_strings("MATERIALS", items)

    old_dir = os.path.join(root, "bundles", "PACK1")
    new_dir = os.path.join(root, "bundles", "PACK2")
    build_bundle(pack1, [dictionary], old_dir)
    build_bundle(pack2, [dictionary], new_dir)
    old_manifest = read_manifest(os.path.join(old_dir, "manifest.json"))
    new_manifest = read_manifest(os.path.join(new_dir, "manifest.json"))
    report.step(
        "materials_export",
        PACK1_blocks=[b["block_id"] for b in old_manifest["blocks"]],
        PACK2_blocks=[b["block_id"] for b in new_manifest["blocks"]],
        PACK2_text=pack2["rendered_text"],
    )
    report.check(
        "materials_export_uses_frozen_ids",
        [b["block_id"] for b in new_manifest["blocks"]]
        == [c["translation_id"] for c in pack2["chunks"]],
        [b["block_id"] for b in new_manifest["blocks"]],
    )

    device_dir = os.path.join(root, "device")
    install = _worker(
        "install", "--device", device_dir, "--bundle", new_dir,
        "--capacity", "100000", "--traffic", "100000",
    )
    read = _worker("read", "--device", device_dir)
    report.step("materials_device", install=install, read=read.get("book"))
    report.check("materials_device_installs", install.get("ok") is True, install)
    report.check(
        "materials_device_restores_frozen_text",
        read.get("book", {}).get("rendered_text") == pack2["rendered_text"],
        {"got": read.get("book", {}).get("rendered_text"), "want": pack2["rendered_text"]},
    )
    report.check(
        "materials_old_package_still_readable",
        read_package(pack1_path)["rendered_text"] == pack1["rendered_text"],
        pack1_path,
    )
    report.check(
        "materials_publisher_cover_unchanged",
        repo.plan_cover(document_id, "EN").ok,
        None,
    )


# ----------------------------------------------------------------------
# scenario: crash before switch + corrupted block
# ----------------------------------------------------------------------
def _scenario_crash_and_bad(root: str, report: _Report) -> None:
    old_pack, new_pack = _publish_atomic_document(os.path.join(root, "pub"))
    dictionary = PhraseDictionary.from_strings("SHARED", ["COMMONPHR"])
    old_dir = os.path.join(root, "old")
    new_dir = os.path.join(root, "new")
    build_bundle(old_pack, [dictionary], old_dir)
    build_bundle(new_pack, [dictionary], new_dir)
    new_manifest = read_manifest(os.path.join(new_dir, "manifest.json"))
    device_dir = os.path.join(root, "device")

    _worker(
        "install", "--device", device_dir, "--bundle", old_dir,
        "--capacity", "100000", "--traffic", "100000",
    )
    old_text = _worker("read", "--device", device_dir)["book"]["rendered_text"]
    report.check("crash_device_has_old", old_text == old_pack["rendered_text"], old_text)

    # Power loss after new objects are written, before the switch.
    crash = _worker(
        "install", "--device", device_dir, "--bundle", new_dir,
        "--capacity", "100000", "--traffic", "100000", "--crash",
    )
    report.step("crash_before_switch", result=crash)
    report.check(
        "crash_left_old_active",
        crash.get("ok") is True and crash["report"]["installed"] is False,
        crash,
    )
    after_crash = _worker("read", "--device", device_dir)["book"]["rendered_text"]
    report.check("old_readable_after_crash", after_crash == old_pack["rendered_text"], after_crash)

    # Verified objects from the interrupted attempt are reused on retry: only
    # the shared dictionary was already present, so the retry costs the blocks.
    retry = ReaderDevice(device_dir).plan(new_manifest, total_capacity=100000)
    report.check(
        "verified_objects_survive_crash",
        retry.download_bytes < retry.total_bytes,
        {"download": retry.download_bytes, "total": retry.total_bytes},
    )
    completed = _worker(
        "install", "--device", device_dir, "--bundle", new_dir,
        "--capacity", "100000", "--traffic", "100000",
    )
    final = _worker("read", "--device", device_dir)["book"]["rendered_text"]
    report.check("retry_installs_new", completed.get("ok") is True, completed)
    report.check("retry_reads_new", final == new_pack["rendered_text"], {"got": final})

    # Corrupt every chunk object: the new book must not activate.
    bad_dir = os.path.join(root, "bad")
    build_bundle(new_pack, [dictionary], bad_dir)
    _corrupt_chunk_objects(bad_dir)
    bad_device = os.path.join(root, "device_bad")
    _worker(
        "install", "--device", bad_device, "--bundle", old_dir,
        "--capacity", "100000", "--traffic", "100000",
    )
    bad_old = _worker("read", "--device", bad_device)["book"]["rendered_text"]
    bad_result = _worker(
        "install", "--device", bad_device, "--bundle", bad_dir,
        "--capacity", "100000", "--traffic", "100000",
    )
    report.step("corrupt_block_attempt", result=bad_result)
    report.check(
        "corrupt_block_refused",
        bad_result.get("ok") is False and bad_result.get("reason") == "bad_block",
        bad_result,
    )
    after_bad = _worker("read", "--device", bad_device)["book"]["rendered_text"]
    report.check("old_readable_after_bad_block", after_bad == bad_old, after_bad)


def _corrupt_chunk_objects(bundle_dir: str) -> None:
    for name in sorted(os.listdir(bundle_dir)):
        if not name.startswith("chunk-"):
            continue
        path = os.path.join(bundle_dir, name)
        with open(path, encoding="utf-8") as fh:
            document = json.load(fh)
        document["body_hex"] = "00" + document["body_hex"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(document, fh)


# ----------------------------------------------------------------------
# scenario: dependency identity (same bytes, different dictionary)
# ----------------------------------------------------------------------
def _scenario_identity(report: _Report) -> None:
    body = bytes.fromhex("8182")
    with_a = chunk_object_id("dict-a", body)
    with_b = chunk_object_id("dict-b", body)
    report.step("dependency_identity", with_a=with_a, with_b=with_b, same=with_a == with_b)
    report.check("same_bytes_different_dictionary_distinct", with_a != with_b)
    report.check(
        "literal_identity_distinct_from_dictionary",
        chunk_object_id(None, body) not in (with_a, with_b),
    )


# ----------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------
def run_offline_demo(out_dir: str, materials: str = DEFAULT_MATERIALS) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    report = _Report()

    phrase_root = os.path.join(out_dir, "phrase")
    _scenario_phrase_codec(phrase_root, report)

    _scenario_atomic_update(os.path.join(out_dir, "atomic"), report)
    _scenario_crash_and_bad(os.path.join(out_dir, "crash"), report)
    _scenario_materials_export(os.path.join(out_dir, "materials"), materials, report)
    _scenario_identity(report)

    return report.as_dict()
