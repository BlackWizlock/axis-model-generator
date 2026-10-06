"""Strict, bounded JSON and manifest contract for portable packages."""

from dataclasses import dataclass, field, fields
from datetime import datetime
import json
import math
import re
import unicodedata

from .limits import ReadError


class PackageError(ReadError):
    """An addressed input error; ordinary exception traceback semantics apply."""

    def __init__(self, rule, message, *, file="", actual=None, expected=None,
                 element_key=None, action="Исправить входной пакет"):
        super().__init__(rule, message)
        self.file = file
        self.actual = actual
        self.expected = expected
        self.element_key = element_key
        self.action = action


@dataclass(frozen=True)
class PackageLimits:
    input_bytes: int = 256 * 1024**2
    expanded_bytes: int = 256 * 1024**2
    member_bytes: int = 64 * 1024**2
    entries: int = 1000
    compression_ratio: int = 200
    manifest_bytes: int = 1024**2
    scene_bytes: int = 8 * 1024**2
    json_depth: int = 32
    json_nodes: int = 200000
    json_string_bytes: int = 65536
    scene_records: int = 100000
    array_items: int = 10000000

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field.name} must be a positive integer")


@dataclass(frozen=True)
class ArrayDescriptor:
    dtype: str
    byte_order: str
    count: int
    components: int
    stride_bytes: int


@dataclass(frozen=True)
class FileDescriptor:
    path: str
    role: str
    bytes: int
    sha256: str
    array: ArrayDescriptor | None


@dataclass(frozen=True)
class PackageManifest:
    package_version: tuple[int, int]
    metadata: dict[str, object]
    required_capabilities: tuple[str, ...]
    scene_path: str
    files: tuple[FileDescriptor, ...]
    capabilities: dict[str, dict[str, object]]


CAPABILITIES = frozenset({"scene-v1", "geometry-f64-u32-v1", "links-v1", "material-basic-v1", "vertex-normals-v1", "vertex-uv-v1", "ifc-reference-v1"})
AREAS = frozenset({"geometry", "materials", "uv", "normals", "links", "coordinates", "parameters", "ifc"})
ARRAY_ROLES = {"vertices": ("float64", 3, 24), "triangles": ("uint32", 3, 12), "normals": ("float64", 3, 24), "uv": ("float64", 2, 16)}
ROLE_FOLDERS = {**dict.fromkeys(ARRAY_ROLES, "geometry"), "texture": "textures", "ifc": "information", "evidence": "evidence"}
FORBIDDEN_SUFFIXES = frozenset({"zip", "py", "pyc", "exe", "dll", "sh", "bat", "cmd", "ps1", "blend"})


def _fail(message, file="manifest.json", *, rule="package.schema", actual=None, expected=None):
    raise PackageError(rule, message, file=file, actual=actual, expected=expected)


def _object(value, required, file):
    if not isinstance(value, dict):
        _fail("Expected a JSON object", file)
    if set(value) - set(required) - {"extensions"} or set(required) - set(value):
        _fail("Missing or unknown object fields", file, actual=sorted(value), expected=sorted(required))
    if "extensions" in value and not isinstance(value["extensions"], dict):
        _fail("extensions must be a JSON object", file)
    return value


def _integer(value, file, *, minimum=0):
    if type(value) is not int or value < minimum:
        _fail("Expected a bounded nonnegative integer", file, actual=value, expected=f"integer >= {minimum}")
    return value


def _text(value, file, *, identifier=False, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value):
        _fail("Expected a nonempty string", file)
    if identifier and (len(value.encode("utf-8")) > 512 or any(unicodedata.category(c) == "Cc" for c in value)):
        _fail("Invalid identifier", file)
    return value


def _list(value, file, limits):
    if not isinstance(value, list):
        _fail("Expected a JSON list", file)
    if len(value) > limits.json_nodes:
        _fail("List exceeds item budget", file, rule="package.budget")
    return value


def validate_package_path(value):
    """Reject ambiguous names without normalizing them into safe names."""
    if not isinstance(value, str):
        _fail("Expected a relative package path", rule="package.path")
    parts = value.split("/")
    unsafe = (not value or unicodedata.normalize("NFC", value) != value or
              any(c in '<>:"|?*\\' or unicodedata.category(c) in {"Cc", "Cs"} for c in value) or
              any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts))
    for part in parts:
        basename = part.split(".", 1)[0].upper()
        unsafe = unsafe or basename in {"CON", "PRN", "AUX", "NUL"} or bool(re.fullmatch(r"(?:COM|LPT)[1-9]", basename))
    if unsafe:
        _fail("Unsafe or noncanonical package path", value, rule="package.path")
    return value


@dataclass
class _PathNode:
    spelling: str
    children: dict[str, "_PathNode"] = field(default_factory=dict)
    is_file: bool = False


def _unique_paths(paths, limits: PackageLimits | None = None):
    """Check paths with bounded linear component storage and no recursion."""
    limits = limits or PackageLimits()
    root = _PathNode("")
    used_components = 0
    for path in paths:
        validate_package_path(path)
        used_components += path.count("/") + 1
        if used_components > limits.json_nodes:
            _fail("Aggregate path component budget exceeded", path, rule="package.budget",
                  actual=used_components, expected=limits.json_nodes)
        node = root
        for part in path.split("/"):
            if node.is_file:
                _fail("File conflicts with a derived directory", path, rule="package.path")
            key = part.casefold()
            child = node.children.get(key)
            if child is None:
                child = _PathNode(part)
                node.children[key] = child
            elif child.spelling != part:
                _fail("Different path spellings share a casefold key", path, rule="package.path")
            node = child
        if node.is_file or node.children:
            _fail("Duplicate file or derived directory conflict", path, rule="package.path")
        node.is_file = True


def check_member_suffix(path):
    if path.rsplit(".", 1)[-1].casefold() in FORBIDDEN_SUFFIXES:
        _fail("Nested archives and executable resources are forbidden", path, rule="package.content")


def _scan_depth(text, file, limit):
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                _fail("JSON nesting exceeds budget", file, rule="package.budget", actual=depth, expected=limit)
        elif char in "]}":
            depth -= 1


def decode_package_json(data: bytes, *, file: str, byte_budget: int, limits: PackageLimits) -> dict[str, object]:
    """Decode bounded UTF-8 JSON without duplicates or nonfinite numbers."""
    if len(data) > byte_budget:
        _fail("JSON bytes exceed budget", file, rule="package.budget", actual=len(data), expected=byte_budget)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("Duplicate JSON key", file, rule="package.json", actual=key)
            result[key] = value
        return result

    def reject_constant(value):
        _fail("Nonfinite JSON number", file, rule="package.json", actual=value)

    try:
        text = data.decode("utf-8", errors="strict")
        if text.startswith("\ufeff"):
            _fail("UTF-8 BOM is forbidden", file, rule="package.json")
        _scan_depth(text, file, limits.json_depth)
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
        if not isinstance(value, dict):
            _fail("JSON root must be an object", file, rule="package.json")
        pending = [value]
        nodes = 0
        while pending:
            item = pending.pop()
            nodes += 1
            if nodes > limits.json_nodes:
                _fail("JSON nodes exceed budget", file, rule="package.budget", actual=nodes, expected=limits.json_nodes)
            if isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif isinstance(item, str):
                size = len(item.encode("utf-8", errors="strict"))
                if size > limits.json_string_bytes:
                    _fail("JSON string exceeds budget", file, rule="package.budget", actual=size, expected=limits.json_string_bytes)
            elif isinstance(item, float) and not math.isfinite(item):
                _fail("Nonfinite JSON number", file, rule="package.json")
        return value
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("package.json", "Invalid UTF-8 JSON", file=file) from None


def _metadata(value, limits):
    file = "manifest.json"
    obj = _object(value, {"package_id", "snapshot_id", "created_utc", "plugin_version", "revit", "ifc_exporter", "selection"}, file)
    for name in ("package_id", "snapshot_id", "plugin_version"):
        _text(obj[name], file, identifier=True)
    created = _text(obj["created_utc"], file)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", created):
            raise ValueError
        datetime.fromisoformat(created)
    except ValueError:
        _fail("created_utc must be an ISO-8601 UTC timestamp ending in Z", file)
    revit = _object(obj["revit"], {"year", "build"}, file)
    if not 2020 <= _integer(revit["year"], file) <= 2026:
        _fail("Unsupported Revit metadata year", file)
    _text(revit["build"], file, identifier=True)
    if obj["ifc_exporter"] is not None:
        exporter = _object(obj["ifc_exporter"], {"name", "version"}, file)
        for name in ("name", "version"):
            _text(exporter[name], file, identifier=True)
    selection = _object(obj["selection"], {"phase", "design_option", "scope", "included_link_instance_ids"}, file)
    for name in ("phase", "design_option", "scope"):
        _text(selection[name], file)
    ids = _list(selection["included_link_instance_ids"], file, limits)
    for value in ids:
        _text(value, file, identifier=True)
    return obj


def _array(value, role, size, file, limits):
    if role not in ARRAY_ROLES:
        if value is not None:
            _fail("Non-array role requires array=null", file)
        return None
    obj = _object(value, {"dtype", "byte_order", "count", "components", "stride_bytes"}, file)
    dtype, components, stride = ARRAY_ROLES[role]
    count = _integer(obj["count"], file)
    _integer(obj["components"], file)
    _integer(obj["stride_bytes"], file)
    if obj["dtype"] != dtype or obj["byte_order"] != "little" or obj["components"] != components or obj["stride_bytes"] != stride:
        _fail("Array layout does not match its role", file)
    if size != count * stride:
        _fail("Array bytes do not match count times stride", file, rule="package.size", actual=size, expected=count * stride)
    if count * components > limits.array_items:
        _fail("Array scalar budget exceeded", file, rule="package.budget", actual=count * components, expected=limits.array_items)
    return ArrayDescriptor(dtype, "little", count, components, stride)


def _files(value, limits):
    rows = _list(value, "manifest.json", limits)
    if len(rows) + 1 > limits.entries:
        _fail("Manifest file count exceeds entry budget", rule="package.budget")
    result = []
    scalar_count = 0
    for row in rows:
        row = _object(row, {"path", "role", "bytes", "sha256", "array"}, "manifest.json")
        path = validate_package_path(row["path"])
        check_member_suffix(path)
        if path == "manifest.json":
            _fail("Manifest cannot list itself", path, rule="package.inventory")
        role = _text(row["role"], path)
        if role not in ROLE_FOLDERS and role != "scene":
            _fail("Unknown mandatory file role", path, rule="package.unsupported", actual=role)
        if role == "scene":
            if path != "scene.json":
                _fail("Scene must be scene.json", path)
        elif not path.startswith(ROLE_FOLDERS[role] + "/"):
            _fail("File is outside its role folder", path)
        size = _integer(row["bytes"], path)
        maximum = min(limits.member_bytes, limits.scene_bytes) if role == "scene" else limits.member_bytes
        if size > maximum:
            _fail("Declared member size exceeds budget", path, rule="package.budget", actual=size, expected=maximum)
        sha256 = _text(row["sha256"], path)
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            _fail("Invalid SHA-256 encoding", path)
        array = _array(row["array"], role, size, path, limits)
        if array is not None:
            scalar_count += array.count * array.components
            if scalar_count > limits.array_items:
                _fail("Total array scalar budget exceeded", path, rule="package.budget", actual=scalar_count, expected=limits.array_items)
        result.append(FileDescriptor(path, role, size, sha256, array))
    _unique_paths((row.path for row in result), limits)
    return tuple(result)


def _element_key(value, file, limits):
    obj = _object(value, {"document_id", "link_instance_path", "unique_id"}, file)
    for key in ("document_id", "unique_id"):
        _text(obj[key], file, identifier=True)
    for item in _list(obj["link_instance_path"], file, limits):
        _text(item, file, identifier=True)


def _capabilities(value, files, limits):
    obj = _object(value, AREAS, "manifest.json")
    paths = {item.path for item in files}
    for area in AREAS:
        capability = _object(obj[area], {"state", "evidence", "limitations"}, "manifest.json")
        state = _text(capability["state"], "manifest.json")
        if state not in {"available", "partial", "missing", "unsupported"}:
            _fail("Unknown capability state")
        evidence = _list(capability["evidence"], "manifest.json", limits)
        for item in evidence:
            _text(item, "manifest.json")
        if capability["state"] == "available" and not evidence:
            _fail("Available capability requires evidence")
        for item in _list(capability["limitations"], "manifest.json", limits):
            item = _object(item, {"file", "element_key", "message"}, "manifest.json")
            file = _text(item["file"], "manifest.json", allow_empty=True)
            if file and file not in paths:
                _fail("Capability limitation references an unlisted file", file)
            _text(item["message"], file or "manifest.json")
            if item["element_key"] is not None:
                _element_key(item["element_key"], file or "manifest.json", limits)
    return {area: obj[area] for area in AREAS}


def parse_manifest(data: bytes, limits: PackageLimits) -> PackageManifest:
    obj = decode_package_json(data, file="manifest.json", byte_budget=limits.manifest_bytes, limits=limits)
    _object(obj, {"package_version", "metadata", "required_capabilities", "scene", "files", "capabilities"}, "manifest.json")
    version = _object(obj["package_version"], {"major", "minor"}, "manifest.json")
    major = _integer(version["major"], "manifest.json")
    minor = _integer(version["minor"], "manifest.json")
    if major != 1:
        _fail("Unsupported package major version", rule="package.unsupported", actual=major, expected=1)
    required = _list(obj["required_capabilities"], "manifest.json", limits)
    for item in required:
        _text(item, "manifest.json")
        if item not in CAPABILITIES:
            _fail("Unsupported required capability", rule="package.unsupported", actual=item)
    if len(set(required)) != len(required) or not {"scene-v1", "geometry-f64-u32-v1"}.issubset(required):
        _fail("Missing or duplicate core required capability")
    metadata = _metadata(obj["metadata"], limits)
    scene = _object(obj["scene"], {"path", "schema_version"}, "manifest.json")
    if scene["path"] != "scene.json" or type(scene["schema_version"]) is not int or scene["schema_version"] != 1:
        _fail("Invalid scene path or schema version")
    files = _files(obj["files"], limits)
    if sum(item.role == "scene" for item in files) != 1:
        _fail("Exactly one scene descriptor is required", "scene.json", rule="package.inventory")
    if any(item.role == "ifc" for item in files) and metadata["ifc_exporter"] is None:
        _fail("IFC file requires exporter metadata")
    return PackageManifest((major, minor), metadata, tuple(required), scene["path"], files, _capabilities(obj["capabilities"], files, limits))
