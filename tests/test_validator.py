import json
import unittest
from unittest import mock
from fixtures.builders import fbx_bytes, scene_bytes, zip_bytes
from model_generator.profiles import check_image
from model_generator.limits import Limits
from model_generator import validator
from model_generator.diagnostics import Finding
from model_generator.validator import validate_bytes, validate_fbx_bytes


class ValidatorTests(unittest.TestCase):
    def test_repeated_remarks_and_models_are_counted_not_listed(self):
        remarks = [Finding("fbx.transform", "warn", "a.fbx", [i, 0, 0], [0, 0, 0], f"wall {i}") for i in range(250)]
        remarks.insert(3, Finding("fbx.transform", "fail", "a.fbx", 7, None, "Invalid local transform"))
        remarks.append(Finding("fbx.hierarchy", "warn", "a.fbx", [1, 2], None, "Model hierarchy"))
        kept = validator._bounded(remarks, "a.fbx")
        self.assertEqual(kept[:101], remarks[:101])
        self.assertEqual(kept[101], remarks[-1])
        self.assertEqual(len(kept), 103)
        total = kept[-1]
        self.assertEqual((total.rule_id, total.status, total.actual), ("fbx.transform", "warn", 250))
        self.assertIn("150 more", total.message)
        with mock.patch.object(validator, "FILE_MODELS", 0):
            listed = validate_fbx_bytes(scene_bytes(axes=True)).files[0]
        self.assertEqual(listed["models"], [])
        self.assertEqual(listed["models_total"], len(validate_fbx_bytes(scene_bytes(axes=True)).files[0]["models"]))
        self.assertGreater(listed["models_total"], 0)

    def test_embedded_images_with_legacy_paths_and_coverage(self):
        report = validate_bytes(zip_bytes([("a.fbx", scene_bytes()), ("a_Ground.fbx", scene_bytes())]))
        data = json.loads(report.to_json())
        self.assertEqual(data["profile"]["status"], "research")
        self.assertEqual([f["triangles"] for f in data["files"]], [1, 1])
        self.assertFalse(report.has_failures())
        self.assertIn("georeference", data["coverage"]["not_checked"])
        self.assertEqual(data["coverage"]["external"], "not_checked")
        self.assertNotIn("ready", data)

    def test_missing_resource_is_a_failure(self):
        from fixtures.builders import prop
        data = fbx_bytes([("Objects", [], [("Texture", [prop("L", 4)], [])]),
                          ("Connections", [], [])])
        report = validate_bytes(zip_bytes([("a.fbx", data)]))
        self.assertIn("fbx.resource", [f.rule_id for f in report.findings if f.status == "fail"])

    def test_partial_package_and_unsupported_remain_addressed(self):
        report = validate_bytes(zip_bytes([("a.fbx", scene_bytes()), ("a_Ground.fbx", fbx_bytes([], 7500))]))
        self.assertEqual(len(report.files), 2)
        self.assertTrue(report.has_failures())
        self.assertIn("fbx.unsupported", [f.rule_id for f in report.findings])
        self.assertEqual(next(f for f in report.files if "Ground" in f["file"])["read_status"], "unsupported")
        json.loads(report.to_json())
        empty = validate_bytes(zip_bytes([("readme", b"text")]))
        self.assertIn("package.fbx_count", [f.rule_id for f in empty.findings if f.status == "fail"])
        missing_ground = validate_bytes(zip_bytes([("a.fbx", scene_bytes()), ("b.fbx", scene_bytes())]))
        self.assertIn("package.ground", [f.rule_id for f in missing_ground.findings])

    def test_standalone_fbx_reads_placed_scene_without_package_rules(self):
        report = validate_fbx_bytes(scene_bytes(axes=True))
        scene = next(f for f in report.findings if f.rule_id == "fbx.scene")
        self.assertEqual(scene.status, "pass")
        self.assertEqual(scene.actual, {"meshes": 1, "triangles": 1, "unit_meters": 1.0,
                                        "bounds": [[10.0, 0.0, 0.0], [11.0, 1.0, 0.0]]})
        self.assertFalse(report.has_failures())
        self.assertEqual([f["read_status"] for f in report.files], ["read"])
        self.assertFalse(any(f.rule_id.startswith(("package.", "zip.")) for f in report.findings))
        self.assertEqual(report.coverage["technical"], "partial")
        json.loads(report.to_json())

    def test_standalone_fbx_unknown_role_warns_inside_ground_range(self):
        from unittest.mock import patch
        from model_generator import validator
        real = validator.inspect_fbx
        for triangles, status in ((150000, "pass"), (150001, "warn"), (180000, "warn"), (180001, "fail")):
            def counted(tree, name, triangles=triangles):
                measurements, findings = real(tree, name)
                return {**measurements, "triangles": triangles}, findings
            with self.subTest(triangles=triangles), patch.object(validator, "inspect_fbx", counted):
                report = validate_fbx_bytes(scene_bytes(axes=True))
                self.assertEqual(next(f for f in report.findings if f.rule_id == "profile.triangles").status, status)
                packed = validate_bytes(zip_bytes([("a.fbx", scene_bytes()), ("a_Ground.fbx", scene_bytes())]))
                self.assertEqual({f.status for f in packed.findings if f.rule_id == "profile.triangles"},
                                 {"pass"} if triangles <= 150000 else {"pass", "fail"} if triangles <= 180000 else {"fail"})

    def test_standalone_fbx_reader_limit_is_not_a_model_defect(self):
        report = validate_fbx_bytes(scene_bytes())
        scene = next(f for f in report.findings if f.rule_id == "fbx.scene")
        self.assertEqual(scene.status, "not_checked")
        self.assertFalse(report.has_failures())

    def test_standalone_fbx_failures_stay_failures(self):
        for data, limits, rule, status in ((b"not-fbx", None, "fbx.unsupported", "unsupported"),
                                           (scene_bytes(axes=True)[:60], None, "fbx.integrity", "failed"),
                                           (fbx_bytes([], 7500), None, "fbx.unsupported", "unsupported"),
                                           (scene_bytes(axes=True), Limits(fbx_array_bytes=8), "fbx.budget", "failed")):
            with self.subTest(rule=rule, status=status):
                report = validate_fbx_bytes(data, limits)
                self.assertTrue(report.has_failures())
                self.assertEqual(report.files[0]["read_status"], status)
                self.assertNotIn("fbx.scene", [f.rule_id for f in report.findings])
                if rule:
                    self.assertIn(rule, [f.rule_id for f in report.findings if f.status == "fail"])

    def test_profile_png_boundaries_and_exceptions(self):
        base = {"width": 256, "height": 256, "bit_depth": 8, "alpha": False, "bytes": 100}
        self.assertFalse(any(f.status == "fail" for f in check_image(base, "a")))
        for changes in [{"width": 300}, {"bit_depth": 16}, {"alpha": True}, {"bytes": 3145729}]:
            self.assertTrue(any(f.status == "fail" for f in check_image(base | changes, "a")))
        for changes in [{"width": 128, "height": 128}, {"bytes": 3050000}]:
            self.assertTrue(any(f.status == "not_checked" for f in check_image(base | changes, "a")))
