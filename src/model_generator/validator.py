"""Independent package validation pipeline."""

import hashlib
from pathlib import PurePosixPath

from .archive import read_archive
from .diagnostics import Finding, Report
from .fbx_binary import parse_fbx
from .fbx_inspection import inspect_fbx
from .fbx_scene import read_scene
from .limits import Limits, ReadError
from .profiles import PROFILE, check_image

UNCHECKED = ["silhouette", "self_intersections", "normals", "uv_padding", "georeference",
             "design_correspondence", "pixel_decoding", "texture_density", "material_semantics",
             "ground_perimeter", "source_revit_fidelity", "current_regulatory_applicability"]


def validate_bytes(data, limits=None, observer=None):
    limits = limits or Limits()
    report = Report(input_sha256=hashlib.sha256(data).hexdigest(), profile=dict(PROFILE))
    if observer: observer("input.archive", "checking")
    archive = read_archive(data, limits)
    if observer: observer("input.archive", "passed", archive.findings)
    report.findings.extend(archive.findings)
    if archive.wrapped:
        report.findings.append(Finding("package.wrapper", "warn", "", True, None,
                                       "Outer ZIP is a transport wrapper, not submission evidence", "procedure"))
    entries = [entry for entry in archive.entries if entry.name.lower().endswith(".fbx")]
    report.findings.append(Finding("package.fbx_count", "pass" if 2 <= len(entries) <= 21 else "fail",
                                   "", len(entries), "2–21", "Research baseline for FBX file count", "profile"))
    ground = [entry for entry in entries if PurePosixPath(entry.name).stem.lower().endswith("_ground")]
    report.findings.append(Finding("package.ground", "warn" if len(ground) == 1 else "fail",
                                   "", len(ground), 1, "Ground identification uses filename heuristic", "profile"))
    if observer: observer("input.fbx", "checking")
    for entry in entries:
        _inspect_file(report, entry.name, entry.data, entry.sha256, limits, 180000 if entry in ground else 150000)
    if observer: observer("input.fbx", "passed" if entries else "not_checked", [f for f in report.findings if f.rule_id.startswith(("fbx.", "png.", "profile."))])
    return _close(report)


def _inspect_file(report, name, data, sha256, limits, budget, ceiling=None):
    """Append one FBX file; return its parsed tree, or None when unreadable.

    ceiling is the larger baseline of a role that the caller cannot rule out.
    """
    try:
        tree = parse_fbx(data, limits)
        measurements, findings = inspect_fbx(tree, name)
        measurements.update({"read_status": "read", "sha256": sha256, "bytes": len(data)})
        report.files.append(measurements)
        report.findings.extend(findings)
        report.findings.append(Finding("fbx.integrity", "pass", name, 7400, 7400,
                                       "Binary structure read successfully"))
        geometry_fail = any(f.status == "fail" and f.rule_id in {"fbx.geometry", "fbx.geometry_type", "fbx.triangulation", "fbx.objects"} for f in findings)
        report.findings.append(Finding("profile.triangles", "not_checked" if geometry_fail else
                                       "pass" if measurements["triangles"] <= budget else
                                       "warn" if ceiling and measurements["triangles"] <= ceiling else "fail",
                                       name, measurements["triangles"], budget,
                                       "Research triangle baseline uses filename classification", "profile"))
        for image in measurements["images"]:
            report.findings.extend(check_image(image, f"{name}#Video:{image['id']}"))
        return tree
    except ReadError as exc:
        status = "unsupported" if exc.rule == "fbx.unsupported" else "failed"
        report.files.append({"file": name, "sha256": sha256, "bytes": len(data), "read_status": status})
        report.findings.append(Finding(exc.rule, "fail", name, None, None, str(exc)))
        return None


def _close(report):
    for rule in UNCHECKED:
        report.findings.append(Finding(rule, "not_checked", "", None, None, "No validated algorithm or evidence", "procedure"))
    for rule in ["architecture_council_defence", "approval_for_vpm"]:
        report.findings.append(Finding(rule, "not_checked", "", None, None, "External action requires separate evidence", "external"))
    report.coverage = {"technical": "partial", "profile": "research", "procedure": "partial",
                       "external": "not_checked", "not_checked": UNCHECKED.copy()}
    return report


FBX_NAME = "input.fbx"


def validate_fbx_bytes(data, limits=None, observer=None):
    """Diagnose one standalone FBX. Its role is unknown: the ground range only warns."""
    limits = limits or Limits()
    sha256 = hashlib.sha256(data).hexdigest()
    report = Report(input_sha256=sha256, profile=dict(PROFILE))
    if observer: observer("input.fbx", "checking")
    tree = _inspect_file(report, FBX_NAME, data, sha256, limits, 150000, 180000)
    if tree is not None:
        try:
            scene = read_scene(tree, limits)
            report.findings.append(Finding("fbx.scene", "pass", FBX_NAME, {
                "meshes": len(scene.meshes), "triangles": sum(len(m.triangles) // 3 for m in scene.meshes),
                "unit_meters": scene.unit_meters, "bounds": [list(corner) for corner in scene.bounds]},
                None, "Placed geometry read in metres without recentering"))
        except ReadError as exc:
            # A reader limit is not a model defect; only a budget breach fails.
            report.findings.append(Finding("fbx.scene" if exc.rule == "fbx.scene_unsupported" else exc.rule,
                                           "not_checked" if exc.rule == "fbx.scene_unsupported" else "fail",
                                           FBX_NAME, None, None, str(exc)))
    if observer: observer("input.fbx", "passed", report.findings)
    return _close(report)


def _read_bounded(path, limits, rule):
    with open(path, "rb") as stream:
        # Seek before reading so an oversized regular file is never allocated.
        stream.seek(0, 2)
        size = stream.tell()
        if size > limits.input_bytes:
            raise ReadError(rule, "Input size exceeds budget")
        stream.seek(0)
        return stream.read(limits.input_bytes + 1)


def validate_path(path, limits=None, observer=None):
    limits = limits or Limits()
    return validate_bytes(_read_bounded(path, limits, "zip.budget"), limits, observer)


def validate_fbx_path(path, limits=None, observer=None):
    limits = limits or Limits()
    return validate_fbx_bytes(_read_bounded(path, limits, "fbx.budget"), limits, observer)
