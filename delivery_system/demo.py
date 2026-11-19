"""End-to-end reproduction of materials/753.

Runs the four-segment / two-language scenario, then verifies the frozen packages
by invoking the *provided read-only client* (``package_reader.py``) as a
subprocess, so the check is against actual delivered text rather than our own
solver.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from .delivery_repository import DeliveryRepository
from .errors import NoCoverError
from .package_io import read_package, validate_package, write_package
from .source_catalog import PublishedSourceReader, SourceCatalog

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MATERIALS = os.path.join(os.path.dirname(HERE), "materials", "753")


def _client_read(reader_path: str, package_path: str) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, reader_path, package_path],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def run_demo(out_dir: str, materials: str = DEFAULT_MATERIALS) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    packages_dir = os.path.join(out_dir, "packages")
    os.makedirs(packages_dir, exist_ok=True)
    source_db = os.path.join(out_dir, "source.db")
    delivery_db = os.path.join(out_dir, "delivery.db")

    source_path = os.path.join(materials, "source.json")
    translations_path = os.path.join(materials, "translations.jsonl")
    reader_path = os.path.join(materials, "package_reader.py")

    with open(source_path, encoding="utf-8") as fh:
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
    with open(translations_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            item = json.loads(line)
            repo.add_translation(
                translation_id=item["translation_id"],
                document_id=document_id,
                language=item["language"],
                text=item["text"],
                source_refs=item["source_refs"],
            )

    report: dict[str, Any] = {"ok": True, "steps": [], "checks": []}

    def step(name: str, payload: dict[str, Any]) -> None:
        report["steps"].append({"name": name, **payload})

    def check(name: str, condition: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": bool(condition), "detail": detail})
        if not condition:
            report["ok"] = False

    # 1. EN initial publish (two legal covers exist: T12+T3+T4 or T1+T23+T4).
    pack1 = repo.publish_language(document_id, "EN", "PACK1")
    pack1_path = os.path.join(packages_dir, "PACK1.json")
    write_package(pack1, pack1_path)
    step(
        "publish_en_initial",
        {
            "package_id": "PACK1",
            "translation_ids": [c["translation_id"] for c in pack1["chunks"]],
            "rendered_text": pack1["rendered_text"],
        },
    )
    check("PACK1_valid", not validate_package(pack1), validate_package(pack1))
    check(
        "PACK1_exact_cover",
        [sid for c in pack1["chunks"] for sid in c["source_ids"]]
        == ["S1", "S2", "S3", "S4"],
    )

    # 2. FR: union covers all four but F12/F23 overlap S2 and cannot be split.
    fr = repo.plan_cover(document_id, "FR")
    step(
        "plan_fr_initial",
        {
            "cover_ok": fr.ok,
            "reason": None if fr.ok else fr.diagnosis.reason,
            "diagnosis": None if fr.ok else fr.diagnosis.to_dict(),
        },
    )
    check("FR_cover_rejected", not fr.ok)
    if not fr.ok:
        check("FR_reason_no_exact_cover", fr.diagnosis.reason == "no_exact_cover")
        check("FR_union_covers_all", fr.diagnosis.union_covers_all)
    fr_publish_blocked = False
    try:
        repo.publish_language(document_id, "FR", "PACK_FR")
    except NoCoverError:
        fr_publish_blocked = True
    check("FR_publish_blocked", fr_publish_blocked)
    check(
        "FR_no_empty_package_written",
        not os.path.exists(os.path.join(packages_dir, "PACK_FR.json")),
    )

    # 3. Source correction: S2 -> revision 2. T12/T23/F12/F23 become stale.
    catalog.revise_segment(document_id, "S2", "Beta revised.", revision=2)
    en_after = repo.plan_cover(document_id, "EN")
    step(
        "plan_en_after_revision",
        {
            "cover_ok": en_after.ok,
            "reason": None if en_after.ok else en_after.diagnosis.reason,
        },
    )
    check("EN_after_revision_blocked", not en_after.ok)
    if not en_after.ok:
        check("EN_reason_no_current_cover", en_after.diagnosis.reason == "no_current_cover")
        check(
            "EN_gap_is_S2",
            en_after.diagnosis.uncovered_segment_ids == ("S2",),
            en_after.diagnosis.uncovered_segment_ids,
        )
    states = {a.entry.translation_id: a.state for a in repo.assess(document_id, "EN")}
    step("en_states_after_revision", {"states": states})
    check("T12_stale", states.get("T12") == "stale_revision", states)
    check("T23_stale", states.get("T23") == "stale_revision", states)
    check("T3_still_current", states.get("T3") == "current_usable", states)

    # 4. A real new translation for the corrected revision restores delivery.
    repo.add_translation(
        translation_id="T2_NEW",
        document_id=document_id,
        language="EN",
        text="Beta revised.",
        source_refs=[{"segment_id": "S2", "revision": 2}],
    )
    pack2 = repo.publish_language(document_id, "EN", "PACK2")
    pack2_path = os.path.join(packages_dir, "PACK2.json")
    write_package(pack2, pack2_path)
    step(
        "publish_en_after_new_translation",
        {
            "package_id": "PACK2",
            "translation_ids": [c["translation_id"] for c in pack2["chunks"]],
            "rendered_text": pack2["rendered_text"],
            "source_snapshot": pack2["source_snapshot"],
        },
    )
    check("PACK2_valid", not validate_package(pack2), validate_package(pack2))
    check(
        "PACK2_uses_T2_NEW",
        "T2_NEW" in [c["translation_id"] for c in pack2["chunks"]],
    )
    check(
        "PACK2_S2_revision_2",
        any(
            s["segment_id"] == "S2" and s["revision"] == 2
            for s in pack2["source_snapshot"]
        ),
    )

    # 5. Old package keeps original source text and translation ids.
    old = read_package(pack1_path)
    new = read_package(pack2_path)
    check(
        "PACK1_immutable_source_rev1",
        all(s["revision"] == 1 for s in old["source_snapshot"]),
        old["source_snapshot"],
    )
    check(
        "PACK1_text_unchanged",
        old["rendered_text"] in ("Alpha Beta.\nGamma.\nDelta.", "Alpha.\nBeta Gamma.\nDelta."),
        old["rendered_text"],
    )
    check("PACK2_text_new", "Beta revised." in new["rendered_text"], new["rendered_text"])
    check("PACK1_sha_stable", old["content_sha256"] == pack1["content_sha256"])
    check("PACK2_sha_differs", new["content_sha256"] != old["content_sha256"])

    # 6. The *provided* read-only client actually reads both delivered packages.
    for label, path in (("PACK1", pack1_path), ("PACK2", pack2_path)):
        result = _client_read(reader_path, path)
        step(f"client_read_{label}", result)
        check(f"client_read_{label}_ok", result["returncode"] == 0, result)
        check(
            f"client_read_{label}_has_text",
            "Alpha." in result["stdout"] and "Delta." in result["stdout"],
            result["stdout"],
        )

    # 7. Republishing the same id with different content is rejected.
    immutable_enforced = False
    try:
        repo.publish_language(document_id, "EN", "PACK1")
    except Exception as exc:  # PackageExistsImmutable
        immutable_enforced = type(exc).__name__ == "PackageExistsImmutable"
    check("PACK1_id_reuse_rejected", immutable_enforced)

    report["packages"] = {
        "PACK1": pack1,
        "PACK2": pack2,
    }
    report["document_id"] = document_id
    return report
