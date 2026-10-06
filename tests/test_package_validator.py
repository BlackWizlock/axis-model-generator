"""Portable report aggregation and safe malformed-input diagnostics."""

import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fixtures.package_builders import make_package, make_scene, pack_array
from model_generator.package_manifest import PackageError, PackageLimits
from model_generator.package_validator import validate_package_bytes, validate_package_path


class PackageValidatorTests(unittest.TestCase):
    def test_success_inventory_measurements_and_research_boundaries(self):
        data = make_package()
        report = validate_package_bytes(data)
        decoded = json.loads(report.to_json())
        self.assertFalse(report.has_failures())
        self.assertEqual(report.input_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(report.profile, {"id": "portable-package-p0", "status": "research",
                                          "normative_readiness": False})
        self.assertEqual(report.coverage["package_status"], "passed")
        self.assertEqual(report.coverage["input_hash_status"], "available")
        for area, state in (("technical", "partial"), ("profile", "research"),
                            ("procedure", "unknown"), ("external", "not_checked")):
            self.assertEqual(report.coverage[area], state)
        self.assertEqual(report.coverage["capabilities"]["geometry"]["state"], "available")
        self.assertEqual(report.coverage["capabilities"]["ifc"]["state"], "missing")
        self.assertEqual(report.coverage["capabilities"]["coordinates"]["state"], "missing")
        files = {row["file"]: row for row in decoded["files"]}
        self.assertEqual(set(files), {"scene.json", "geometry/vertices.bin", "geometry/triangles.bin"})
        self.assertEqual(files["geometry/vertices.bin"]["bytes"], 72)
        self.assertEqual(files["geometry/vertices.bin"]["role"], "vertices")
        vertices = pack_array("float64", [(0, 0, 0), (1, 0, 0), (0, 1, 0)])
        self.assertEqual(files["geometry/vertices.bin"]["sha256"], hashlib.sha256(vertices).hexdigest())
        self.assertEqual(files["scene.json"]["mesh_vertices"], 3)
        self.assertEqual(files["scene.json"]["rendered_triangles"], 1)
        self.assertEqual(files["scene.json"]["processing_bounds"],
                         {"min": [0, 0, 0], "max": [1, 1, 0]})
        self.assertEqual(files["scene.json"]["height_residual"], None)
        self.assertTrue(any(f.status == "not_checked" for f in report.findings))
        self.assertEqual([row["file"] for row in decoded["files"]], sorted(files))

    def test_preserves_valid_large_float64_measurements(self):
        offset = 2**40
        vertices = pack_array("float64", [(offset, 0, 0), (offset + 0.125, 0, 0), (offset, 0.25, 0)])
        report = validate_package_bytes(make_package(member_overrides={"geometry/vertices.bin": vertices}))
        scene = next(row for row in json.loads(report.to_json())["files"] if row["file"] == "scene.json")
        self.assertEqual(scene["processing_bounds"],
                         {"min": [offset, 0, 0], "max": [offset + 0.125, 0.25, 0]})

    def test_scene_error_preserves_inventory_and_source_address_without_fake_measurements(self):
        scene = make_scene()
        key = scene["instances"][0]["element_key"]
        scene["instances"][0]["mesh_id"] = "missing"
        report = validate_package_bytes(make_package(scene_updates=scene))
        self.assertTrue(report.has_failures())
        self.assertEqual(report.coverage["package_status"], "failed")
        self.assertEqual(report.coverage["capabilities"], {})
        self.assertEqual(len(report.files), 3)
        self.assertFalse(any("mesh_vertices" in row for row in report.files))
        failure = next(f for f in report.findings if f.status == "fail")
        self.assertEqual(failure.rule_id, "package.reference")
        self.assertEqual(failure.file, "scene.json")
        self.assertEqual(failure.actual["element_key"], key)
        self.assertTrue(failure.actual["action"])
        self.assertIn(failure.actual["action"], failure.message)
        self.assertEqual(failure.expected, {"value": None})

    def test_hash_error_has_address_actual_expected_and_no_unverified_inventory(self):
        import io
        import zipfile
        data = make_package()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(members["manifest.json"])
        descriptor = next(row for row in manifest["files"] if row["path"] == "geometry/vertices.bin")
        actual_hash = descriptor["sha256"]
        descriptor["sha256"] = "0" * 64
        members["manifest.json"] = json.dumps(manifest).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        report = validate_package_bytes(output.getvalue())
        failure = report.findings[0]
        self.assertEqual(failure.rule_id, "package.hash")
        self.assertEqual(failure.file, "geometry/vertices.bin")
        self.assertEqual(failure.actual["value"], actual_hash)
        self.assertIsNone(failure.actual["element_key"])
        self.assertEqual(failure.expected, {"value": "0" * 64})
        self.assertEqual(report.files, [])

    def test_unsupported_is_failure_without_claimed_capabilities(self):
        report = validate_package_bytes(make_package(manifest_updates={"package_version": {"major": 2, "minor": 0}}))
        self.assertTrue(report.has_failures())
        self.assertEqual(report.coverage["package_status"], "unsupported")
        self.assertEqual(report.coverage["capabilities"], {})
        self.assertEqual(report.findings[0].rule_id, "package.unsupported")

    def test_syntax_errors_and_duplicate_surrogate_keys_remain_serializable(self):
        for scene in (b"{", b'{"\\ud800": 1, "\\ud800": 2}'):
            with self.subTest(scene=scene):
                report = validate_package_bytes(make_package(member_overrides={"scene.json": scene}))
                self.assertTrue(report.has_failures())
                self.assertEqual(report.findings[0].rule_id, "package.json")
                self.assertEqual(report.findings[0].file, "scene.json")
                report.to_json().encode("utf-8", errors="strict")

    def test_recursive_sanitization_of_error_payload_preserves_finite_values(self):
        error = PackageError("package.geometry", "Malformed \ud800 diagnostic", file="scene.json",
                             actual={"nested": [math.nan, {"bad\udfff": math.inf}, -math.inf, 0.125, 7]},
                             expected={"nested": [math.inf, "\ud800"]},
                             element_key={"document_id": "root", "link_instance_path": [],
                                          "unique_id": "bad\udfff"}, action="Fix \ud800 value")
        # A consumer may receive richer nested diagnostics from a reader version.
        # Exercise the adapter boundary, keeping real container/report processing.
        with patch("model_generator.package_validator.inspect_package_scene", side_effect=error):
            report = validate_package_bytes(make_package())
        text = report.to_json()
        text.encode("utf-8", errors="strict")
        value = json.loads(text)["findings"][0]
        self.assertEqual(value["actual"]["value"]["nested"],
                         ["nan", {"bad�": "inf"}, "-inf", 0.125, 7])
        self.assertEqual(value["expected"]["value"], {"nested": ["inf", "�"]})
        self.assertEqual(value["actual"]["element_key"]["unique_id"], "bad�")
        self.assertTrue(report.has_failures())

    def test_bounded_path_input_has_no_fake_hash_and_preserves_source(self):
        data = make_package()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.zip"
            path.write_bytes(data)
            valid = validate_package_path(path)
            self.assertEqual(valid.to_json(), validate_package_bytes(data).to_json())
            report = validate_package_path(path, PackageLimits(input_bytes=len(data) - 1))
            self.assertEqual(report.findings[0].rule_id, "package.budget")
            self.assertEqual(report.input_sha256, "")
            self.assertEqual(report.coverage["input_hash_status"], "not_available")
            self.assertEqual(report.coverage["package_status"], "failed")
            self.assertEqual(path.read_bytes(), data)

    def test_supplied_oversized_bytes_still_have_a_real_hash(self):
        data = make_package()
        report = validate_package_bytes(data, PackageLimits(input_bytes=len(data) - 1))
        self.assertEqual(report.findings[0].rule_id, "package.budget")
        self.assertEqual(report.input_sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(report.coverage["input_hash_status"], "available")

    def test_path_io_errors_are_operational(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(OSError):
                validate_package_path(Path(directory) / "missing.zip")
