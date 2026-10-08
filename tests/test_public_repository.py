"""Verify the distributable license, documentation links and standalone source."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parents[1]
REPO_DOCS = "https://github.com/BlackWizlock/axis-model-generator/blob/main/"
REQUIRED = {
    "LICENSE", "NOTICE", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md",
    ".github/PULL_REQUEST_TEMPLATE.md", "tests/test_public_repository.py",
}
NOTICE = (
    "Axis Model Generator\n"
    "Copyright 2026 Axis Consult\n"
    "Разработано Axis Consult · Axis Platform\n"
    "Axis Consult https://axisconsult.ru\n"
    "Axis Platform https://axisplatform.ru\n"
).encode("utf-8")
EXTERNAL_LINKS = {
    "https://github.com/numpy/numpy/tree/v2.5.3",
    "https://github.com/python-pillow/Pillow/tree/12.3.0",
    "https://download.blender.org/release/Blender4.5/blender-4.5.14-linux-x64.tar.xz",
    "https://download.blender.org/source/blender-4.5.14.tar.xz",
    "https://pypi.org/pypi/numpy/2.5.3/json",
    "https://pypi.org/pypi/Pillow/12.3.0/json",
    "https://axisconsult.ru", "https://axisplatform.ru",
    "https://www.apache.org/licenses/LICENSE-2.0.txt",
    "https://github.com/BlackWizlock/axis-model-generator/security/advisories",
    "https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/"
    "configure-vulnerability-reporting/configure-for-a-repository",
}


def inventory():
    return json.loads((ROOT / "public/export-allowlist.json").read_text())["files"]


def run(cwd, *args, environment=None):
    return subprocess.run(list(args), cwd=cwd, env=environment,
                          capture_output=True, text=True, timeout=120)


class PublicRepositoryTests(unittest.TestCase):
    def test_license_is_complete_official_apache_text(self):
        path = ROOT / "LICENSE"
        self.assertTrue(path.is_file(), "public source must include LICENSE")
        data = path.read_bytes()
        self.assertEqual(len(data), 11358)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30")

    def test_notice_preserves_approved_attribution(self):
        path = ROOT / "NOTICE"
        self.assertTrue(path.is_file(), "public source must include NOTICE")
        self.assertEqual(path.read_bytes(), NOTICE)

    def test_inventory_covers_repository_files_and_both_readme_locations(self):
        entries = inventory()
        pairs = {(entry["source"], entry["export"]) for entry in entries}
        self.assertTrue({(path, path) for path in REQUIRED} <= pairs)
        self.assertTrue({("docs/public/README.md", "README.md"),
                         ("docs/public/README.md", "docs/public/README.md"),
                         ("public/gitignore.template", ".gitignore"),
                         ("public/gitignore.template", "public/gitignore.template")} <= pairs)
        self.assertEqual(len(entries), len({entry["export"].casefold() for entry in entries}))
        for entry in entries:
            self.assertTrue((ROOT / entry["source"]).is_file(), entry["source"])

    def test_document_links_resolve_in_exported_inventory(self):
        entries = inventory()
        targets = {entry["export"]: entry["source"] for entry in entries}
        readme_links = set()
        for entry in entries:
            if not entry["export"].endswith(".md"):
                continue
            content = (ROOT / entry["source"]).read_text(encoding="utf-8")
            links = re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", content)
            for link in links:
                with self.subTest(document=entry["export"], link=link):
                    if link.startswith(REPO_DOCS):
                        target = unquote(urlparse(link[len(REPO_DOCS):]).path)
                    elif urlparse(link).scheme:
                        self.assertIn(link, EXTERNAL_LINKS)
                        continue
                    else:
                        target = (Path(entry["export"]).parent / unquote(urlparse(link).path)).as_posix()
                    self.assertIn(target, targets, "link must reference an exported file")
                    self.assertTrue((ROOT / targets[target]).is_file())
                    if entry["export"] == "README.md":
                        readme_links.add(target)
        self.assertTrue({"LICENSE", "NOTICE", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"}
                        <= readme_links)

    def test_exported_source_runs_cli_tests_and_reexports_without_parent_checkout(self):
        entries = inventory()
        sources = sorted({entry["source"] for entry in entries})
        self.assertTrue(REQUIRED <= set(sources), "repository documents must ship")
        with tempfile.TemporaryDirectory(prefix="public-repository-") as directory:
            workspace = Path(directory).resolve()
            source, first, second = (workspace / name for name in ("source", "first", "second"))
            source.mkdir()
            for path in sources:
                destination = source / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, destination)
            self.initialize_git(source, sources)
            self.export(source, first)
            manifest = json.loads((first / "public-manifest.json").read_text())
            self.assertEqual({entry["path"] for entry in manifest["files"]},
                             {entry["export"] for entry in entries})
            for entry in entries:
                self.assertEqual((source / entry["source"]).read_bytes(),
                                 (first / entry["export"]).read_bytes())
            self.assertFalse((first / ".git").exists())
            self.assertEqual((first / "README.md").read_bytes(),
                             (first / "docs/public/README.md").read_bytes())
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(first / "src")
            # Run every shipped suite except this end-to-end suite to avoid recursion.
            child_suite = (
                "from pathlib import Path; import unittest; "
                "loader=unittest.TestLoader(); suite=unittest.TestSuite(); "
                "[suite.addTests(loader.discover('tests', pattern=p.name)) "
                "for p in sorted(Path('tests').glob('test_*.py')) "
                "if p.name != 'test_public_repository.py']; "
                "result=unittest.TextTestRunner(verbosity=1).run(suite); "
                "raise SystemExit(not result.wasSuccessful())"
            )
            result = run(first, sys.executable, "-c", child_suite, environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            smoke = (
                "from pathlib import Path; from fixtures.builders import scene_bytes, zip_bytes; "
                "from fixtures.package_builders import make_package; "
                "Path('_output').mkdir(); "
                "Path('_output/example.zip').write_bytes(zip_bytes([('model.fbx',scene_bytes()),"
                "('model_Ground.fbx',scene_bytes())])); "
                "Path('_output/example-package.zip').write_bytes(make_package())"
            )
            environment["PYTHONPATH"] = os.pathsep.join([str(first / "src"), str(first / "tests")])
            result = run(first, sys.executable, "-c", smoke, environment=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name, extra in [("example.zip", []), ("example-package.zip", ["--kind", "package"])]:
                result = run(first, sys.executable, "-m", "model_generator", "_output/" + name,
                             *extra, environment=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["profile"]["status"], "research")
                self.assertFalse(report["profile"]["normative_readiness"])
            self.initialize_git(first, [entry["path"] for entry in manifest["files"]])
            self.export(first, second)
            repeated = json.loads((second / "public-manifest.json").read_text())
            self.assertEqual(manifest["files"], repeated["files"])
            self.assertNotEqual(manifest["source_revision"], repeated["source_revision"])
            self.assertFalse((second / ".git").exists())

    def initialize_git(self, root, paths):
        for args in [("init", "-q"), ("config", "user.name", "Synthetic Author"),
                     ("config", "user.email", "synthetic@users.noreply.github.com"),
                     ("add", "--", *paths), ("commit", "-qm", "synthetic standalone source")]:
            result = run(root, "git", *args)
            self.assertEqual(result.returncode, 0, result.stderr)

    def export(self, source, destination):
        result = run(source, sys.executable, str(source / "scripts/export-public.py"),
                     "--revision", "HEAD", "--output", str(destination))
        self.assertEqual(result.returncode, 0, result.stderr)
