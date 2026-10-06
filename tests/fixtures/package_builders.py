"""Synthetic portable packages; no model provenance or regulatory claims."""

import hashlib
import io
import json
import struct
import zipfile


IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def pack_array(dtype, values):
    """Encode flat or grouped synthetic values in the wire byte order."""
    flat = [item for value in values for item in value] if values and isinstance(values[0], (list, tuple)) else values
    return struct.pack("<" + {"float64": "d", "uint32": "I"}[dtype] * len(flat), *flat)


def make_scene():
    unknown = {"state": "unknown", "name": None, "zone": None, "method": "unknown", "transform": None, "parameters": {}}
    return {
        "schema_version": 1, "snapshot_id": "synthetic-snapshot", "root_document_id": "root",
        "documents": [{"document_id": "root", "revision": {"value": None, "method": "unknown", "saved_file_sha256": None, "unsaved_changes": False}}],
        "links": [], "omitted_links": [],
        "meshes": [{"mesh_id": "triangle", "vertices": "geometry/vertices.bin", "triangles": "geometry/triangles.bin", "vertex_count": 3, "triangle_count": 1, "normals": None, "uv": None, "material_ranges": [{"start_triangle": 0, "triangle_count": 1, "material_id": "plain"}]}],
        "instances": [{"instance_id": "instance", "mesh_id": "triangle", "element_key": {"document_id": "root", "link_instance_path": [], "unique_id": "synthetic-element"}, "transform": list(IDENTITY), "element_id": None}],
        "materials": [{"material_id": "plain", "status": "available", "source_parameters": {"red": 1.0}, "color_space": "srgb", "alpha_mode": "opaque", "textures": []}],
        "coordinates": {"units": "metre", "axes": "right-handed-z-up", "processing_origin": [0, 0, 0], "processing_to_project": list(IDENTITY), "shared": dict(unknown), "regional": dict(unknown), "vertical": dict(unknown), "control_points": [], "height_check": None},
        "ifc": None,
    }


def make_package(*, manifest_updates=None, scene_updates=None, member_overrides=None):
    """Produce an internally hashed ZIP; updates are shallow and explicit."""
    scene = make_scene()
    scene.update(scene_updates or {})
    members = {
        "scene.json": json.dumps(scene, ensure_ascii=False).encode("utf-8"),
        "geometry/vertices.bin": pack_array("float64", [(0, 0, 0), (1, 0, 0), (0, 1, 0)]),
        "geometry/triangles.bin": pack_array("uint32", [(0, 1, 2)]),
    }
    members.update(member_overrides or {})
    files = []
    for path, data in members.items():
        role = {"scene.json": "scene", "geometry/vertices.bin": "vertices", "geometry/triangles.bin": "triangles"}.get(path, "evidence")
        array = None
        if role in {"vertices", "triangles"}:
            array = {"dtype": "float64" if role == "vertices" else "uint32", "byte_order": "little", "count": 3 if role == "vertices" else 1, "components": 3, "stride_bytes": 24 if role == "vertices" else 12}
        files.append({"path": path, "role": role, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "array": array})
    manifest = {
        "package_version": {"major": 1, "minor": 0},
        "metadata": {"package_id": "synthetic-package", "snapshot_id": "synthetic-snapshot", "created_utc": "2026-10-06T00:00:00Z", "plugin_version": "synthetic-0.1", "revit": {"year": 2026, "build": "synthetic-build"}, "ifc_exporter": None, "selection": {"phase": "synthetic-phase", "design_option": "synthetic-option", "scope": "synthetic-scope", "included_link_instance_ids": []}},
        "required_capabilities": ["scene-v1", "geometry-f64-u32-v1"],
        "scene": {"path": "scene.json", "schema_version": 1}, "files": files,
        "capabilities": {name: {"state": "available" if name in {"geometry", "materials"} else "missing", "evidence": ["synthetic fixture"] if name in {"geometry", "materials"} else [], "limitations": []} for name in ("geometry", "materials", "uv", "normals", "links", "coordinates", "parameters", "ifc")},
    }
    manifest.update(manifest_updates or {})
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
            for path, data in members.items():
                archive.writestr(path, data)
        return output.getvalue()
