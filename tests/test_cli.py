import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from fixtures.builders import scene_bytes, zip_bytes
from fixtures.package_builders import make_package


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        return subprocess.run([sys.executable, "-m", "model_generator", *map(str, args)],
                              capture_output=True, text=True, env=environment)

    def test_stdout_file_and_return_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.zip"
            content = zip_bytes([("a.fbx", scene_bytes()), ("a_Ground.fbx", scene_bytes())])
            source.write_bytes(content)
            valid = self.run_cli(source)
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(json.loads(valid.stdout)["profile"]["status"], "research")
            output = Path(directory) / "report.json"
            written = self.run_cli(source, "--output", output)
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(written.stdout, "")
            self.assertEqual(json.loads(output.read_text())["schema_version"], 1)
            source.write_bytes(zip_bytes([]))
            failed = self.run_cli(source)
            self.assertEqual(failed.returncode, 1)
            self.assertTrue(json.loads(failed.stdout)["findings"])

    def test_io_and_argument_errors_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.zip"
            source.write_bytes(zip_bytes([]))
            before = source.read_bytes()
            for args in [[], ["missing.zip"], [source, "--output", directory],
                         [source, "--output", source]]:
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertEqual(source.read_bytes(), before)

    def test_package_is_explicit_and_default_remains_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.zip"
            source.write_bytes(make_package())
            default = self.run_cli(source)
            explicit = self.run_cli(source, "--kind", "zip-fbx")
            self.assertEqual(default.returncode, 1)
            self.assertEqual(explicit.returncode, default.returncode)
            self.assertEqual(json.loads(default.stdout), json.loads(explicit.stdout))
            self.assertNotIn("package_status", json.loads(default.stdout)["coverage"])
            package = self.run_cli(source, "--kind", "package")
            self.assertEqual(package.returncode, 0, package.stderr)
            decoded = json.loads(package.stdout)
            self.assertEqual(decoded["coverage"]["package_status"], "passed")
            self.assertFalse(decoded["profile"]["normative_readiness"])
            output = Path(directory) / "report.json"
            written = self.run_cli(source, "--kind", "package", "--output", output)
            self.assertEqual(written.returncode, 0, written.stderr)
            self.assertEqual(written.stdout, "")
            self.assertEqual(json.loads(output.read_text()), decoded)

    def test_package_damaged_unsupported_and_syntax_errors_return_json_exit_one(self):
        inputs = [(b"damaged", "failed"),
                  (make_package(manifest_updates={"package_version": {"major": 2, "minor": 0}}), "unsupported"),
                  (make_package(member_overrides={"scene.json": b"{"}), "failed"),
                  (make_package(member_overrides={"scene.json": b'{"\\ud800": 1, "\\ud800": 2}'}), "failed")]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.zip"
            for content, status in inputs:
                with self.subTest(content=content[:12], status=status):
                    source.write_bytes(content)
                    result = self.run_cli(source, "--kind", "package")
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(result.stderr, "")
                    decoded = json.loads(result.stdout)
                    self.assertEqual(decoded["coverage"]["package_status"], status)
                    self.assertTrue(any(f["status"] == "fail" for f in decoded["findings"]))
                    self.assertNotIn("Traceback", result.stderr)

    def test_package_io_arguments_and_input_aliases_return_exit_two_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "package.zip"
            source.write_bytes(make_package())
            before = source.read_bytes()
            hardlink = Path(directory) / "hardlink.zip"
            os.link(source, hardlink)
            symlink = Path(directory) / "symlink.zip"
            symlink.symlink_to(source)
            for args in [["missing.zip", "--kind", "package"],
                         [source, "--kind", "unknown"],
                         [source, "--kind", "package", "--output", directory],
                         *[[source, "--kind", "package", "--output", target]
                           for target in (source, hardlink, symlink)]]:
                with self.subTest(args=args):
                    result = self.run_cli(*args)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertTrue(result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(source.read_bytes(), before)
