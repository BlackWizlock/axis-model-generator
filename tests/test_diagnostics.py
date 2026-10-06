import json
import unittest

from model_generator.diagnostics import Finding, Report


class DiagnosticsTests(unittest.TestCase):
    def test_report_keeps_unchecked_and_orders_findings(self):
        report = Report(input_sha256="abc")
        report.findings = [
            Finding("z", "not_checked", "b.fbx", None, None, "Геопривязка", "procedure"),
            Finding("a", "warn", "a.fbx", 1, 0, "Трансформация"),
            Finding("b", "fail", "a.fbx", 4, 3, "Грань"),
            Finding("c", "pass", "a.fbx", 3, 3, "Треугольник"),
        ]
        data = json.loads(report.to_json())
        self.assertEqual([f["rule_id"] for f in data["findings"]], ["a", "b", "c", "z"])
        self.assertEqual(data["findings"][-1]["status"], "not_checked")
        self.assertEqual(data["schema_version"], 1)
        self.assertNotIn("ready", data)
        self.assertIn("Геопривязка", report.to_json())
        self.assertTrue(report.has_failures())

    def test_invalid_status_and_nonfinite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            Finding("x", "green", "", None, None, "")
        report = Report(input_sha256="abc", files=[{"value": float("nan")}])
        with self.assertRaises(ValueError):
            report.to_json()

    def test_warning_does_not_claim_failure(self):
        report = Report(input_sha256="abc", findings=[Finding("x", "warn", "", 1, 0, "")])
        self.assertFalse(report.has_failures())


if __name__ == "__main__":
    unittest.main()
