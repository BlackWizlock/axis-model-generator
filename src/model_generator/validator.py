"""Independent package validation pipeline."""

import hashlib
from pathlib import PurePosixPath

from .archive import read_archive
from .diagnostics import Finding, Report
from .fbx_binary import parse_fbx
from .fbx_inspection import inspect_fbx
from .limits import Limits, ReadError
from .profiles import PROFILE, check_image

UNCHECKED = ["silhouette", "self_intersections", "normals", "uv_padding", "georeference",
             "design_correspondence", "pixel_decoding", "texture_density", "material_semantics",
             "ground_perimeter", "source_revit_fidelity", "current_regulatory_applicability"]


def validate_bytes(data, limits=None):
    limits = limits or Limits()
    report = Report(input_sha256=hashlib.sha256(data).hexdigest(), profile=dict(PROFILE))
    archive = read_archive(data, limits)
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
    for entry in entries:
        try:
            tree = parse_fbx(entry.data, limits)
            measurements, findings = inspect_fbx(tree, entry.name)
            measurements.update({"read_status": "read", "sha256": entry.sha256, "bytes": len(entry.data)})
            report.files.append(measurements)
            report.findings.extend(findings)
            report.findings.append(Finding("fbx.integrity", "pass", entry.name, 7400, 7400,
                                           "Binary structure read successfully"))
            budget = 180000 if entry in ground else 150000
            geometry_fail = any(f.status == "fail" and f.rule_id in {"fbx.geometry", "fbx.geometry_type", "fbx.triangulation", "fbx.objects"} for f in findings)
            report.findings.append(Finding("profile.triangles", "not_checked" if geometry_fail else
                                           "pass" if measurements["triangles"] <= budget else "fail",
                                           entry.name, measurements["triangles"], budget,
                                           "Research triangle baseline uses filename classification", "profile"))
            for image in measurements["images"]:
                image_filename = f"{entry.name}#Video:{image['id']}"
                report.findings.extend(check_image(image, image_filename))
        except ReadError as exc:
            status = "unsupported" if exc.rule == "fbx.unsupported" else "failed"
            report.files.append({"file": entry.name, "sha256": entry.sha256, "bytes": len(entry.data), "read_status": status})
            report.findings.append(Finding(exc.rule, "fail", entry.name, None, None, str(exc)))
    for rule in UNCHECKED:
        report.findings.append(Finding(rule, "not_checked", "", None, None, "No validated algorithm or evidence", "procedure"))
    for rule in ["architecture_council_defence", "approval_for_vpm"]:
        report.findings.append(Finding(rule, "not_checked", "", None, None, "External action requires separate evidence", "external"))
    report.coverage = {"technical": "partial", "profile": "research", "procedure": "partial",
                       "external": "not_checked", "not_checked": UNCHECKED.copy()}
    return report


def validate_path(path, limits=None):
    limits = limits or Limits()
    with open(path, "rb") as stream:
        # Seek before reading so an oversized regular file is never allocated.
        stream.seek(0, 2)
        size = stream.tell()
        if size > limits.input_bytes:
            raise ReadError("zip.budget", "Input size exceeds budget")
        stream.seek(0)
        data = stream.read(limits.input_bytes + 1)
    return validate_bytes(data, limits)
