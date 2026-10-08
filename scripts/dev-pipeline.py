#!/usr/bin/env python3
"""Local Docker check images and explicit SSH delivery, without runtime deployment."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile

PROJECT = "axis-model-generator"
PLATFORM = "linux/amd64"
LABEL = "com.axis.model-generator.project"
PURPOSE = "com.axis.model-generator.purpose"
DOCKERFILE_LABEL = "com.axis.model-generator.dockerfile-sha256"
REVISION_LABEL = "org.opencontainers.image.revision"
CORE_SUITE = ("from pathlib import Path; import unittest; suite=unittest.TestSuite(); "
              "[suite.addTests(unittest.defaultTestLoader.discover('tests',pattern=p.name)) "
              "for p in sorted(Path('tests').glob('test_*.py'))]; "
              "result=unittest.TextTestRunner(verbosity=1).run(suite); raise SystemExit(not result.wasSuccessful())")
TEST_COMMAND = ['python', '-c', CORE_SUITE]
IMAGE_FORMAT = ('{"id":{{json .Id}},"os":{{json .Os}},'
                '"architecture":{{json .Architecture}},"labels":{{json .Config.Labels}}}')
CONTAINER_FORMAT = ('{"name":{{json .Name}},"user":{{json .Config.User}},'
    '"labels":{{json .Config.Labels}},"privileged":{{json .HostConfig.Privileged}},'
    '"cap_add":{{json .HostConfig.CapAdd}},"cap_drop":{{json .HostConfig.CapDrop}},'
    '"security_opt":{{json .HostConfig.SecurityOpt}},"network_mode":{{json .HostConfig.NetworkMode}},'
    '"networks":{{json .NetworkSettings.Networks}},"ports":{{json .NetworkSettings.Ports}},'
    '"mounts":{{json .Mounts}},"pid_mode":{{json .HostConfig.PidMode}},'
    '"ipc_mode":{{json .HostConfig.IpcMode}},"devices":{{json .HostConfig.Devices}}}')


class PipelineError(ValueError):
    """Sanitized failure: do not echo command output, credentials or file contents."""

    def __init__(self, message, exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


def run(command, *, log=None, missing_container=None):
    if command[0] == "docker":
        # Explicit CLI context takes precedence over ambient Docker endpoint settings.
        command = ["docker", "--context", "default", *command[1:]]
    if log is not None:
        with Path(log).open("wb") as stream:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise PipelineError("command.failed: see local log", result.returncode)
        return b""
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        expected = {"Error response from daemon: No such container: " + str(missing_container),
                    "Error: No such container: " + str(missing_container)}
        if missing_container is not None and result.returncode == 1 and result.stderr.decode(errors="replace").strip() in expected:
            return None
        raise PipelineError("command.failed: requested operation failed")
    return result.stdout


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def no_symlinks(path):
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise PipelineError("path.symlink: symbolic path component refused")


def local_docker():
    endpoint = json.loads(run(["docker", "context", "inspect", "--format",
                              "{{json .Endpoints.docker.Host}}", "default"]))
    if not isinstance(endpoint, str) or not (
            re.fullmatch(r"unix:///[^\x00\r\n]+", endpoint) or
            endpoint in {"npipe:////./pipe/docker_engine", "npipe:////./pipe/dockerDesktopLinuxEngine"}):
        raise PipelineError("docker.local: remote Docker endpoint refused")
    driver = run(["docker", "buildx", "inspect", "default"]).decode()
    if not re.search(r"^Driver:[ \t]+docker$", driver, re.M) or re.findall(r"^Endpoint:[ \t]+(\S+)[ \t]*$", driver, re.M) != ["default"]:
        raise PipelineError("docker.builder: local Docker driver required")


def exporter():
    path = Path(__file__).with_name("export-public.py")
    spec = importlib.util.spec_from_file_location("public_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_context(repo, revision, output, private_policy=None):
    repo = Path(repo).resolve()
    output = Path(output).absolute()
    no_symlinks(output)
    output = output.resolve()
    if output.exists():
        raise PipelineError("output.new: output must not exist")
    if not isinstance(revision, str) or not revision or revision.startswith("-"):
        raise PipelineError("source.ref: committed revision required")
    try:
        relative = output.relative_to(repo)
    except ValueError:
        raise PipelineError("output.ignored: use a new ignored directory in this repository") from None
    if subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", str(relative)],
                      capture_output=True).returncode:
        raise PipelineError("output.ignored: use a new ignored directory in this repository")
    output.mkdir(parents=True)
    try:
        source = output / "context/source"
        exported = exporter().build_export(repo, source, revision, private_policy)
        dockerfile = source / "deploy/dev/Dockerfile.check"
        if not dockerfile.is_file():
            raise PipelineError("source.dockerfile: committed inventory must contain check Dockerfile")
        shutil.copyfile(dockerfile, output / "context/Dockerfile")
    except ValueError:
        raise PipelineError("source.inventory: committed public context refused") from None
    return exported["source_revision"]


def ssh_prefix(target):
    if not isinstance(target, str) or not re.fullmatch(
            r"(?:[a-zA-Z_][a-zA-Z0-9_-]*@)?[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", target):
        raise PipelineError("ssh.target: invalid explicit SSH target")
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "--", target]


def remote_command(target, command):
    return [*ssh_prefix(target), shlex.join(command)]


def inspect_image(image_id, target=None):
    command = ["docker", "image", "inspect", "--format", IMAGE_FORMAT, image_id]
    return json.loads(run(remote_command(target, command) if target else command))


def verify_image(image, manifest):
    labels = image.get("labels") or {}
    if (image.get("id") != manifest["image_id"] or image.get("os") != "linux" or
            image.get("architecture") != "amd64" or labels.get(LABEL) != PROJECT or
            labels.get(PURPOSE) != "check" or labels.get(REVISION_LABEL) != manifest["source_revision"] or
            labels.get(DOCKERFILE_LABEL) != manifest["dockerfile_sha256"]):
        raise PipelineError("image.identity: image ID, platform or source labels mismatch")


def gate(image_id, log):
    run(["docker", "run", "--rm", "--pull=never", "--platform=linux/amd64", "--read-only", "--network=none",
         "--user=10001:10001", "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
         "--pids-limit=256", "--memory=1g", "--cpus=2", "--tmpfs=/tmp:rw,nosuid,nodev,size=512m,mode=1777",
         image_id, *TEST_COMMAND], log=log)


def validate_manifest(manifest, directory):
    required = {"schema_version", "purpose", "source_revision", "platform", "tag", "image_id",
                "archive", "archive_sha256", "archive_bytes", "dockerfile_sha256", "status", "test_command"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise PipelineError("manifest.schema: exact schema 1 required")
    sha = manifest["source_revision"]
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1 or
            manifest["purpose"] != "check" or manifest["status"] != "verified" or
            manifest["platform"] != PLATFORM or manifest["test_command"] != TEST_COMMAND or
            not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or
            manifest["tag"] != PROJECT + "/check:" + sha or
            not isinstance(manifest["image_id"], str) or
            not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest["image_id"]) or
            any(not isinstance(manifest[key], str) or not re.fullmatch(r"[0-9a-f]{64}", manifest[key])
                for key in ["archive_sha256", "dockerfile_sha256"]) or
            type(manifest["archive_bytes"]) is not int or manifest["archive_bytes"] <= 0 or
            manifest["archive"] != "image.tar.gz"):
        raise PipelineError("manifest.values: invalid check image contract")
    archive = Path(directory).absolute() / manifest["archive"]
    no_symlinks(archive)
    if not archive.is_file() or archive.stat().st_size != manifest["archive_bytes"] or digest(archive) != manifest["archive_sha256"]:
        raise PipelineError("archive.integrity: archive size or digest mismatch")
    return archive


def verify_archive(archive, manifest):
    """Validate every tar path and the authoritative OCI graph before any SSH call."""
    try:
        files, payloads, seen = {}, {}, set()
        total_metadata = 0
        total_bytes = 0
        with tarfile.open(archive, "r|gz") as bundle:
            for member in bundle:
                name = member.name
                if (not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")) or
                        any(ord(c) < 32 or ord(c) == 127 for c in name) or "\\" in name or ":" in name or
                        name in seen or not (member.isfile() or member.isdir())):
                    raise PipelineError("archive.path: noncanonical, duplicate or linked member")
                seen.add(name)
                total_bytes += member.size
                if len(seen) > 4096 or total_bytes > 4294967296:
                    raise PipelineError("archive.bounds: archive exceeds check image limits")
                blob = re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", name)
                legacy = re.fullmatch(r"[0-9a-f]{64}(?:\.json|/(?:layer\.tar|VERSION|json))?", name)
                known = name in {"manifest.json", "index.json", "oci-layout", "repositories"}
                if member.isdir():
                    if name not in {"blobs", "blobs/sha256"} and not re.fullmatch(r"[0-9a-f]{64}", name):
                        raise PipelineError("archive.path: unexpected directory")
                    continue
                if not (blob or legacy or known) or member.size > 2147483648:
                    raise PipelineError("archive.member: unexpected or oversized member")
                stream = bundle.extractfile(member)
                if member.size <= 1048576:
                    total_metadata += member.size
                    if total_metadata > 16777216:
                        raise PipelineError("archive.metadata: oversized metadata")
                    data = stream.read()
                    payloads[name] = data
                    hashed = hashlib.sha256(data).hexdigest()
                else:
                    hashed = hashlib.file_digest(stream, "sha256").hexdigest()
                if blob and hashed != blob[1]:
                    raise PipelineError("archive.digest: blob digest mismatch")
                files[name] = (member.size, hashed)
        metadata = json.loads(payloads["manifest.json"])
        if not isinstance(metadata, list) or len(metadata) != 1:
            raise PipelineError("archive.metadata: exactly one check image required")
        entry = metadata[0]
        if entry.get("RepoTags") != [manifest["tag"]]:
            raise PipelineError("archive.tag: archive namespace mismatch")
        match = re.fullmatch(r"(?:blobs/sha256/)?([0-9a-f]{64})(?:\.json)?", entry["Config"])
        if not match:
            raise PipelineError("archive.config: invalid config path")
        config_id = "sha256:" + match[1]
        if files[entry["Config"]][1] != config_id[7:]:
            raise PipelineError("archive.config: config digest mismatch")
        image_id = manifest["image_id"]
        own_name = "docker.io/" + manifest["tag"]
        used_blobs = set()

        def annotations(descriptor):
            values = descriptor.get("annotations", {})
            if (not isinstance(values, dict) or
                    ("platform" in descriptor and descriptor["platform"] != {"os": "linux", "architecture": "amd64"}) or
                    values.get("io.containerd.image.name", own_name) != own_name or
                    values.get("org.opencontainers.image.ref.name", manifest["source_revision"])
                    not in {manifest["tag"], manifest["source_revision"]}):
                raise PipelineError("archive.tag: foreign OCI reference")

        def descriptor_blob(descriptor):
            annotations(descriptor)
            value = descriptor.get("digest")
            if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
                raise PipelineError("archive.descriptor: invalid digest")
            name = "blobs/sha256/" + value[7:]
            if name not in files or ("size" in descriptor and
                    (type(descriptor["size"]) is not int or descriptor["size"] != files[name][0])):
                raise PipelineError("archive.descriptor: missing blob or size mismatch")
            used_blobs.add(name)
            return name

        if "index.json" in files or "oci-layout" in files:
            if json.loads(payloads["oci-layout"]) != {"imageLayoutVersion": "1.0.0"}:
                raise PipelineError("archive.layout: unsupported OCI layout")
            top = json.loads(payloads["index.json"])
            descriptors = top.get("manifests", [])
            if top.get("schemaVersion") != 2 or not isinstance(descriptors, list) or len(descriptors) != 1:
                raise PipelineError("archive.index: exactly one OCI reference required")
            root = descriptors[0]
            if root.get("annotations", {}).get("io.containerd.image.name") != own_name:
                raise PipelineError("archive.tag: authoritative OCI tag mismatch")
            native_id = root["digest"]
            descriptor = root
            for depth in range(4):
                node = json.loads(payloads[descriptor_blob(descriptor)])
                if node.get("schemaVersion") != 2:
                    raise PipelineError("archive.graph: unsupported image metadata")
                if "manifests" not in node:
                    if node.get("mediaType", "application/vnd.oci.image.manifest.v1+json") not in {
                            "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}:
                        raise PipelineError("archive.graph: unsupported image manifest")
                    break
                if node.get("mediaType", "application/vnd.oci.image.index.v1+json") not in {
                        "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}:
                    raise PipelineError("archive.graph: unsupported image index")
                children = node["manifests"]
                if (not isinstance(children, list) or len(children) != 1 or
                        children[0].get("platform") != {"os": "linux", "architecture": "amd64"}):
                    raise PipelineError("archive.platform: exactly one Linux amd64 image required")
                descriptor = children[0]
            else:
                raise PipelineError("archive.graph: excessive index depth")
            if node.get("config", {}).get("digest") != config_id or image_id not in {native_id, config_id}:
                raise PipelineError("archive.image: native or legacy ID mismatch")
            if descriptor_blob(node["config"]) != entry["Config"]:
                raise PipelineError("archive.config: OCI and Docker config mismatch")
            layers = node.get("layers", [])
            if not isinstance(layers, list) or [descriptor_blob(layer) for layer in layers] != entry.get("Layers"):
                raise PipelineError("archive.layers: OCI and Docker layers mismatch")
            if {name for name in files if name.startswith("blobs/")} != used_blobs:
                raise PipelineError("archive.graph: unreferenced OCI blobs")
            if set(files) != used_blobs | {"index.json", "oci-layout", "manifest.json"}:
                raise PipelineError("archive.graph: unexpected OCI entries")
        else:
            if image_id != config_id or not isinstance(entry.get("Layers"), list):
                raise PipelineError("archive.image: legacy archive ID mismatch")
            if any(name not in files for name in entry["Layers"]):
                raise PipelineError("archive.layers: missing legacy layer")
            if "repositories" in files:
                repository = json.loads(payloads["repositories"])
                expected_repo, expected_tag = manifest["tag"].rsplit(":", 1)
                if (set(repository) != {expected_repo} or set(repository[expected_repo]) != {expected_tag}):
                    raise PipelineError("archive.repositories: foreign legacy reference")
        config = json.loads(payloads[entry["Config"]])
        verify_image({"id": image_id, "os": config.get("os"), "architecture": config.get("architecture"),
                      "labels": config.get("config", {}).get("Labels")}, manifest)
    except (OSError, tarfile.TarError, KeyError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError("archive.metadata: invalid Docker save archive") from None


def save_archive(tag, archive):
    with tempfile.TemporaryFile() as errors, Path(archive).open("wb") as destination:
        process = subprocess.Popen(["docker", "--context", "default", "save", tag], stdout=subprocess.PIPE, stderr=errors)
        try:
            with gzip.GzipFile(fileobj=destination, mode="wb", mtime=0) as compressed:
                shutil.copyfileobj(process.stdout, compressed)
        finally:
            process.stdout.close()
            code = process.wait()
        if code:
            raise PipelineError("archive.save: Docker save failed")


def build(repo, revision, output, *, archive=False, private_policy=None):
    local_docker()
    output = Path(output).absolute()
    sha = prepare_context(repo, revision, output, private_policy)
    dockerfile_hash = digest(output / "context/Dockerfile")
    tag = PROJECT + "/check:" + sha
    # Force the local engine's default builder, never a configured remote buildx runner.
    run(["docker", "buildx", "build", "--builder", "default", "--platform", PLATFORM, "--load", "--provenance=false", "--sbom=false",
         "--iidfile", str(output / "image-id"), "--label", LABEL + "=" + PROJECT,
         "--label", PURPOSE + "=check", "--label", REVISION_LABEL + "=" + sha,
         "--label", DOCKERFILE_LABEL + "=" + dockerfile_hash,
         "-f", str(output / "context/Dockerfile"), str(output / "context")], log=output / "build.log")
    image_id = (output / "image-id").read_text().strip()
    manifest = {"schema_version": 1, "purpose": "check", "source_revision": sha,
                "platform": PLATFORM, "tag": tag, "image_id": image_id,
                "dockerfile_sha256": dockerfile_hash, "test_command": TEST_COMMAND}
    verify_image(inspect_image(image_id), manifest)
    try:
        existing = inspect_image(tag)
    except PipelineError:
        existing = None
    if existing is not None and existing["id"] != image_id:
        raise PipelineError("image.immutable: committed tag already points to a different image")
    try:
        gate(image_id, output / "check.log")
    except PipelineError as exc:
        (output / "check-result.json").write_text(json.dumps({"exit_code": exc.exit_code, "status": "failed", "image_id": image_id}) + "\n")
        raise
    (output / "check-result.json").write_text(json.dumps({"exit_code": 0, "status": "passed", "coverage": "core-only", "web": "not_checked", "image_id": image_id}) + "\n")
    run(["docker", "tag", image_id, tag])
    if archive:
        path = output / "image.tar.gz"
        save_archive(tag, path)
        manifest.update(status="verified", archive=path.name, archive_bytes=path.stat().st_size,
                        archive_sha256=digest(path))
        verify_archive(path, manifest)
        (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return {"status": "verified" if archive else "passed", "source_revision": sha, "image_id": image_id}


def transfer(archive, target):
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(remote_command(target, ["docker", "load"]), stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=errors)
        try:
            with gzip.open(archive, "rb") as source:
                shutil.copyfileobj(source, process.stdin)
        except (OSError, EOFError):
            raise PipelineError("ship.transfer: archive transfer failed") from None
        finally:
            process.stdin.close()
            code = process.wait()
        if code:
            raise PipelineError("ship.load: remote Docker load failed")


def ship(manifest_path, target):
    ssh_prefix(target)
    manifest_path = Path(manifest_path).absolute()
    no_symlinks(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    archive = validate_manifest(manifest, manifest_path.parent)
    verify_image(inspect_image(manifest["image_id"]), manifest)
    # verified is only a recorded result; this gate is performed again on the exact ID.
    gate(manifest["image_id"], manifest_path.parent / "ship-check.log")
    verify_archive(archive, manifest)
    transfer(archive, target)
    verify_image(inspect_image(manifest["tag"], target), manifest)
    return {"status": "loaded", "purpose": "check", "image_id": manifest["image_id"],
            "source_revision": manifest["source_revision"], "runtime": "not_deployed"}


def resource_name(value):
    return isinstance(value, str) and re.fullmatch(r"model-generator_[a-z0-9][a-z0-9_-]*", value) is not None


def validate_policy(policy):
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "networks", "volumes"} or
            type(policy["schema_version"]) is not int or policy["schema_version"] != 1):
        raise PipelineError("isolation.policy: exact schema 1 required")
    for key in ["networks", "volumes"]:
        if not isinstance(policy[key], list) or any(not resource_name(name) for name in policy[key]):
            raise PipelineError("isolation.namespace: only exact own resource names permitted")


def evaluate_isolation(containers, policy):
    validate_policy(policy)
    result = {"status": "not_deployed" if not containers else "structural_pass",
              "connectivity": "not_verified", "storage_iam": "not_verified", "locality": "not_verified",
              "containers": len(containers), "violations": []}
    for index, container in enumerate(containers):
        bad = []
        user = container.get("user") or ""
        if not re.fullmatch(r"[1-9][0-9]*(?::[1-9][0-9]*)?", user):
            bad.append("nonroot")
        if (container.get("labels") or {}).get(LABEL) != PROJECT:
            bad.append("project_label")
        if container.get("privileged") or container.get("cap_add") or "ALL" not in (container.get("cap_drop") or []):
            bad.append("capabilities")
        if not any(option in {"no-new-privileges", "no-new-privileges:true"} for option in (container.get("security_opt") or [])):
            bad.append("no_new_privileges")
        if container.get("pid_mode") or container.get("ipc_mode") not in {"", "private", "none"} or container.get("devices"):
            bad.append("host_access")
        networks = container.get("networks") or []
        if container.get("network_mode") not in ["none", *policy["networks"]] or any(name not in policy["networks"] for name in networks):
            bad.append("networks")
        if any(bindings for bindings in (container.get("ports") or {}).values()):
            bad.append("host_ports")
        for mount in container.get("mounts") or []:
            if mount.get("Type") != "volume" or mount.get("Name") not in policy["volumes"]:
                bad.append("mounts")
        if bad:
            result["violations"].append({"container_index": index, "rules": sorted(set(bad))})
    if result["violations"]:
        result["status"] = "refused"
    return result


def inspect_isolation(names, policy, target=None):
    validate_policy(policy)
    if target:
        ssh_prefix(target)
    containers = []
    missing = 0
    for name in names:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name):
            raise PipelineError("isolation.container: invalid explicit container name")
        command = ["docker", "container", "inspect", "--format", CONTAINER_FORMAT, name]
        data = run(remote_command(target, command) if target else command, missing_container=name)
        if data is None:
            missing += 1
        else:
            containers.append(json.loads(data))
    result = evaluate_isolation(containers, policy)
    if result["status"] == "structural_pass":
        # Names alone cannot prove ownership of pre-existing networks or volumes.
        used = {"network": set(), "volume": set()}
        for container in containers:
            used["network"].update(container.get("networks") or [])
            used["volume"].update(mount["Name"] for mount in (container.get("mounts") or []))
        for kind, resources in used.items():
            for name in sorted(resources):
                template = ('{"labels":{{json .Labels}},"driver":{{json .Driver}},"options":{{json .Options}}}'
                            if kind == "volume" else "{{json .Labels}}")
                command = ["docker", kind, "inspect", "--format", template, name]
                resource = json.loads(run(remote_command(target, command) if target else command)) or {}
                labels = (resource.get("labels") or {}) if kind == "volume" else resource
                if kind == "volume" and (resource.get("driver") != "local" or resource.get("options") not in [None, {}]):
                    result["status"] = "refused"
                    result["violations"].append({"resource_type": kind, "rule": "driver_options"})
                if labels.get(LABEL) != PROJECT:
                    result["status"] = "refused"
                    result["violations"].append({"resource_type": kind, "rule": "project_label"})
    if missing:
        result["missing_containers"] = missing
        if result["status"] != "refused":
            result["status"] = "not_deployed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["check", "build"]:
        command = commands.add_parser(name)
        command.add_argument("--revision", required=True)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--private-policy", type=Path)
    command = commands.add_parser("ship")
    command.add_argument("--manifest", required=True, type=Path)
    command.add_argument("--ssh-target", required=True)
    command = commands.add_parser("inspect-isolation")
    command.add_argument("--container", action="append", default=[])
    command.add_argument("--policy", required=True, type=Path)
    command.add_argument("--ssh-target")
    args = parser.parse_args()
    try:
        if args.command in {"check", "build"}:
            result = build(Path(__file__).resolve().parents[1], args.revision, args.output,
                           archive=args.command == "build", private_policy=args.private_policy)
        elif args.command == "ship":
            local_docker()
            result = ship(args.manifest, args.ssh_target)
        else:
            if not args.ssh_target:
                local_docker()
            result = inspect_isolation(args.container, json.loads(args.policy.read_text()), args.ssh_target)
        print(json.dumps(result, sort_keys=True))
        return 1 if result["status"] == "refused" else 0
    except PipelineError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError):
        print("pipeline.failed: invalid input or unavailable operation", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
