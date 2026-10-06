"""Measurements from parsed FBX, without modifying geometry or coordinates."""

from array import array
from collections import Counter
import math

from .diagnostics import Finding
from .limits import ReadError
from .png_inspection import inspect_png


def inspect_fbx(roots, filename):
    findings = []
    result = {"file": filename, "version": 7400, "counts": {}, "triangles": 0,
              "polygons": 0, "unit_meters": None, "models": [], "images": [], "textures": []}

    def finding(rule, status, actual, expected, message):
        findings.append(Finding(rule, status, filename, actual, expected, message))

    def root(name):
        return next((n for n in roots if n.name == name), None)

    def properties(node):
        container = node.child("Properties70") if node else None
        return {p.props[0]: p.props[4:] for p in container.children
                if p.name == "P" and len(p.props) >= 5 and isinstance(p.props[0], str)} if container else {}

    def numeric(values):
        return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)

    settings = properties(root("GlobalSettings"))
    units = settings.get("UnitScaleFactor", [])
    if len(units) == 1 and numeric(units) and units[0] > 0:
        result["unit_meters"] = units[0] / 100
        finding("fbx.units", "pass", result["unit_meters"], None, "Meters per FBX unit")
    else:
        finding("fbx.units", "warn", None, "explicit positive UnitScaleFactor", "Units are not established")
    objects = root("Objects")
    if not objects:
        finding("fbx.objects", "fail", None, "Objects", "Missing object section")
        return result, findings
    by_id = {}
    result["counts"] = dict(Counter(n.name for n in objects.children))
    for node in objects.children:
        if not node.props or not isinstance(node.props[0], int) or node.props[0] in by_id:
            finding("fbx.object_id", "fail", None, "unique integer ID", "Invalid or duplicate object ID")
            continue
        by_id[node.props[0]] = node
    connections = root("Connections")
    links = []
    for node in connections.children if connections else []:
        if node.name != "C":
            continue
        kind = node.props[0] if node.props else None
        valid_form = ((kind == "OO" and len(node.props) == 3) or
                      (kind == "OP" and len(node.props) == 4 and isinstance(node.props[3], str)))
        if not valid_form or not all(isinstance(v, int) for v in node.props[1:3]):
            finding("fbx.connection", "fail", None, "valid connection", "Malformed connection")
            continue
        source, target = node.props[1:3]
        if source not in by_id or (target != 0 and target not in by_id):
            finding("fbx.connection", "fail", [source, target], None, "Connection object is absent")
            continue
        links.append((source, target, kind))
        if target and by_id[source].name == by_id[target].name == "Model":
            finding("fbx.hierarchy", "warn", [source, target], None, "Model hierarchy requires transform composition")
    meshes = [n for n in by_id.values() if n.name == "Geometry" and len(n.props) > 2 and n.props[2] == "Mesh"]
    if not meshes or not any(n.name == "Model" for n in by_id.values()):
        finding("fbx.objects", "fail", None, "mesh geometry and model", "Missing model scene or mesh geometry")
    for object_id, node in by_id.items():
        name = str(node.props[1]).split("\x00")[0] if len(node.props) > 1 else str(object_id)
        if node.name == "Geometry" and len(node.props) > 2 and node.props[2] == "Mesh":
            vertex_node = node.child("Vertices")
            index_node = node.child("PolygonVertexIndex")
            vertices = vertex_node.props[0] if vertex_node and vertex_node.props else None
            indices = index_node.props[0] if index_node and index_node.props else None
            if not isinstance(vertices, (list, tuple, array)) or len(vertices) % 3 or not numeric(vertices) or not isinstance(indices, (list, tuple, array)):
                finding("fbx.geometry", "fail", object_id, None, "Invalid vertex or polygon arrays")
                continue
            face_length = 0
            valid = True
            non_triangles = 0
            for encoded in indices:
                if not isinstance(encoded, int):
                    valid = False
                    break
                index = -encoded-1 if encoded < 0 else encoded
                if index >= len(vertices)//3:
                    valid = False
                face_length += 1
                if encoded < 0:
                    result["polygons"] += 1
                    if face_length == 3:
                        result["triangles"] += 1
                    else:
                        non_triangles += 1
                    face_length = 0
            if face_length or not valid or not indices:
                finding("fbx.geometry", "fail", object_id, None, "Invalid face indices or unfinished polygon")
            if non_triangles:
                finding("fbx.triangulation", "fail", non_triangles, 0, "Non-triangle polygons")
        elif node.name == "Geometry":
            finding("fbx.geometry_type", "fail", object_id, "Mesh", "Unsupported geometry type")
        elif node.name == "Model":
            props = properties(node)
            transforms = {}
            for key, default in [("Lcl Translation", [0, 0, 0]), ("Lcl Rotation", [0, 0, 0]), ("Lcl Scaling", [1, 1, 1])]:
                value = props.get(key, default)
                if len(value) != 3 or not numeric(value):
                    finding("fbx.transform", "fail", object_id, None, "Invalid local transform")
                    transforms[key] = None
                else:
                    transforms[key] = value
                    if value != default:
                        finding("fbx.transform", "warn", value, default, f"{name}: {key} is not identity")
            result["models"].append({"id": object_id, "name": name, "transforms": transforms})
        elif node.name == "Video":
            content = node.child("Content")
            data = content.props[0] if content and content.props else b""
            paths = {}
            for path_key in ["Filename", "FileName", "RelativeFilename"]:
                path_node = node.child(path_key)
                if path_node and path_node.props and isinstance(path_node.props[0], str):
                    paths[path_key] = path_node.props[0]
            if data:
                try:
                    if not isinstance(data, bytes):
                        raise ReadError("png.integrity", "Embedded content is not raw bytes")
                    image = inspect_png(data)
                    image.update({"id": object_id, "name": name, "paths": paths})
                    result["images"].append(image)
                except ReadError as exc:
                    finding(exc.rule, "fail", object_id, None, str(exc))
        elif node.name == "Texture":
            result["textures"].append({"id": object_id, "name": name})
    image_ids = {image["id"] for image in result["images"]}
    for texture in result["textures"]:
        available = any(source in image_ids and target == texture["id"] and kind == "OO"
                        for source, target, kind in links)
        finding("fbx.resource", "pass" if available else "fail", texture["id"], "linked embedded PNG",
                "Embedded resource is available" if available else "Texture has no valid linked embedded PNG")
    return result, findings
