import unittest

from fixtures.builders import scene_bytes, zip_bytes
from model_generator.profiles import check_image
from model_generator.validator import validate_bytes


class PublicProfileTests(unittest.TestCase):
    def test_empty_archive_has_anonymous_research_profile(self):
        report = validate_bytes(zip_bytes([]))
        self.assertEqual(report.profile, {
            "id": "technical-research-v1", "status": "research",
            "source": "public technical demonstration thresholds",
            "applicability": "not_checked", "normative_readiness": False})
        self.assertTrue(report.has_failures())

    def test_research_findings_keep_technical_thresholds(self):
        report = validate_bytes(zip_bytes([
            ("model.fbx", scene_bytes()), ("model_Ground.fbx", scene_bytes())]))
        image = {"width": 300, "height": 256, "bit_depth": 8,
                 "alpha": True, "bytes": 100}
        findings = report.findings + check_image(image, "example.png")
        for rule in ["package.fbx_count", "profile.triangles",
                     "profile.png_dimensions", "profile.png_alpha"]:
            selected = [f for f in findings if f.rule_id == rule]
            self.assertTrue(selected)
            for finding in selected:
                self.assertIn("research", finding.message.lower())
                self.assertNotIn("supplied task", finding.message.lower())
        self.assertFalse(report.has_failures())
        self.assertEqual([f.expected for f in findings if f.rule_id == "profile.triangles"],
                         [150000, 180000])
        self.assertTrue(all(f.status == "fail" for f in findings
                            if f.file == "example.png" and f.rule_id in {"profile.png_alpha", "profile.png_dimensions"}))
