#!/usr/bin/env python3
"""Create a reviewed, explicit source snapshot without copying Git history."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

INVENTORY = "public/export-allowlist.json"
MANIFEST = "public-manifest.json"
DENIED_PARTS = {".git", ".claude", ".codex", ".agents", ".worktrees", "_input",
                "_output", "_materials", "_prd", "_standards", "_vault", "node_modules",
                "__pycache__", ".venv"}
DENIED_NAMES = {"agents.md", "claude.md", "test_sample.py", ".mcp.json", MANIFEST}
DENIED_SUFFIXES = {".rvt", ".rfa", ".fbx", ".ifc", ".blend", ".pdf", ".zip", ".pem",
                   ".key", ".p12", ".pfx", ".pyc", ".png", ".jpg", ".ico"}
EMAIL = re.compile(r"[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
HOME_PATH = re.compile(r"(?:/(?:Users|home)/[A-Za-z0-9_.-]+(?:/|\b)|"
                       r"[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][A-Za-z0-9_.-]+)", re.I)
KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
                   r"AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9_-]{20,})\b")
ASSIGNMENT = re.compile(r"\b(?:password|secret|token|api[_-]?key|access[_-]?key)\b"
                        r"\s*[=:]\s*[\"']?([A-Za-z0-9_+/=-]{20,})", re.I)


PUBLIC_CONTACT_PATHS={'scripts/export-public.py','web/index.html','web/privacy.html','web/support.html','web/analytics-consent.html','tests/web/test_static.py','web/tests/support/analytics-cases.mjs'}
PUBLIC_CONTACT='info@axisconsult.ru'

PUBLIC_ASSETS = {'web/favicon.ico': (10635, '6879e820d351f2bc765eba1eb560e74f0235500be18e54cf7b29313a2e6cc553'), 'web/assets/axis-sign.png': (68273, '7d845af40aea19f8ff48ee35c3d06ccbe74a7802ca66c37f85d18d7a9ec61729'), 'web/assets/fonts/manrope-latin-wght-normal.woff2': (24836, 'a30ddcd349703aff7464c34bef3fffdff405ee50c113440d7c8693c02d210972'), 'web/assets/fonts/manrope-cyrillic-wght-normal.woff2': (14500, 'c268b459a9329e59fecf39a17618efd44c71735532048d60b12aab76a8c14914'), 'web/assets/fonts/geologica-latin-700-normal.woff2': (14412, '7f7f79c5a8bcfdae1dffc5b90dc49fd145951a50e2fecb92f97b93be6c7d5bfa'), 'web/assets/fonts/geologica-cyrillic-700-normal.woff2': (9164, '646d6acf1b000c0c63d36bcebd7a14379220bf559ed9152c58eb89ee2a12b13a'), 'web/assets/fonts/geologica-latin-800-normal.woff2': (14464, '482d9d7848a9bf11d7a6a7e0ef2e2a2cc833253f8cf43ca2738ef438441561f3'), 'web/assets/fonts/geologica-cyrillic-800-normal.woff2': (9120, '94eebb38e423bfd55b111a58753493a38c79a873f73b8c5cf5babdbf67e7500b')}


class ExportError(ValueError):
    """Safe diagnostic which never includes a matched sensitive value."""


def _git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        # Git stderr can echo arbitrary revision or file content; never forward it.
        raise ExportError("git.object: cannot read requested committed source")
    return result.stdout


def _safe_path(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\\:\x00*?[]"):
        raise ExportError("inventory.path: invalid relative path")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ExportError("inventory.path: invalid relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ExportError("inventory.path: invalid relative path")
    if unicodedata.normalize("NFC", value) != value:
        raise ExportError("inventory.path: noncanonical path")
    return value


def _permitted(path, source=False):
    parts = PurePosixPath(path).parts
    lower = [p.casefold() for p in parts]
    name = lower[-1]
    if any(p in DENIED_PARTS for p in lower) or name in DENIED_NAMES:
        raise ExportError("inventory.denied: forbidden source category")
    if any(p.startswith(".env") and p != ".env.example" for p in lower):
        raise ExportError("inventory.denied: environment file")
    if PurePosixPath(name).suffix in DENIED_SUFFIXES and path not in PUBLIC_ASSETS:
        raise ExportError("inventory.denied: private or binary resource")
    if source and (path == "README.md" or (parts[0] == "docs" and parts[:2] != ("docs", "public"))):
        raise ExportError("inventory.denied: private documentation")


def _blob(repo, revision, path):
    entry = _git(repo, "ls-tree", "-z", revision, "--", path).split(b"\0")
    if len(entry) != 2 or not entry[0]:
        raise ExportError("git.regular: inventory file missing or ambiguous")
    metadata, actual_path = entry[0].split(b"\t", 1)
    mode, kind, _ = metadata.split()
    if mode not in {b"100644", b"100755"} or kind != b"blob" or actual_path != path.encode():
        raise ExportError("git.regular: inventory source must be a regular file")
    return _git(repo, "show", revision + ":" + path)


def _no_symlinks(path):
    for current in [path, *path.parents]:
        if current.is_symlink():
            raise ExportError("output.symlink: symbolic path component")


def _ignored(repo, path):
    try:
        relative = path.relative_to(repo)
    except ValueError:
        return False
    result = subprocess.run(["git", "-C", str(repo), "check-ignore", "-q", "--", str(relative)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode == 0


def _policy(repo, path, output):
    if path is None:
        return []
    path = Path(path).absolute()
    _no_symlinks(path)
    path = path.resolve()
    if not path.is_file():
        raise ExportError("policy.regular: private policy must be a regular file")
    if repo in path.parents and not _ignored(repo, path):
        raise ExportError("policy.ignored: in-repository private policy must be ignored")
    if path == output or output in path.parents:
        raise ExportError("policy.output: private policy overlaps snapshot")
    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ExportError("policy.schema: invalid private policy") from None
    if (not isinstance(policy, dict) or policy.get("schema_version") != 1 or
            not isinstance(policy.get("denyTokens"), list) or
            any(not isinstance(t, str) or not t.strip() for t in policy["denyTokens"])):
        raise ExportError("policy.schema: invalid private policy")
    return [unicodedata.normalize("NFKC", t).casefold() for t in policy["denyTokens"]]


def _scan(data, path, deny_tokens):
    if path in PUBLIC_ASSETS:
        size,sha256=PUBLIC_ASSETS[path]
        if len(data)!=size or hashlib.sha256(data).hexdigest()!=sha256:
            raise ExportError('content.public_asset: reviewed binary checksum mismatch')
        return
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise ExportError("content.utf8: non-text source") from None
    normalized = unicodedata.normalize("NFKC", text).casefold()
    if any(t in normalized for t in deny_tokens):
        raise ExportError("content.private_policy: prohibited private content")
    for rule, pattern in [("home_path", HOME_PATH), ("private_key", KEY),
                          ("token", TOKEN), ("secret_assignment", ASSIGNMENT)]:
        if pattern.search(text):
            raise ExportError("content." + rule + ": sensitive content")
    for match in EMAIL.finditer(text):
        if match.group().casefold()==PUBLIC_CONTACT and path in PUBLIC_CONTACT_PATHS:continue
        domain = match.group().rsplit("@", 1)[1].casefold()
        if not (domain == "example.invalid" or domain.endswith(".invalid") or
                domain in {"users.noreply.github.com", "noreply.github.com"}):
            raise ExportError("content.email: personal email")
    if PurePosixPath(path).name.casefold() == ".env.example":
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "=" not in line:
                raise ExportError("content.env_example: expected placeholder assignment")
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value not in {"", "replace_me", "placeholder", "example.invalid", "localhost"}:
                raise ExportError("content.env_example: expected placeholder value")


def build_export(repo_root: Path, output: Path, revision: str,
                 private_policy: Path | None = None) -> dict:
    repo = Path(repo_root).resolve()
    output = Path(output).absolute()
    _no_symlinks(output)
    output = output.resolve()
    archive = output.with_suffix(".zip")
    _no_symlinks(archive)
    if output == repo or (repo in output.parents and not _ignored(repo, output)):
        raise ExportError("output.source: output inside unignored source tree")
    if repo in archive.parents and not _ignored(repo, archive):
        raise ExportError("output.archive: archive inside unignored source tree")
    if output in repo.parents:
        raise ExportError("output.source: output contains source repository")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ExportError("output.empty: output must be an empty directory")
    if archive.exists() or archive == output:
        raise ExportError("output.archive: archive destination must be unused")
    if not isinstance(revision, str) or not revision:
        raise ExportError("git.revision: invalid revision")
    commit = _git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ExportError("git.revision: invalid commit object")
    deny_tokens = _policy(repo, private_policy, output)
    policy_path = Path(private_policy).resolve() if private_policy is not None else None
    inventory_data = _blob(repo, commit, INVENTORY)
    _scan(inventory_data, INVENTORY, deny_tokens)
    try:
        inventory = json.loads(inventory_data)
    except ValueError:
        raise ExportError("inventory.schema: invalid inventory") from None
    if (not isinstance(inventory, dict) or inventory.get("schema_version") != 1 or
            not isinstance(inventory.get("files"), list) or not inventory["files"]):
        raise ExportError("inventory.schema: expected schema 1 with explicit files")
    payloads = {}
    modes = {}
    targets = set()
    for entry in inventory["files"]:
        if not isinstance(entry, dict) or set(entry) != {"source", "export"}:
            raise ExportError("inventory.schema: expected source and export paths")
        source, target = _safe_path(entry["source"]), _safe_path(entry["export"])
        if policy_path is not None and (repo / source).resolve() == policy_path:
            raise ExportError("policy.source: private policy cannot be exported")
        _permitted(source, source=True)
        _permitted(target)
        key = target.casefold()
        if any(key == t or key.startswith(t + "/") or t.startswith(key + "/") for t in targets):
            raise ExportError("inventory.collision: duplicate or overlapping export target")
        targets.add(key)
        data = _blob(repo, commit, source)
        _scan(data, source, deny_tokens)
        _scan(data, target, deny_tokens)
        payloads[target] = data
        modes[target]=int(_git(repo,"ls-tree",commit,"--",source).split()[0][-3:],8)
    files = [{"path": path, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for path, data in sorted(payloads.items())]
    manifest = {"schema_version": 1, "source_revision": commit,
                "provenance": "sanitized_source_snapshot", "files": files}
    payloads[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    # All inventory and content checks completed before creating any exported bytes.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".public-export-", dir=output.parent) as staging:
        staged = Path(staging) / "snapshot"
        staged.mkdir()
        for path, data in sorted(payloads.items()):
            destination = staged / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            destination.chmod(modes.get(path,0o644))
        staged_archive = Path(staging) / "snapshot.zip"
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path, data in sorted(payloads.items()):
                info = zipfile.ZipInfo(path, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100000 | modes.get(path,0o644)) << 16
                bundle.writestr(info, data)
        if output.exists():
            output.rmdir()
        staged.rename(output)
        try:
            staged_archive.rename(archive)
        except OSError:
            shutil.rmtree(output)
            raise ExportError("output.write: archive could not be saved") from None
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--private-policy", type=Path)
    args = parser.parse_args()
    try:
        manifest = build_export(Path(__file__).resolve().parents[1], args.output,
                                args.revision, args.private_policy)
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print("export.failure: source or output operation failed", file=sys.stderr)
        return 1
    print(json.dumps({"source_revision": manifest["source_revision"], "files": len(manifest["files"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
