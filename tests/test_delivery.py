"""Test suite (stdlib unittest; run with ``python -m unittest discover``).

Covers exactly the failure modes the brief warns about: union-only acceptance of
overlapping candidates, revision re-labelling, identity vs. position, immutable
packages, reorder compatibility, and scale.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

from delivery_system.coverage import assess_entry, solve_cover
from delivery_system.delivery_repository import DeliveryRepository
from delivery_system.errors import (
    InvalidTranslation,
    NoCoverError,
    PackageExistsImmutable,
    RevisionConflict,
    TranslationExists,
)
from delivery_system.models import SourceProduct, SourceRef, SourceSegment, TranslationEntry
from delivery_system.package_io import validate_package, write_package
from delivery_system.source_catalog import PublishedSourceReader, SourceCatalog

MATERIALS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "materials", "753"
)


def make_product(ids_and_revs):
    return SourceProduct(
        document_id="D",
        document_revision=1,
        segments=tuple(
            SourceSegment(segment_id=sid, position=i, revision=rev, text=f"t-{sid}")
            for i, (sid, rev) in enumerate(ids_and_revs)
        ),
    )


def entry(tid, refs, text="x", language="EN"):
    return TranslationEntry(
        translation_id=tid,
        document_id="D",
        language=language,
        text=text,
        source_refs=tuple(SourceRef(s, r) for s, r in refs),
    )


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.source_db = os.path.join(self.tmp, "source.db")
        self.delivery_db = os.path.join(self.tmp, "delivery.db")
        self.packages = os.path.join(self.tmp, "packages")
        os.makedirs(self.packages)
        self.catalog = SourceCatalog(self.source_db)
        self.repo = DeliveryRepository(
            self.delivery_db, PublishedSourceReader(self.source_db)
        )

    def tearDown(self):
        self._tmp.cleanup()

    def load_materials(self):
        with open(os.path.join(MATERIALS, "source.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        self.catalog.create_document(
            doc["document_id"],
            doc["segments"],
            document_revision=doc["revision"],
        )
        with open(os.path.join(MATERIALS, "translations.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    item = json.loads(line)
                    self.repo.add_translation(
                        item["translation_id"],
                        doc["document_id"],
                        item["language"],
                        item["text"],
                        item["source_refs"],
                    )
        return doc["document_id"]

    def publish(self, document_id, language, package_id):
        content = self.repo.publish_language(document_id, language, package_id)
        path = os.path.join(self.packages, f"{package_id}.json")
        write_package(content, path)
        return content, path


class CoverageSolverTests(unittest.TestCase):
    def test_union_covers_all_but_no_exact_cover(self):
        product = make_product([("S1", 1), ("S2", 1), ("S3", 1)])
        entries = [entry("A", [("S1", 1), ("S2", 1)]), entry("B", [("S2", 1), ("S3", 1)])]
        result = solve_cover(product, "EN", entries)
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnosis.reason, "no_exact_cover")
        self.assertTrue(result.diagnosis.union_covers_all)
        # B is the entry that crosses the blocked boundary at S3.
        self.assertIn("B", result.diagnosis.crossing_translation_ids)

    def test_simple_gap(self):
        product = make_product([("S1", 1), ("S2", 1), ("S3", 1)])
        result = solve_cover(product, "EN", [entry("A", [("S1", 1)]), entry("C", [("S3", 1)])])
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnosis.reason, "no_current_cover")
        self.assertEqual(result.diagnosis.uncovered_segment_ids, ("S2",))

    def test_exact_cover_multiple_choices_allowed(self):
        product = make_product([("S1", 1), ("S2", 1), ("S3", 1)])
        entries = [
            entry("A", [("S1", 1), ("S2", 1)]),
            entry("B", [("S3", 1)]),
            entry("C", [("S1", 1)]),
            entry("D", [("S2", 1), ("S3", 1)]),
        ]
        result = solve_cover(product, "EN", entries)
        self.assertTrue(result.ok)
        chosen = [c.translation_id for c in result.chunks]
        # Any legal exact cover is allowed; both ["A","B"] and ["C","D"] work.
        self.assertIn(chosen, (["A", "B"], ["C", "D"]))

    def test_stale_revision_not_reused(self):
        product = make_product([("S1", 1), ("S2", 2)])
        e = entry("A", [("S1", 1), ("S2", 1)])
        assessment = assess_entry(e, product, "EN", product.index_by_segment_id())
        self.assertEqual(assessment.state, "stale_revision")
        result = solve_cover(product, "EN", [e, entry("B", [("S1", 1)])])
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnosis.uncovered_segment_ids, ("S2",))

    def test_non_contiguous_rejected(self):
        product = make_product([("S1", 1), ("S2", 1), ("S3", 1)])
        e = entry("A", [("S1", 1), ("S3", 1)])
        assessment = assess_entry(e, product, "EN", product.index_by_segment_id())
        self.assertEqual(assessment.state, "non_contiguous")
        result = solve_cover(product, "EN", [e])
        self.assertFalse(result.ok)

    def test_out_of_order_refs_rejected(self):
        product = make_product([("S1", 1), ("S2", 1)])
        e = entry("A", [("S2", 1), ("S1", 1)])
        self.assertEqual(
            assess_entry(e, product, "EN", product.index_by_segment_id()).state,
            "non_contiguous",
        )

    def test_duplicate_segment_ref_rejected(self):
        product = make_product([("S1", 1), ("S2", 1)])
        e = entry("A", [("S1", 1), ("S1", 1)])
        self.assertEqual(
            assess_entry(e, product, "EN", product.index_by_segment_id()).state,
            "malformed",
        )

    def test_empty_text_never_selected(self):
        product = make_product([("S1", 1)])
        result = solve_cover(product, "EN", [entry("A", [("S1", 1)], text="   ")])
        self.assertFalse(result.ok)

    def test_unknown_segment_rejected(self):
        product = make_product([("S1", 1)])
        e = entry("A", [("S1", 1), ("S9", 1)])
        self.assertEqual(
            assess_entry(e, product, "EN", product.index_by_segment_id()).state,
            "unknown_segment",
        )

    def test_wrong_language_ignored(self):
        product = make_product([("S1", 1)])
        e = entry("A", [("S1", 1)], language="FR")
        self.assertEqual(
            assess_entry(e, product, "EN", product.index_by_segment_id()).state,
            "other_language",
        )


class CatalogTests(TempCase):
    def test_identity_survives_reorder(self):
        self.catalog.create_document(
            "D",
            [
                {"segment_id": "S1", "text": "a"},
                {"segment_id": "S2", "text": "b"},
                {"segment_id": "S3", "text": "c"},
            ],
        )
        product = self.catalog.move_segment("D", "S3", 0)
        self.assertEqual(product.segment_ids, ("S3", "S1", "S2"))
        self.assertEqual(
            [s.position for s in product.segments], [0, 1, 2]
        )
        self.assertEqual(self.catalog.get_segment("D", "S1").revision, 1)

    def test_revision_bumps_and_release_history(self):
        self.catalog.create_document("D", [{"segment_id": "S1", "text": "a"}])
        self.catalog.revise_segment("D", "S1", "b")
        self.catalog.revise_segment("D", "S1", "c")
        product = self.catalog.published_product("D")
        self.assertEqual(product.segments[0].revision, 3)
        releases = self.catalog.list_releases("D")
        self.assertEqual(len(releases), 3)
        first = self.catalog.product_at_release(releases[0]["release_id"])
        self.assertEqual(first.segments[0].text, "a")
        self.assertEqual(first.segments[0].revision, 1)

    def test_revision_must_increase(self):
        self.catalog.create_document("D", [{"segment_id": "S1", "text": "a"}])
        with self.assertRaises(RevisionConflict):
            self.catalog.revise_segment("D", "S1", "b", revision=1)

    def test_delivery_reader_sees_new_revision(self):
        self.catalog.create_document("D", [{"segment_id": "S1", "text": "a"}])
        reader = PublishedSourceReader(self.source_db)
        self.assertEqual(reader.published_product("D").segments[0].revision, 1)
        self.catalog.revise_segment("D", "S1", "b")
        self.assertEqual(reader.published_product("D").segments[0].revision, 2)


class DeliveryTests(TempCase):
    def test_materials_full_scenario(self):
        document_id = self.load_materials()

        pack1, pack1_path = self.publish(document_id, "EN", "PACK1")
        self.assertEqual(validate_package(pack1), [])
        self.assertEqual(
            [sid for c in pack1["chunks"] for sid in c["source_ids"]],
            ["S1", "S2", "S3", "S4"],
        )

        fr = self.repo.plan_cover(document_id, "FR")
        self.assertFalse(fr.ok)
        self.assertEqual(fr.diagnosis.reason, "no_exact_cover")
        with self.assertRaises(NoCoverError):
            self.repo.publish_language(document_id, "FR", "PACK_FR")
        self.assertFalse(os.path.exists(os.path.join(self.packages, "PACK_FR.json")))

        self.catalog.revise_segment(document_id, "S2", "Beta revised.", revision=2)
        en = self.repo.plan_cover(document_id, "EN")
        self.assertFalse(en.ok)
        self.assertEqual(en.diagnosis.reason, "no_current_cover")
        states = {a.entry.translation_id: a.state for a in self.repo.assess(document_id, "EN")}
        self.assertEqual(states["T12"], "stale_revision")
        self.assertEqual(states["T23"], "stale_revision")
        self.assertEqual(states["T3"], "current_usable")

        self.repo.add_translation(
            "T2_NEW", document_id, "EN", "Beta revised.", [{"segment_id": "S2", "revision": 2}]
        )
        pack2, pack2_path = self.publish(document_id, "EN", "PACK2")
        self.assertEqual(validate_package(pack2), [])
        self.assertIn("T2_NEW", [c["translation_id"] for c in pack2["chunks"]])
        self.assertEqual(
            [sid for c in pack2["chunks"] for sid in c["source_ids"]],
            ["S1", "S2", "S3", "S4"],
        )

        with open(pack1_path, encoding="utf-8") as fh:
            old = json.load(fh)
        with open(pack2_path, encoding="utf-8") as fh:
            new = json.load(fh)
        self.assertTrue(all(s["revision"] == 1 for s in old["source_snapshot"]))
        self.assertIn({"segment_id": "S2", "revision": 2}, new["source_snapshot"])
        self.assertEqual(old["content_sha256"], pack1["content_sha256"])
        self.assertNotEqual(old["content_sha256"], new["content_sha256"])

        # the provided read-only client must actually read both packages
        for path in (pack1_path, pack2_path):
            proc = subprocess.run(
                [sys.executable, os.path.join(MATERIALS, "package_reader.py"), path],
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Alpha.", proc.stdout)

    def test_package_immutable(self):
        document_id = self.load_materials()
        self.publish(document_id, "EN", "PACK1")
        self.catalog.revise_segment(document_id, "S2", "revised", revision=2)
        self.repo.add_translation(
            "T2_NEW", document_id, "EN", "revised", [{"segment_id": "S2", "revision": 2}]
        )
        with self.assertRaises(PackageExistsImmutable):
            self.repo.publish_language(document_id, "EN", "PACK1")

    def test_relabelling_old_translation_rejected(self):
        document_id = self.load_materials()
        # Adding the same id with a smuggled new revision is not allowed.
        with self.assertRaises(TranslationExists):
            self.repo.add_translation(
                "T12", document_id, "EN", "Alpha Beta.", [{"segment_id": "S1", "revision": 1}, {"segment_id": "S2", "revision": 2}]
            )

    def test_reorder_breaks_contiguity_of_cross_segment_entry(self):
        document_id = self.load_materials()
        # T12 covers S1,S2; moving S1 after S3 makes those refs non-contiguous.
        self.catalog.move_segment(document_id, "S1", 3)
        states = {a.entry.translation_id: a.state for a in self.repo.assess(document_id, "EN")}
        self.assertEqual(states["T12"], "non_contiguous")
        result = self.repo.plan_cover(document_id, "EN")
        self.assertTrue(result.ok)  # T1 / T23 / T4 still single- or pair-cover
        self.assertEqual(
            [sid for c in result.chunks for sid in c.source_ids], ["S2", "S3", "S4", "S1"]
        )

    def test_empty_source_refused(self):
        self.catalog.create_document("EMPTY", [])
        result = self.repo.plan_cover("EMPTY", "EN")
        self.assertFalse(result.ok)
        self.assertEqual(result.diagnosis.reason, "empty_source")
        with self.assertRaises(NoCoverError):
            self.repo.publish_language("EMPTY", "EN", "P")


class ScaleTests(TempCase):
    def test_100_segments_500_translations_5_languages(self):
        rng = random.Random(753)
        n = 100
        segments = [
            {"segment_id": f"S{i+1}", "revision": 1, "text": f"seg {i+1}"}
            for i in range(n)
        ]
        self.catalog.create_document("BIG", segments)

        languages = ["EN", "FR", "DE", "ES", "IT"]
        translations = []
        tid = 0
        for lang in languages:
            pos = 0
            while pos < n:
                width = rng.choice([1, 1, 2, 3])
                width = min(width, n - pos)
                refs = [{"segment_id": f"S{i+1}", "revision": 1} for i in range(pos, pos + width)]
                tid += 1
                translations.append((f"{lang}_{tid}", lang, refs))
                pos += width
        # Add overlapping decoys that must never be selected.
        for lang in languages:
            for _ in range(45):
                start = rng.randrange(0, n - 1)
                refs = [
                    {"segment_id": f"S{i+1}", "revision": 1}
                    for i in range(start, min(n, start + rng.choice([2, 3, 4])))
                ]
                tid += 1
                translations.append((f"{lang}_X{tid}", lang, refs))
        self.assertGreaterEqual(len(translations), 500)
        for t_id, lang, refs in translations:
            self.repo.add_translation(t_id, "BIG", lang, f"text {t_id}", refs)

        for lang in languages:
            result = self.repo.plan_cover("BIG", lang)
            self.assertTrue(result.ok, lang)
            flat = [sid for c in result.chunks for sid in c.source_ids]
            self.assertEqual(flat, [f"S{i+1}" for i in range(n)])
            # no overlapping decoy selected, and every chunk is contiguous
            for c in result.chunks:
                self.assertIn(c.translation_id, {t[0] for t in translations if t[1] == lang})
            content = self.repo.publish_language("BIG", lang, f"PKG_{lang}")
            self.assertEqual(validate_package(content), [])

        # revise one segment: every entry touching S50 goes stale, so the
        # surviving current entries leave a contiguous gap around S50.
        self.catalog.revise_segment("BIG", "S50", "changed")
        for lang in languages:
            result = self.repo.plan_cover("BIG", lang)
            self.assertFalse(result.ok, lang)
            self.assertIn("S50", result.diagnosis.uncovered_segment_ids)
        # fresh translations for the gap restore every language; refs to
        # unchanged segments keep their current revision, S50 uses revision 2.
        for lang in languages:
            gap = self.repo.plan_cover("BIG", lang).diagnosis.uncovered_segment_ids
            self.repo.add_translation(
                f"{lang}_FIX",
                "BIG",
                lang,
                "fixed",
                [
                    {"segment_id": sid, "revision": 2 if sid == "S50" else 1}
                    for sid in gap
                ],
            )
            result = self.repo.plan_cover("BIG", lang)
            self.assertTrue(result.ok, lang)
            flat = [sid for c in result.chunks for sid in c.source_ids]
            self.assertEqual(flat, [f"S{i+1}" for i in range(n)])


if __name__ == "__main__":
    unittest.main()
