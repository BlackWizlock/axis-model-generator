"""Adapt autonomous package inspection to the existing diagnostic report."""

import hashlib
import math
from pathlib import Path

from .diagnostics import Finding, Report
from .package_manifest import PackageError, PackageLimits
from .package_reader import read_package_bytes
from .package_scene import inspect_package_scene


def _safe_diagnostic(value):
    """Keep malformed values readable without breaking strict UTF-8 JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, str):
        return "".join("\ufffd" if 0xd800 <= ord(char) <= 0xdfff else char for char in value)
    if isinstance(value, dict):
        return {_safe_diagnostic(key): _safe_diagnostic(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_diagnostic(item) for item in value]
    return value


def _new_report(input_sha256=""):
    return Report(
        input_sha256=input_sha256,
        profile={"id": "portable-package-p0", "status": "research", "normative_readiness": False},
        coverage={"package_status": "failed", "capabilities": {}, "technical": "partial",
                  "profile": "research", "procedure": "unknown", "external": "not_checked",
                  "input_hash_status": "available" if input_sha256 else "not_available"},
    )


def _add_error(report, error):
    report.findings.append(Finding(
        _safe_diagnostic(error.rule), "fail", _safe_diagnostic(error.file),
        _safe_diagnostic({"value": error.actual, "element_key": error.element_key, "action": error.action}),
        _safe_diagnostic({"value": error.expected}),
        _safe_diagnostic(f"{error}. {error.action}"),
    ))
    report.coverage["package_status"] = "unsupported" if error.rule == "package.unsupported" else "failed"
    return report


def validate_package_bytes(data: bytes, limits: PackageLimits | None = None) -> Report:
    """Content failures are reports; structurally available is not source fidelity."""
    limits = limits or PackageLimits()
    report = _new_report(hashlib.sha256(data).hexdigest())
    try:
        package = read_package_bytes(data, limits)
        report.files = [{"file": item.path, "bytes": item.bytes, "sha256": item.sha256,
                         "role": item.role} for item in package.manifest.files]
        inspection = inspect_package_scene(package, limits)
        for item in report.files:
            if item["file"] == package.manifest.scene_path:
                item.update(inspection.measurements)
        report.findings.extend(inspection.findings)
        report.coverage.update({"package_status": "passed", "capabilities": inspection.capabilities})
    except PackageError as error:
        _add_error(report, error)
    return report


def validate_package_path(path: Path, limits: PackageLimits | None = None) -> Report:
    """Bound reads independently of ZIP metadata; leave OSError to the caller."""
    limits = limits or PackageLimits()
    with path.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        if size > limits.input_bytes:
            return _add_error(_new_report(), PackageError(
                "package.budget", "Input bytes exceed budget", actual=size, expected=limits.input_bytes))
        stream.seek(0)
        data = stream.read(limits.input_bytes + 1)
    # A file may grow after the size check. Do not hash a truncated prefix.
    if len(data) > limits.input_bytes:
        return _add_error(_new_report(), PackageError(
            "package.budget", "Input bytes exceed budget", actual=len(data), expected=limits.input_bytes))
    return validate_package_bytes(data, limits)
