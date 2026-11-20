"""Tests for the offline export / device install pipeline.

Covers: the phrase codec (shortest bodies, exact decode, the ``abcdeg`` no-
spelling case), object identity including dictionary dependencies, bundle
export from frozen chunk text, per-block representation choice, dictionary
sharing (charged once, not per block), capacity/protection planning, the
traffic budget, crash-safe atomic activation, bad-block rejection, and the
separate-process device worker.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

from delivery_system.offline_bundle import (
    build_bundle,
    read_manifest,
    validate_manifest,
)
from delivery_system.offline_codec import (
    PhraseDictionary,
    chunk_object_id,
    decode_body,
    parse_dictionary_body,
    shortest_body,
)
from delivery_system.offline_device import DeviceError, ReaderDevice
from delivery_system.offline_demo import run_offline_demo

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fake_package(package_id, texts):
    return {
        "format": "translation-package/1",
        "package_id": package_id,
        "document_id": "D",
        "language": "EN",
        "source_snapshot": [
            {"segment_id": f"S{i + 1}", "revision": 1} for i in range(len(texts))
        ],
        "chunks": [
            {
                "translation_id": f"T{i + 1}",
                "source_ids": [f"S{i + 1}"],
                "source_revisions": [1],
                "text": text,
            }
            for i, text in enumerate(texts)
        ],
        "rendered_text": "\n".join(texts),
        "content_sha256": hashlib.sha256("\n".join(texts).encode()).hexdigest(),
    }


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = PhraseDictionary.from_strings("D", ["abcd", "abc", "def"])

    def test_dictionary_size_and_body(self):
        self.assertEqual(self.dictionary.size, 13)
        self.assertEqual(self.dictionary.body, b"\x04abcd\x03abc\x03def")
        self.assertEqual(parse_dictionary_body(self.dictionary.body), (b"abcd", b"abc", b"def"))

    def test_abcdef_shortest_bodies(self):
        lit = shortest_body(b"abcdef", None)
        dic = shortest_body(b"abcdef", self.dictionary.items)
        self.assertEqual(len(lit), 7)
        self.assertEqual(lit[0], 6)
        self.assertEqual(len(dic), 2)
        self.assertEqual(decode_body(dic, self.dictionary.items), b"abcdef")
        self.assertEqual(decode_body(lit, None), b"abcdef")
        # Two dictionary instructions, both high-bit set.
        self.assertEqual(list(dic), [0x80 + 1, 0x80 + 2])

    def test_abcdeg_has_no_two_dictionary_spelling(self):
        dic = shortest_body(b"abcdeg", self.dictionary.items)
        self.assertEqual(decode_body(dic, self.dictionary.items), b"abcdeg")
        self.assertGreater(len(dic), 2)
        for i in range(len(self.dictionary.items)):
            for j in range(len(self.dictionary.items)):
                self.assertNotEqual(self.dictionary.items[i] + self.dictionary.items[j], b"abcdeg")

    def test_literal_and_dict_heads_are_disjoint(self):
        # Every literal head has the high bit clear; every dict head has it set.
        for n in (1, 50, 127):
            body = shortest_body(b"x" * n, None)
            self.assertFalse(body[0] & 0x80)
        body = shortest_body(b"abc", self.dictionary.items)
        self.assertTrue(body[0] & 0x80)

    def test_dictionary_bounds(self):
        with self.assertRaises(Exception):
            PhraseDictionary.from_strings("E", [])
        with self.assertRaises(Exception):
            PhraseDictionary.from_strings("E", ["a", "a"])
        with self.assertRaises(Exception):
            PhraseDictionary.from_strings("E", [""])
        with self.assertRaises(Exception):
            PhraseDictionary.from_strings("E", ["x" * 128])
        PhraseDictionary.from_strings("MAX", [f"i{i}" for i in range(127)])

    def test_unicode_roundtrip_is_byte_exact(self):
        items = ["é", "ü", "中文"]
        dictionary = PhraseDictionary.from_strings("U", items)
        text = "éü中文é".encode("utf-8")
        body = shortest_body(text, dictionary.items)
        self.assertEqual(decode_body(body, dictionary.items), text)

    def test_invalid_instruction_head_rejected(self):
        with self.assertRaises(Exception):
            decode_body(b"\x00abc", None)
        with self.assertRaises(Exception):
            decode_body(b"\xff", self.dictionary.items)


class IdentityTests(unittest.TestCase):
    def test_same_bytes_different_dictionary_are_distinct(self):
        body = bytes.fromhex("8182")
        self.assertNotEqual(chunk_object_id("a", body), chunk_object_id("b", body))
        self.assertNotEqual(chunk_object_id(None, body), chunk_object_id("a", body))

    def test_same_bytes_same_dependency_are_identical(self):
        body = bytes.fromhex("8182")
        self.assertEqual(chunk_object_id("a", body), chunk_object_id("a", body))


class BundleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dictionary = PhraseDictionary.from_strings("D", ["abcd", "abc", "def"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_export_reports_sizes_and_digests(self):
        bundle = os.path.join(self.tmp, "b")
        manifest = build_bundle(fake_package("P", ["abcdef"]), [self.dictionary], bundle)
        validate_manifest(manifest)
        block = manifest["blocks"][0]
        sizes = {r["kind"]: r["size"] for r in block["representations"]}
        self.assertEqual(sizes["literal"], 7)
        self.assertEqual(sizes["dictionary"], 2)
        self.assertEqual(block["text_sha256"], hashlib.sha256(b"abcdef").hexdigest())
        self.assertEqual(manifest["dictionaries"][0]["size"], 13)
        # Object files exist on disk and are independently loadable.
        self.assertTrue(os.path.exists(os.path.join(bundle, "manifest.json")))
        for rep in block["representations"]:
            self.assertTrue(
                os.path.exists(os.path.join(bundle, f"chunk-{rep['object_id']}.json"))
            )

    def test_export_rejects_too_many_blocks(self):
        with self.assertRaises(Exception):
            build_bundle(fake_package("P", [f"t{i}" for i in range(13)]), [], os.path.join(self.tmp, "x"))

    def test_export_rejects_too_many_dictionaries(self):
        dicts = [PhraseDictionary.from_strings(f"D{i}", [f"i{i}"]) for i in range(5)]
        with self.assertRaises(Exception):
            build_bundle(fake_package("P", ["abc"]), dicts, os.path.join(self.tmp, "x"))


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.dictionary = PhraseDictionary.from_strings("D", ["abcd", "abc", "def"])

    def tearDown(self):
        self._tmp.cleanup()

    def _bundle(self, texts, name):
        directory = os.path.join(self.tmp, name)
        build_bundle(fake_package(name, texts), [self.dictionary], directory)
        return directory, read_manifest(os.path.join(directory, "manifest.json"))

    def test_empty_device_prefers_literal_within_7_bytes(self):
        directory, manifest = self._bundle(["abcdef"], "b")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        report = device.install(manifest, directory, total_capacity=1000, traffic_limit=7)
        self.assertEqual(report["download_bytes"], 7)
        self.assertEqual(report["used_dictionaries"], [])
        self.assertEqual(device.read()["rendered_text"], "abcdef")

    def test_installed_dictionary_costs_2_bytes(self):
        directory, manifest = self._bundle(["abcdef"], "b")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.seed_object_from_bundle(directory, manifest["dictionaries"][0]["object_id"])
        report = device.install(manifest, directory, total_capacity=1000, traffic_limit=2)
        self.assertEqual(report["download_bytes"], 2)
        self.assertEqual(device.read()["rendered_text"], "abcdef")

    def test_abcdeg_2_byte_budget_rejected(self):
        directory, manifest = self._bundle(["abcdeg"], "b")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.seed_object_from_bundle(directory, manifest["dictionaries"][0]["object_id"])
        with self.assertRaises(DeviceError) as ctx:
            device.install(manifest, directory, total_capacity=1000, traffic_limit=2)
        self.assertEqual(ctx.exception.reason, "traffic_limit_exceeded")

    def test_shared_dictionary_charged_once(self):
        dictionary = PhraseDictionary.from_strings("C", ["COMMONPHR"])
        texts = [f"COMMONPHRnew{i:04d}" for i in range(4)]
        directory = os.path.join(self.tmp, "shared")
        build_bundle(fake_package("S", texts), [dictionary], directory)
        manifest = read_manifest(os.path.join(directory, "manifest.json"))
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        plan = device.plan(manifest, total_capacity=100000)
        # One dictionary object, charged once, plus four dictionary bodies.
        self.assertEqual(len(plan.used_dictionaries), 1)
        literal_total = sum(
            {r["kind"]: r["size"] for r in b["representations"]}["literal"]
            for b in manifest["blocks"]
        )
        self.assertLess(plan.total_bytes, literal_total)

    def test_capacity_keeping_old_protected(self):
        old_dir, old_manifest = self._bundle(["xxxxxxxxxx"], "old")
        new_dir, new_manifest = self._bundle(["yyyyyyyyyyyy"], "new")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.install(old_manifest, old_dir, total_capacity=100)
        # target fits alone but old+new does not
        with self.assertRaises(DeviceError) as ctx:
            device.install(new_manifest, new_dir, total_capacity=13)
        self.assertEqual(ctx.exception.reason, "capacity_exceeded")
        # old still serves
        self.assertEqual(device.read()["rendered_text"], "xxxxxxxxxx")
        # fresh device fits target alone
        fresh = ReaderDevice(os.path.join(self.tmp, "fresh"))
        fresh.install(new_manifest, new_dir, total_capacity=13)
        self.assertEqual(fresh.read()["rendered_text"], "yyyyyyyyyyyy")

    def test_crash_before_switch_keeps_old(self):
        old_dir, old_manifest = self._bundle(["abcdef"], "old")
        new_dir, new_manifest = self._bundle(["abcdeg"], "new")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.install(old_manifest, old_dir, total_capacity=100000)
        crash = device.install(
            new_manifest, new_dir, total_capacity=100000, simulate_crash=True
        )
        self.assertFalse(crash["installed"])
        self.assertEqual(device.read()["rendered_text"], "abcdef")
        # retry completes; verified objects are reused
        retry = device.install(new_manifest, new_dir, total_capacity=100000)
        self.assertTrue(retry["installed"])
        self.assertEqual(device.read()["rendered_text"], "abcdeg")

    def test_bad_block_never_activates(self):
        old_dir, old_manifest = self._bundle(["abcdef"], "old")
        # Build the good new bundle first, then corrupt its chunk objects.
        new_dir = os.path.join(self.tmp, "new")
        build_bundle(fake_package("N", ["abcdeg"]), [self.dictionary], new_dir)
        new_manifest = read_manifest(os.path.join(new_dir, "manifest.json"))
        for name in os.listdir(new_dir):
            if name.startswith("chunk-"):
                path = os.path.join(new_dir, name)
                with open(path, encoding="utf-8") as fh:
                    document = json.load(fh)
                document["body_hex"] = "00" + document["body_hex"]
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(document, fh)
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.install(old_manifest, old_dir, total_capacity=100000)
        with self.assertRaises(DeviceError) as ctx:
            device.install(new_manifest, new_dir, total_capacity=100000)
        self.assertEqual(ctx.exception.reason, "bad_block")
        self.assertEqual(device.read()["rendered_text"], "abcdef")

    def test_reused_objects_cost_nothing(self):
        directory, manifest = self._bundle(["abcdef"], "b")
        device = ReaderDevice(os.path.join(self.tmp, "dev"))
        device.install(manifest, directory, total_capacity=1000, traffic_limit=7)
        plan = device.plan(manifest, total_capacity=1000)
        self.assertEqual(plan.download_bytes, 0)


class WorkerProcessTests(unittest.TestCase):
    """The device worker runs in its own interpreter, using only files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, "-m", "delivery_system.offline_worker", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
        )
        return proc.returncode, json.loads(proc.stdout) if proc.stdout.strip() else {}

    def test_worker_install_read_cycle(self):
        dictionary = PhraseDictionary.from_strings("D", ["abcd", "abc", "def"])
        bundle = os.path.join(self.tmp, "b")
        build_bundle(fake_package("P", ["abcdef"]), [dictionary], bundle)
        device = os.path.join(self.tmp, "dev")
        rc, payload = self._run(
            "install", "--device", device, "--bundle", bundle,
            "--capacity", "1000", "--traffic", "7",
        )
        self.assertEqual(rc, 0)
        self.assertEqual(payload["report"]["download_bytes"], 7)
        rc, payload = self._run("read", "--device", device)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["book"]["rendered_text"], "abcdef")
        rc, payload = self._run("space", "--device", device)
        self.assertEqual(rc, 0)
        self.assertEqual(payload["space"]["active_book"], "P")


class FullDemoTests(unittest.TestCase):
    def test_offline_demo_all_checks_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = run_offline_demo(tmp)
        self.assertTrue(report["ok"], [c for c in report["checks"] if not c["ok"]])
        names = {c["name"] for c in report["checks"]}
        for required in (
            "dict_body_is_2_bytes",
            "empty_device_uses_7_bytes",
            "abcdeg_2_byte_budget_rejected",
            "atomic_update_refused_capacity",
            "old_still_serves_after_refusal",
            "roomy_reader_restores_full_translation",
            "fresh_device_restores_full_translation",
            "shared_dictionary_stored_once",
            "crash_left_old_active",
            "corrupt_block_refused",
            "materials_device_restores_frozen_text",
        ):
            self.assertIn(required, names)


if __name__ == "__main__":
    unittest.main()
