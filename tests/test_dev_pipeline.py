"""Local delivery boundaries; real Git fixtures, selective Docker/SSH adapters."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/dev-pipeline.py"


def load_pipeline():
    if not SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("dev_pipeline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = load_pipeline()

    def setUp(self):
        self.assertIsNotNone(self.p, "the local pipeline implementation must exist")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def manifest(self):
        archive = self.root / "image.tar.gz"
        archive.write_bytes(b"synthetic archive")
        return {"schema_version": 1, "purpose": "check", "source_revision": "a" * 40,
                "platform": "linux/amd64", "tag": "axis-model-generator/check:" + "a" * 40,
                "image_id": "sha256:" + "b" * 64, "archive": archive.name,
                "archive_sha256": self.p.digest(archive), "archive_bytes": archive.stat().st_size,
                "dockerfile_sha256": "c" * 64, "status": "verified",
                "test_command": self.p.TEST_COMMAND}

    def image(self):
        return {"id": "sha256:" + "b" * 64, "os": "linux", "architecture": "amd64",
                "labels": {"org.opencontainers.image.revision": "a" * 40,
                           "com.axis.model-generator.purpose": "check",
                           "com.axis.model-generator.project": "axis-model-generator",
                           "com.axis.model-generator.dockerfile-sha256": "c" * 64}}

    def policy(self):
        return {"schema_version": 1, "networks": ["model-generator_private"],
                "volumes": ["model-generator_models"]}

    def container(self):
        return {"name": "/model-generator-api", "user": "10001:10001",
                "labels": {"com.axis.model-generator.project": "axis-model-generator"},
                "privileged": False, "cap_add": [], "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"], "network_mode": "model-generator_private",
                "networks": ["model-generator_private"], "ports": {}, "mounts": [],
                "pid_mode": "", "ipc_mode": "private", "devices": []}

    def test_manifest_accepts_matching_archive_and_identity(self):
        m = self.manifest()
        self.assertEqual(self.p.validate_manifest(m, self.root), self.root / "image.tar.gz")
        self.p.verify_image(self.image(), m)

    def test_manifest_rejects_tampering_before_delivery(self):
        for key, value in [("schema_version", 2), ("purpose", "runtime"), ("status", "pending"),
                           ("platform", "linux/arm64"), ("tag", "axis-erp/api:latest"),
                           ("source_revision", "HEAD"), ("image_id", "latest"),
                           ("archive", "../image.tar.gz"), ("archive_bytes", True),
                           ("archive_sha256", "d" * 64), ("test_command", ["true"])]:
            with self.subTest(key=key):
                m = self.manifest()
                m[key] = value
                with self.assertRaises(self.p.PipelineError):
                    self.p.validate_manifest(m, self.root)
        m = self.manifest()
        (self.root / m["archive"]).write_bytes(b"changed")
        with self.assertRaises(self.p.PipelineError):
            self.p.validate_manifest(m, self.root)

    def test_archive_symlink_is_rejected(self):
        m = self.manifest()
        target = self.root / m["archive"]
        target.rename(self.root / "actual")
        target.symlink_to(self.root / "actual")
        with self.assertRaises(self.p.PipelineError):
            self.p.validate_manifest(m, self.root)

    def test_image_mismatched_id_source_platform_or_label_is_rejected(self):
        m = self.manifest()
        for key, value in [("id", "sha256:" + "d" * 64), ("os", "windows"),
                           ("architecture", "arm64"), ("labels", {})]:
            image = self.image()
            image[key] = value
            with self.subTest(key=key), self.assertRaises(self.p.PipelineError):
                self.p.verify_image(image, m)

    def test_ssh_target_refuses_options_shell_injection_and_unvalidated_aliases(self):
        for target in ["-oProxyCommand=x", "host;id", "host$(id)", "host\ntrue", "x y", "x@y@z", ""]:
            with self.subTest(target=target), self.assertRaises(self.p.PipelineError):
                self.p.ssh_prefix(target)
        self.assertEqual(self.p.ssh_prefix("deploy@server.example.invalid")[-1],
                         "deploy@server.example.invalid")
        self.assertIn("StrictHostKeyChecking=yes", self.p.ssh_prefix("server"))

    def test_context_uses_committed_inventory_not_untracked_files_or_symlinks(self):
        repo = self.root / "repo"
        repo.mkdir()
        entries = [{"source": "safe.py", "export": "safe.py"},
                   {"source": "deploy/dev/Dockerfile.check", "export": "deploy/dev/Dockerfile.check"}]
        for path, content in {".gitignore": "_output/\n", "safe.py": "original",
                              "public/export-allowlist.json": json.dumps({"schema_version": 1, "files": entries}),
                              "deploy/dev/Dockerfile.check": "FROM scratch\n"}.items():
            file = repo / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
        for args in [("init", "-q"), ("config", "user.name", "Synthetic Author"),
                     ("config", "user.email", "synthetic@example.invalid"),
                     ("add", "--", ".gitignore", "safe.py", "public/export-allowlist.json", "deploy/dev/Dockerfile.check"),
                     ("commit", "-qm", "fixture")]:
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        (repo / "safe.py").write_text("dirty")
        (repo / "private.py").write_text("private")
        output = repo / "_output/check"
        commit = self.p.prepare_context(repo, "HEAD", output)
        self.assertEqual(len(commit), 40)
        self.assertEqual((output / "context/source/safe.py").read_text(), "original")
        self.assertFalse((output / "context/source/private.py").exists())
        self.assertFalse((output / "context/source/.git").exists())
        with self.assertRaises(self.p.PipelineError):
            self.p.prepare_context(repo, "--help", repo / "_output/bad")
        with self.assertRaises(self.p.PipelineError):
            self.p.prepare_context(repo, "HEAD", repo / "unignored")
        with self.assertRaises(self.p.PipelineError):
            self.p.prepare_context(repo, "HEAD", output)

    def test_gate_uses_image_id_and_denies_ambient_privilege(self):
        with patch.object(self.p, "run", return_value=b"") as run:
            self.p.gate("sha256:" + "b" * 64, self.root / "gate.log")
        command = run.call_args.args[0]
        for option in ["--read-only", "--network=none", "--cap-drop=ALL", "--user=10001:10001",
                       "--security-opt=no-new-privileges:true", "--pids-limit=256"]:
            self.assertIn(option, command)
        self.assertIn("sha256:" + "b" * 64, command)
        self.assertFalse(any("socket" in arg for arg in command))

    def test_failed_recheck_prevents_ssh_transfer(self):
        m = self.manifest()
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(m))
        with patch.object(self.p, "inspect_image", return_value=self.image()), \
             patch.object(self.p, "gate", side_effect=self.p.PipelineError("failed")), \
             patch.object(self.p, "transfer") as transfer:
            with self.assertRaises(self.p.PipelineError):
                self.p.ship(manifest, "server.example.invalid")
            transfer.assert_not_called()

    def archived_image(self, manifest, *, tag=None, corrupt=False):
        config = json.dumps({"os": "linux", "architecture": "amd64",
                             "config": {"Labels": self.image()["labels"]}}).encode()
        import hashlib
        config_sha = hashlib.sha256(config).hexdigest()
        manifest["image_id"] = "sha256:" + config_sha
        config_name = "blobs/sha256/" + config_sha
        archive = self.root / "image.tar.gz"
        metadata = [{"Config": config_name, "RepoTags": [tag or manifest["tag"]], "Layers": []}]
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in [(config_name, b"changed" if corrupt else config),
                               ("manifest.json", json.dumps(metadata).encode())]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
        return archive

    def test_archive_metadata_is_bound_to_tested_image_and_own_tag(self):
        m = self.manifest()
        self.p.verify_archive(self.archived_image(m), m)
        with self.assertRaises(self.p.PipelineError):
            self.p.verify_archive(self.archived_image(m, tag="axis-erp/api:latest"), m)
        with self.assertRaises(self.p.PipelineError):
            self.p.verify_archive(self.archived_image(m, corrupt=True), m)

    def oci_archive(self, m, *, native_id=True, extra_reference=False, extra_platform=False):
        import hashlib
        archive = self.archived_image(m)
        config_id = m["image_id"]
        with tarfile.open(archive, "r:gz") as bundle:
            entries = [(member.name, bundle.extractfile(member).read()) for member in bundle]
        image_manifest = json.dumps({"schemaVersion": 2, "config": {"digest": config_id}}).encode()
        manifest_id = "sha256:" + hashlib.sha256(image_manifest).hexdigest()
        children = [{"digest": manifest_id, "platform": {"os": "linux", "architecture": "amd64"}}]
        if extra_platform:
            children.append({"digest": manifest_id, "platform": {"os": "linux", "architecture": "arm64"}})
        index = json.dumps({"schemaVersion": 2, "manifests": children}).encode()
        index_id = "sha256:" + hashlib.sha256(index).hexdigest()
        if native_id:
            m["image_id"] = index_id
        descriptors = [{"digest": index_id,
            "annotations": {"io.containerd.image.name": "docker.io/" + m["tag"]}}]
        if extra_reference:
            descriptors.append({"digest": index_id,
                "annotations": {"io.containerd.image.name": "docker.io/axis-erp/api:latest"}})
        top_index = json.dumps({"schemaVersion": 2, "manifests": descriptors}).encode()
        entries.extend([("blobs/sha256/" + manifest_id[7:], image_manifest),
                        ("blobs/sha256/" + index_id[7:], index), ("index.json", top_index),
                        ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}')])
        with tarfile.open(archive, "w:gz") as bundle:
            for name, data in entries:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                bundle.addfile(member, io.BytesIO(data))
        return archive

    def test_containerd_archive_binds_native_oci_index_id_to_config(self):
        m = self.manifest()
        archive = self.oci_archive(m)
        self.p.verify_archive(archive, m)

    def test_oci_foreign_references_are_rejected_for_both_native_and_legacy_ids(self):
        for native in [False, True]:
            m = self.manifest()
            archive = self.oci_archive(m, native_id=native, extra_reference=True)
            with self.subTest(native=native), self.assertRaises(self.p.PipelineError):
                self.p.verify_archive(archive, m)

    def test_oci_extra_platform_is_rejected_for_both_native_and_legacy_ids(self):
        for native in [False, True]:
            m = self.manifest()
            archive = self.oci_archive(m, native_id=native, extra_platform=True)
            with self.subTest(native=native), self.assertRaises(self.p.PipelineError):
                self.p.verify_archive(archive, m)

    def test_archive_path_aliases_duplicate_entries_and_links_are_rejected(self):
        for path, kind in [("extra/../index.json", tarfile.REGTYPE), ("./index.json", tarfile.REGTYPE),
                           ("index.json", tarfile.REGTYPE), ("linked", tarfile.SYMTYPE),
                           ("linked", tarfile.LNKTYPE), ("/index.json", tarfile.REGTYPE)]:
            m = self.manifest()
            archive = self.oci_archive(m)
            with tarfile.open(archive, "r:gz") as source:
                entries = [(member.name, source.extractfile(member).read()) for member in source]
            with tarfile.open(archive, "w:gz") as target:
                for name, data in entries:
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    target.addfile(member, io.BytesIO(data))
                member = tarfile.TarInfo(path)
                member.type = kind
                member.linkname = "index.json" if kind != tarfile.REGTYPE else ""
                data = b'{"schemaVersion":2,"manifests":[]}'
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                target.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
            with self.subTest(path=path, kind=kind), self.assertRaises(self.p.PipelineError):
                self.p.verify_archive(archive, m)

    def test_volume_driver_options_cannot_hide_a_host_bind_or_remote_plugin(self):
        container = self.container()
        container["mounts"] = [{"Type": "volume", "Name": "model-generator_models", "Destination": "/data"}]
        for driver, options in [("local", {"type": "none", "device": "/srv/foreign", "o": "bind"}),
                                ("remote-plugin", {}), ("local", {"type": "nfs"})]:
            def adapter(command, **kwargs):
                if "container" in command:
                    return json.dumps(container).encode()
                if "volume" in command and any(".Options" in item for item in command):
                    return json.dumps({"labels": {self.p.LABEL: self.p.PROJECT}, "driver": driver,
                                       "options": options}).encode()
                return json.dumps({self.p.LABEL: self.p.PROJECT}).encode()
            with patch.object(self.p, "run", side_effect=adapter):
                result = self.p.inspect_isolation(["model-generator-api"], self.policy())
            with self.subTest(driver=driver, options=options):
                self.assertEqual(result["status"], "refused")

    def test_reviewed_local_volume_with_no_driver_options_is_allowed(self):
        container = self.container()
        container["mounts"] = [{"Type": "volume", "Name": "model-generator_models", "Destination": "/data"}]
        with patch.object(self.p, "run", side_effect=[json.dumps(container).encode(),
                json.dumps({self.p.LABEL: self.p.PROJECT}).encode(),
                json.dumps({"labels": {self.p.LABEL: self.p.PROJECT}, "driver": "local", "options": {}}).encode()]):
            result = self.p.inspect_isolation(["model-generator-api"], self.policy())
        self.assertEqual(result["status"], "structural_pass")

    def test_docker_named_pipe_and_builder_node_cannot_address_remote_machine(self):
        for endpoint, builder in [(b'"npipe:////remote-machine/pipe/docker_engine"', b'Driver: docker\nEndpoint: default\n'),
                                   (b'"unix:///var/run/docker.sock"', b'Driver: docker\nEndpoint: remote-alias\n')]:
            with patch.object(self.p, "run", side_effect=[endpoint, builder]), self.assertRaises(self.p.PipelineError):
                self.p.local_docker()

    def test_missing_container_is_not_deployed_but_daemon_failure_remains_error(self):
        completed = subprocess.CompletedProcess([], 1, b'', b'Error response from daemon: No such container: model-generator-api\n')
        with patch.object(self.p.subprocess, "run", return_value=completed):
            result = self.p.inspect_isolation(["model-generator-api"], self.policy())
        self.assertEqual(result["status"], "not_deployed")
        failure = subprocess.CompletedProcess([], 1, b'', b'Cannot connect to the Docker daemon')
        with patch.object(self.p.subprocess, "run", return_value=failure), self.assertRaises(self.p.PipelineError):
            self.p.inspect_isolation(["model-generator-api"], self.policy())

    def test_selective_inspection_does_not_request_environment(self):
        with patch.object(self.p, "run", return_value=json.dumps(self.container()).encode()) as run:
            result = self.p.inspect_isolation(["model-generator-api"], self.policy())
        self.assertEqual(result["status"], "refused", "resource labels must also be checked")
        self.assertFalse(any(".Env" in arg for call in run.call_args_list for arg in call.args[0]))
        with self.assertRaises(self.p.PipelineError):
            self.p.inspect_isolation(["--help"], self.policy())

    def test_local_docker_refuses_remote_engine_and_remote_builder(self):
        with patch.object(self.p, "run", return_value=b'"tcp://remote:2375"'):
            with self.assertRaises(self.p.PipelineError):
                self.p.local_docker()
        with patch.object(self.p, "run", side_effect=[b'"unix:///var/run/docker.sock"', b'Driver: remote\n']):
            with self.assertRaises(self.p.PipelineError):
                self.p.local_docker()

    def test_check_command_failure_records_actual_exit_status(self):
        with self.assertRaises(self.p.PipelineError) as caught:
            self.p.run(["python", "-c", "raise SystemExit(7)"], log=self.root / "failure.log")
        self.assertEqual(caught.exception.exit_code, 7)

    def test_isolation_no_containers_is_not_deployed(self):
        result = self.p.evaluate_isolation([], self.policy())
        self.assertEqual(result["status"], "not_deployed")
        self.assertEqual(result["connectivity"], "not_verified")

    def test_valid_structural_isolation_retains_missing_connectivity_evidence(self):
        result = self.p.evaluate_isolation([self.container()], self.policy())
        self.assertEqual(result["status"], "structural_pass")
        self.assertEqual(result["connectivity"], "not_verified")
        self.assertEqual(result["storage_iam"], "not_verified")
        self.assertEqual(result["locality"], "not_verified")

    def test_policy_cannot_allow_axis_resources_wildcards_or_defaults(self):
        for key in ["networks", "volumes"]:
            for value in ["axis_private", "axis_platform_private", "axis_tenant_x", "axis_client_models",
                          "*", "model-generator_*", "default", "bridge", "model-generator"]:
                policy = self.policy()
                policy[key] = [value]
                with self.subTest(key=key, value=value), self.assertRaises(self.p.PipelineError):
                    self.p.evaluate_isolation([], policy)

    def test_container_refuses_root_capabilities_host_connections_and_foreign_storage(self):
        violations = [("user", "root"), ("user", "0:10001"), ("user", ""), ("privileged", True),
                      ("labels", {}), ("cap_drop", []), ("cap_add", ["SYS_ADMIN"]),
                      ("security_opt", []), ("network_mode", "host"), ("pid_mode", "host"),
                      ("ipc_mode", "host"), ("devices", [{}]),
                      ("networks", ["axis_private"]), ("ports", {"8000/tcp": [{"HostPort": "8000"}]}),
                      ("mounts", [{"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/socket"}]),
                      ("mounts", [{"Type": "volume", "Name": "axis_models", "Destination": "/data"}])]
        for key, value in violations:
            container = self.container()
            container[key] = value
            result = self.p.evaluate_isolation([container], self.policy())
            with self.subTest(key=key):
                self.assertEqual(result["status"], "refused")
