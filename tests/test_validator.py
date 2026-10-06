import json
import unittest
from fixtures.builders import fbx_bytes, scene_bytes, zip_bytes
from model_generator.profiles import check_image
from model_generator.validator import validate_bytes


class ValidatorTests(unittest.TestCase):
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

    def test_profile_png_boundaries_and_exceptions(self):
        base = {"width": 256, "height": 256, "bit_depth": 8, "alpha": False, "bytes": 100}
        self.assertFalse(any(f.status == "fail" for f in check_image(base, "a")))
        for changes in [{"width": 300}, {"bit_depth": 16}, {"alpha": True}, {"bytes": 3145729}]:
            self.assertTrue(any(f.status == "fail" for f in check_image(base | changes, "a")))
        for changes in [{"width": 128, "height": 128}, {"bytes": 3050000}]:
            self.assertTrue(any(f.status == "not_checked" for f in check_image(base | changes, "a")))
