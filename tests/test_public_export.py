import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export-public.py"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


class PublicExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Synthetic Author")
        git(self.repo, "config", "user.email", "synthetic@users.noreply.github.com")
        self.assertTrue(SCRIPT.is_file(), "public exporter has not been implemented")
        spec = importlib.util.spec_from_file_location("public_export", SCRIPT)
        self.exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.exporter)
        self.output = self.root / "snapshot"

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self):
        # Only these synthetic temporary repositories belong wholly to this test.
        paths = git(self.repo, "ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        git(self.repo, "add", "--", *paths)
        git(self.repo, "commit", "-qm", "synthetic fixture")
        return git(self.repo, "rev-parse", "HEAD")

    def inventory(self, entries=None):
        self.write("public/export-allowlist.json", json.dumps({"schema_version": 1,
            "files": entries or [{"source": "src/example.py", "export": "src/example.py"}]}))

    def seed(self, content="print('public example')\n", entries=None):
        self.write("src/example.py", content)
        self.inventory(entries)
        return self.commit()

    def rejected(self, revision="HEAD", policy=None, forbidden=()):
        with self.assertRaises(self.exporter.ExportError) as caught:
            self.exporter.build_export(self.repo, self.output, revision, policy)
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))
        for value in forbidden:
            self.assertNotIn(value, str(caught.exception))
        return str(caught.exception)

    def test_committed_bytes_inventory_hashes_and_no_private_history(self):
        self.write(".env", "synthetic private value")
        self.write("_input/client.rvt", "DummyCustomer private model")
        self.write("_materials/client.pdf", "DummyCustomer private document")
        self.write("tests/test_sample.py", "DummyCustomer private integration")
        self.write("README.md", "DummyCustomer private notes")
        self.write("AGENTS.md", "DummyCustomer instructions")
        git(self.repo, "config", "user.email", "developer@" + "example.com")
        revision = self.seed()
        self.write("src/example.py", "uncommitted private material")
        self.inventory([{"source": "README.md", "export": "README.md"}])
        manifest = self.exporter.build_export(self.repo, self.output, revision)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["source_revision"], revision)
        self.assertEqual(manifest["provenance"], "sanitized_source_snapshot")
        content = b"print('public example')\n"
        self.assertEqual(manifest["files"], [{"path": "src/example.py", "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest()}])
        self.assertEqual((self.output / "src/example.py").read_bytes(), content)
        self.assertEqual(json.loads((self.output / "public-manifest.json").read_text()), manifest)
        with zipfile.ZipFile(self.output.with_suffix(".zip")) as archive:
            self.assertEqual(set(archive.namelist()), {"src/example.py", "public-manifest.json"})
            self.assertNotIn(b"DummyCustomer", b"".join(archive.read(n) for n in archive.namelist()))
        self.assertNotIn("Synthetic Author", json.dumps(manifest))

    def test_denied_inventory_and_path_collisions_fail_before_copy(self):
        cases = [".env", ".git/config", "_input/client.rvt", "_output/report.json",
                 "_materials/client.pdf", "tests/test_sample.py", "AGENTS.md", "CLAUDE.md",
                 "README.md", "docs/private.md", "model.fbx", "secret.pem",
                 "../outside.py", "/absolute.py", "a\\b.py"]
        for path in cases:
            with self.subTest(path=path):
                self.seed(entries=[{"source": path, "export": "safe.py"}])
                self.rejected()
        for target in ["../escape.py", ".env", ".git/config", "A.py", "a.py/child.py"]:
            with self.subTest(target=target):
                self.seed(entries=[{"source": "src/example.py", "export": "a.py"},
                                   {"source": "src/example.py", "export": target}])
                self.rejected()

    def test_git_symlinks_and_submodules_are_not_regular_source(self):
        self.seed()
        (self.repo / "src/link.py").symlink_to("example.py")
        self.inventory([{"source": "src/link.py", "export": "link.py"}])
        self.commit()
        self.rejected()
        revision = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "update-index", "--add", "--cacheinfo", "160000," + revision + ",src/module")
        self.inventory([{"source": "src/module", "export": "module.py"}])
        git(self.repo, "add", "--", "public/export-allowlist.json")
        git(self.repo, "commit", "-qm", "synthetic gitlink")
        self.rejected()

    def test_dirty_source_symlink_is_not_followed(self):
        self.seed()
        source = self.repo / "src/example.py"
        source.unlink()
        source.symlink_to(self.repo / ".git/config")
        self.exporter.build_export(self.repo, self.output, "HEAD")
        self.assertEqual((self.output / "src/example.py").read_text(), "print('public example')\n")

    def test_sensitive_content_and_policy_fail_without_echo(self):
        home = "/" + "Users" + "/dummy/private"
        email = "developer@" + "example.com"
        secret = "api_" + "key = '" + "SYNTHETIC_" * 4 + "'"
        key = "-----BEGIN " + "PRIVATE KEY-----"
        for content in [home, email, secret, key]:
            with self.subTest(kind=content.split()[0]):
                self.seed(content)
                self.rejected(forbidden=[content, email])
        self.write(".gitignore", "_output/\n")
        policy = self.repo / "_output/private-policy.json"
        self.write("_output/private-policy.json", json.dumps({"schema_version": 1,
            "denyTokens": ["DummyCustomer", "Fictional Avenue 123"]}))
        self.seed("DummyCustomer at Fictional Avenue 123")
        self.rejected(policy=policy, forbidden=["DummyCustomer", "Fictional Avenue 123"])

    def test_private_policy_must_be_ignored_and_never_exported(self):
        self.seed()
        policy = self.repo / "private-policy.json"
        self.write("private-policy.json", json.dumps({"schema_version": 1,
                                                     "denyTokens": ["DummyCustomer"]}))
        self.rejected(policy=policy)
        self.write(".gitignore", "_output/\n")
        self.write("_output/private-policy.json", policy.read_text())
        self.exporter.build_export(self.repo, self.output, "HEAD", self.repo / "_output/private-policy.json")
        self.assertEqual(set(p.name for p in self.output.iterdir()), {"src", "public-manifest.json"})

    def test_env_example_allows_only_safe_placeholders(self):
        entries = [{"source": ".env.example", "export": ".env.example"}]
        self.write(".env.example", "API_KEY=replace_me\nHOST=example.invalid\n")
        self.seed(entries=entries)
        self.exporter.build_export(self.repo, self.output, "HEAD")
        self.output = self.root / "unsafe-snapshot"
        token = "SYNTHETIC_" * 4
        self.write(".env.example", "API_" + "KEY=" + token)
        self.commit()
        self.rejected(forbidden=[token])

    def test_safe_generic_deny_words_and_synthetic_email_are_exportable(self):
        self.seed("_input _output .env .git developer@example.invalid\n"
                  "synthetic@users.noreply.github.com\n")
        self.exporter.build_export(self.repo, self.output, "HEAD")
        self.assertTrue((self.output / "src/example.py").exists())

    def test_invalid_revision_and_nonempty_or_unsafe_output(self):
        self.seed()
        for ref in ["--help", "unknown", "HEAD:src/example.py"]:
            self.rejected(revision=ref, forbidden=[ref])
        self.output.mkdir()
        (self.output / "existing").write_text("keep")
        with self.assertRaises(self.exporter.ExportError):
            self.exporter.build_export(self.repo, self.output, "HEAD")
        self.assertEqual((self.output / "existing").read_text(), "keep")
        self.output = self.repo / "published"
        self.rejected()
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.output = alias / "nested"
        self.rejected()
        self.output = self.repo / "_output/safe-snapshot"
        self.write(".gitignore", "_output/\n")
        self.exporter.build_export(self.repo, self.output, "HEAD")

    def test_archive_sibling_must_also_be_ignored_inside_source(self):
        self.seed()
        self.write(".gitignore", "published*\n!published.zip\n")
        self.output = self.repo / "published"
        self.rejected()

    def test_inventory_must_be_regular_blob_and_duplicate_targets_rejected(self):
        self.seed()
        inventory = self.repo / "public/export-allowlist.json"
        inventory.unlink()
        inventory.symlink_to("../src/example.py")
        self.commit()
        self.rejected()
        inventory.unlink()
        self.inventory([{"source": "src/example.py", "export": "a.py"},
                        {"source": "src/example.py", "export": "a.py"}])
        self.commit()
        self.rejected()

    def test_current_private_policy_is_never_exported_from_historical_commit(self):
        self.write("private-policy.json", "historical harmless public text")
        revision = self.seed(entries=[{"source": "private-policy.json", "export": "safe.py"}])
        git(self.repo, "rm", "--", "private-policy.json")
        self.write(".gitignore", "private-policy.json\n")
        self.commit()
        policy = self.repo / "private-policy.json"
        self.write("private-policy.json", json.dumps({"schema_version": 1,
                                                     "denyTokens": ["DummyCustomer"]}))
        self.rejected(revision=revision, policy=policy)

    def test_env_placeholder_rules_follow_export_target_case_insensitively(self):
        for index, target in enumerate([".env.example", ".ENV.EXAMPLE"]):
            with self.subTest(target=target):
                self.output = self.root / ("env-target-" + str(index))
                self.seed("PASSWORD=synthetic-short\n", entries=[
                    {"source": "src/example.py", "export": target}])
                self.rejected()

    def test_dotdot_external_alias_cannot_bypass_internal_policy_ignore(self):
        self.seed()
        self.write("private-policy.json", json.dumps({"schema_version": 1,
                                                     "denyTokens": ["DummyCustomer"]}))
        outside = self.root / "outside"
        outside.mkdir()
        alias = outside / ".." / "source" / "private-policy.json"
        self.rejected(policy=alias)

    def test_regular_external_policy_is_supported_without_export(self):
        self.seed()
        policy = self.root / "external-policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "denyTokens": ["DummyCustomer"]}))
        try:
            manifest = self.exporter.build_export(self.repo, self.output, "HEAD", policy)
        except self.exporter.ExportError as exc:
            self.fail("valid external policy rejected: " + str(exc))
        self.assertEqual([f["path"] for f in manifest["files"]], ["src/example.py"])
        self.assertFalse((self.output / "external-policy.json").exists())
        self.output = self.root / "policy-rejected"
        self.seed("DummyCustomer private content")
        self.rejected(policy=policy, forbidden=["DummyCustomer"])

    def test_image_suffixes_rejected_even_when_bytes_are_utf8(self):
        cases = [("source", ".png"), ("source", ".jpg"),
                 ("target", ".png"), ("target", ".jpg"),
                 ("target", ".PNG"), ("target", ".JPG")]
        for index, (direction, suffix) in enumerate(cases):
            with self.subTest(direction=direction, suffix=suffix):
                self.output = self.root / ("image-rejected-" + str(index))
                name = "src/example" + suffix
                self.write(name, "public-looking text")
                source = name if direction == "source" else "src/example.py"
                target = "safe.py" if direction == "source" else "example" + suffix
                self.seed(entries=[{"source": source, "export": target}])
                self.rejected()

    def test_self_contained_snapshot_reexports_after_safe_git_init(self):
        entries = [{"source": "scripts/export-public.py", "export": "scripts/export-public.py"},
                   {"source": "public/export-allowlist.json", "export": "public/export-allowlist.json"},
                   {"source": "docs/public/README.md", "export": "README.md"},
                   {"source": "docs/public/README.md", "export": "docs/public/README.md"},
                   {"source": "public/gitignore.template", "export": ".gitignore"},
                   {"source": "public/gitignore.template", "export": "public/gitignore.template"}]
        self.write("scripts/export-public.py", SCRIPT.read_text())
        self.write("docs/public/README.md", "Synthetic standalone source inspection\n")
        self.write("public/gitignore.template", "_output/\n__pycache__/\n")
        self.seed(entries=entries)
        first = self.exporter.build_export(self.repo, self.output, "HEAD")
        git(self.output, "init", "-q")
        git(self.output, "config", "user.name", "Synthetic Author")
        git(self.output, "config", "user.email", "synthetic@users.noreply.github.com")
        paths = [entry["path"] for entry in first["files"]] + ["public-manifest.json"]
        git(self.output, "add", "--", *paths)
        git(self.output, "commit", "-qm", "initial sanitized source")
        destination = self.root / "second"
        result = subprocess.run(["python3", str(self.output / "scripts/export-public.py"),
            "--revision", "HEAD", "--output", str(destination)], cwd=self.output,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        second = json.loads((destination / "public-manifest.json").read_text())
        self.assertEqual(first["files"], second["files"])
        self.assertNotEqual(first["source_revision"], second["source_revision"])
        for entry in first["files"]:
            self.assertEqual((self.output / entry["path"]).read_bytes(),
                             (destination / entry["path"]).read_bytes())
