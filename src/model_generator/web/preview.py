"""Bounded, neutral display geometry; no source or regulatory fidelity claims."""
from dataclasses import dataclass, fields
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile

from ..package_manifest import PackageError, PackageLimits, decode_package_json
from ..package_reader import PackageData
from ..package_scene import (SceneInspection, _IDENTITY, _compose, _determinant,
                             _matrix, _point)


_HARD_CAPS = {'instances': 1000, 'vertices': 300000, 'triangles': 200000,
              'wire_bytes': 16777216}
ROUNDTRIP_TOLERANCE_METRES = 1e-5
_FIELDS = frozenset({'schemaVersion', 'provenance', 'coordinates', 'origin',
                     'bounds', 'positions', 'indices', 'vertexCount',
                     'triangleCount', 'limitations'})
_LIMITATIONS = [
    'Neutral display preview only; UV, materials and source silhouette fidelity are not verified.',
    'Float32 display precision does not prove global coordinates, export fidelity or regulatory compliance.',
]


class PreviewError(ValueError):
    """A bounded public failure code, without raw source paths or values."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fail(message, code='preview_unsupported'):
    raise PreviewError(code, message)


@dataclass(frozen=True)
class PreviewLimits:
    instances: int = 1000
    vertices: int = 300000
    triangles: int = 200000
    wire_bytes: int = 16777216

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= _HARD_CAPS[field.name]:
                raise ValueError(f'{field.name} must be positive and within the hard cap')


def _number(value):
    if type(value) not in {int, float}:
        _fail('Expected a finite number')
    try:
        number = float(value)
    except (ValueError, OverflowError):
        _fail('Expected a finite float64 number')
    if not math.isfinite(number):
        _fail('Expected a finite number')
    return number


def _count(value, cap):
    if type(value) is not int or value <= 0:
        _fail('Expected a positive integer count')
    if value > cap:
        _fail('Geometry count exceeds preview budget', 'preview_budget')
    return value


def _vector(value):
    if type(value) is not list or len(value) != 3:
        _fail('Expected a three-number vector')
    return [_number(item) for item in value]


def _bounds(positions):
    minimum = list(positions[:3])
    maximum = list(minimum)
    for n in range(3, len(positions), 3):
        for axis in range(3):
            value = positions[n + axis]
            minimum[axis] = min(minimum[axis], value)
            maximum[axis] = max(maximum[axis], value)
    return {'min': minimum, 'max': maximum}


def _float32_error(value):
    try:
        rounded = struct.unpack('<f', struct.pack('<f', value))[0]
    except (OverflowError, struct.error):
        _fail('Rebased coordinates are outside Float32 range', 'preview_roundtrip_error')
    error = abs(value - rounded)
    if not math.isfinite(rounded) or error > ROUNDTRIP_TOLERANCE_METRES:
        _fail('Rebased Float32 precision exceeds 1e-5 metres', 'preview_roundtrip_error')
    return error


def _encode(document, limits):
    """Count UTF-8 bytes while encoding, before assembling the bounded buffer."""
    encoder = json.JSONEncoder(allow_nan=False, ensure_ascii=False,
                               sort_keys=True, separators=(',', ':'))
    buffer = bytearray()
    for chunk in encoder.iterencode(document):
        encoded = chunk.encode('utf-8')
        if len(buffer) + len(encoded) > limits.wire_bytes:
            _fail('Serialized preview exceeds wire budget', 'preview_budget')
        buffer.extend(encoded)
    return bytes(buffer)


def validate_preview(document: dict, limits: PreviewLimits) -> dict:
    """Validate exact schema1 fields, bounds, centring and display precision."""
    if type(document) is not dict or set(document) != _FIELDS:
        _fail('Missing or unknown preview fields')
    if type(document['schemaVersion']) is not int or document['schemaVersion'] != 1:
        _fail('Unknown preview schema')
    if (type(document['provenance']) is not str or
            document['provenance'] not in {'uploaded_package', 'synthetic'}):
        _fail('Unknown preview provenance')
    if document['coordinates'] != 'processing-metres-rebased':
        _fail('Unsupported preview coordinate system')
    vertices = _count(document['vertexCount'], limits.vertices)
    triangles = _count(document['triangleCount'], limits.triangles)
    positions, indices = document['positions'], document['indices']
    if (type(positions) is not list or len(positions) != vertices * 3 or
            type(indices) is not list or len(indices) != triangles * 3):
        _fail('Geometry array lengths disagree with counts')
    origin = _vector(document['origin'])
    for n, value in enumerate(positions):
        number = _number(value)
        _float32_error(number)
        if not math.isfinite(origin[n % 3] + number):
            _fail('Restored processing coordinate overflow')
    for index in indices:
        if type(index) is not int or not 0 <= index < vertices:
            _fail('Triangle index outside preview vertex array')
    bounds = document['bounds']
    if type(bounds) is not dict or set(bounds) != {'min', 'max'}:
        _fail('Missing or unknown bounds fields')
    minimum, maximum = _vector(bounds['min']), _vector(bounds['max'])
    actual = _bounds(positions)
    if minimum != actual['min'] or maximum != actual['max']:
        _fail('Declared bounds disagree with checked geometry')
    for low, high in zip(minimum, maximum):
        if abs(low / 2 + high / 2) > ROUNDTRIP_TOLERANCE_METRES:
            _fail('Preview geometry is not rebased to its bounds centre')
    limitations = document['limitations']
    if type(limitations) is not list or not 1 <= len(limitations) <= 16:
        _fail('Expected a bounded limitations list')
    for item in limitations:
        if type(item) is not str or not item or len(item) > 1024:
            _fail('Expected a bounded limitation string')
        try:
            item.encode('utf-8')
        except UnicodeError:
            _fail('Invalid UTF-8 limitation string')
    _encode(document, limits)
    return document


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('Duplicate preview JSON key')
        result[key] = value
    return result


def decode_preview_json(data: bytes, limits: PreviewLimits) -> dict:
    """Bounded strict wire reader shared with the isolated trusted script."""
    if type(data) is not bytes:
        _fail('Expected preview JSON bytes')
    if len(data) > limits.wire_bytes:
        _fail('Preview JSON exceeds wire budget', 'preview_budget')
    try:
        document = json.loads(data.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=lambda value: _fail('Nonfinite JSON value'))
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        if isinstance(error, PreviewError):
            raise
        _fail('Malformed preview JSON')
    return validate_preview(document, limits)


def preview_measurements(document: dict) -> dict:
    """Keep Float32 bridge measurements separate from model/report status axes."""
    validate_preview(document, PreviewLimits())
    return {'vertex_count': document['vertexCount'], 'triangle_count': document['triangleCount'],
            'bounds': document['bounds'],
            'float32_max_error_metres': max(_float32_error(_number(value)) for value in document['positions']),
            'float32_tolerance_metres': ROUNDTRIP_TOLERANCE_METRES}


def write_preview(document: dict, path: Path, limits: PreviewLimits) -> tuple[int, str]:
    """Atomically replace only a worker-chosen path with private checked bytes."""
    validate_preview(document, limits)
    data = _encode(document, limits)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.preview-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(data), hashlib.sha256(data).hexdigest()


def _scene_object(row, required):
    if (type(row) is not dict or set(row) - set(required) - {'extensions'} or
            set(required) - set(row) or
            ('extensions' in row and (type(row['extensions']) is not dict or row['extensions']))):
        _fail('Missing or unknown scene preview fields or features')


def _checked_member(package, descriptor):
    content = package.members.get(descriptor.path)
    if (type(content) is not bytes or len(content) != descriptor.bytes or
            hashlib.sha256(content).hexdigest() != descriptor.sha256):
        _fail('Inspected member bytes no longer match their descriptor')
    return content


def _build_preview(package, inspection, limits):
    if not isinstance(package, PackageData) or not isinstance(inspection, SceneInspection):
        _fail('Expected a strict package and scene inspection')
    scene = inspection.scene
    _scene_object(scene, {'schema_version', 'snapshot_id', 'root_document_id', 'documents',
                          'links', 'omitted_links', 'meshes', 'instances', 'materials', 'coordinates', 'ifc'})
    descriptors = {item.path: item for item in package.manifest.files}
    if len(descriptors) != len(package.manifest.files):
        _fail('Duplicate member descriptors')
    descriptor = descriptors.get(package.manifest.scene_path)
    if descriptor is None or descriptor.role != 'scene':
        _fail('Missing scene descriptor')
    scene_bytes = _checked_member(package, descriptor)
    if scene != decode_package_json(scene_bytes, limits=PackageLimits(), file=descriptor.path,
                                    byte_budget=PackageLimits().scene_bytes):
        _fail('Scene inspection is not bound to package scene bytes')
    coords = scene['coordinates']
    _scene_object(coords, {'units', 'axes', 'processing_origin', 'processing_to_project',
                           'shared', 'regional', 'vertical', 'control_points', 'height_check'})
    _vector(coords['processing_origin'])
    _matrix(coords['processing_to_project'])
    if (type(scene['schema_version']) is not int or scene['schema_version'] != 1 or
            coords['units'] != 'metre' or coords['axes'] != 'right-handed-z-up'):
        _fail('Unsupported scene coordinates or schema')
    instances = scene['instances']
    if type(instances) is not list:
        _fail('Expected inspected instances')
    _count(len(instances), limits.instances)
    meshes = {}
    for mesh in scene['meshes']:
        _scene_object(mesh, {'mesh_id', 'vertices', 'triangles', 'vertex_count', 'triangle_count',
                             'normals', 'uv', 'material_ranges'})
        if mesh['mesh_id'] in meshes:
            _fail('Duplicate mesh identifier')
        meshes[mesh['mesh_id']] = mesh
    # All instance-expanded budgets and byte/layout checks precede any unpacking
    # or transformed allocation. Meshes are scanned only if actually displayed.
    vertices = triangles = 0
    used_meshes = {}
    for instance in instances:
        _scene_object(instance, {'instance_id', 'mesh_id', 'element_key', 'transform', 'element_id'})
        mesh = meshes[instance['mesh_id']]
        vertices += _count(mesh['vertex_count'], limits.vertices)
        triangles += _count(mesh['triangle_count'], limits.triangles)
        _count(vertices, limits.vertices)
        _count(triangles, limits.triangles)
        used_meshes[mesh['mesh_id']] = mesh
    arrays = {}
    for name, mesh in used_meshes.items():
        arrays[name] = {}
        for role, dtype, stride, count in (
                ('vertices', 'float64', 24, mesh['vertex_count']),
                ('triangles', 'uint32', 12, mesh['triangle_count'])):
            descriptor = descriptors[mesh[role]]
            array = descriptor.array
            if (descriptor.role != role or array is None or array.dtype != dtype or
                    array.byte_order != 'little' or type(array.count) is not int or array.count != count or
                    type(array.components) is not int or array.components != 3 or
                    type(array.stride_bytes) is not int or array.stride_bytes != stride or
                    descriptor.bytes != count * stride):
                _fail('Mesh array layout or byte count disagrees with checked contract')
            arrays[name][role] = _checked_member(package, descriptor)
    links = {}
    for link in scene['links']:
        _scene_object(link, {'parent_document_id', 'document_id', 'link_instance_id', 'transform'})
        key = (link['parent_document_id'], link['link_instance_id'])
        if key in links:
            _fail('Duplicate link instance address')
        links[key] = (link['document_id'], _matrix(link['transform']))
    placements = []
    for instance in instances:
        key = instance['element_key']
        _scene_object(key, {'document_id', 'link_instance_path', 'unique_id'})
        parent, transform = scene['root_document_id'], _IDENTITY
        seen = {parent}
        path = key['link_instance_path']
        if type(path) is not list or len(path) > len(links):
            _fail('Invalid link instance path')
        for name in path:
            parent, link_transform = links[parent, name]
            if parent in seen:
                _fail('Cyclic link instance path')
            seen.add(parent)
            transform = _compose(transform, link_transform)
        if parent != key['document_id']:
            _fail('Link path ends in a different document')
        transform = _compose(transform, _matrix(instance['transform']))
        placements.append((instance['mesh_id'], transform, _determinant(transform) < 0))
    # Recheck every binary index and finite coordinate before transforming.
    for name, buffers in arrays.items():
        count = used_meshes[name]['vertex_count']
        for vertex in struct.iter_unpack('<ddd', buffers['vertices']):
            for value in vertex:
                _number(value)
        for triangle in struct.iter_unpack('<III', buffers['triangles']):
            if any(index >= count for index in triangle):
                _fail('Source triangle index outside source mesh')
    positions, indices = [], []
    for name, transform, mirrored in placements:
        offset = len(positions) // 3
        for vertex in struct.iter_unpack('<ddd', arrays[name]['vertices']):
            positions.extend(_point(transform, vertex))
        for a, b, c in struct.iter_unpack('<III', arrays[name]['triangles']):
            indices.extend((offset + a, offset + c, offset + b) if mirrored else
                           (offset + a, offset + b, offset + c))
    bounds = _bounds(positions)
    origin = [low / 2 + high / 2 for low, high in zip(bounds['min'], bounds['max'])]
    for n, value in enumerate(positions):
        positions[n] = _number(value - origin[n % 3])
    document = {'schemaVersion': 1, 'provenance': 'uploaded_package',
                'coordinates': 'processing-metres-rebased', 'origin': origin,
                'bounds': _bounds(positions), 'positions': positions, 'indices': indices,
                'vertexCount': vertices, 'triangleCount': triangles, 'limitations': list(_LIMITATIONS)}
    return validate_preview(document, limits)


def build_preview(package: PackageData, inspection: SceneInspection, limits: PreviewLimits) -> dict:
    try:
        return _build_preview(package, inspection, limits)
    except (PackageError, KeyError, TypeError, IndexError, OverflowError, struct.error, RecursionError):
        _fail('Inspected geometry is unsupported or inconsistent')


def build_synthetic_demo() -> dict:
    document = {'schemaVersion': 1, 'provenance': 'synthetic',
                'coordinates': 'processing-metres-rebased', 'origin': [0, 0, 0],
                'bounds': {'min': [-.5, -.5, 0], 'max': [.5, .5, 0]},
                'positions': [-.5, -.5, 0, .5, -.5, 0, -.5, .5, 0],
                'indices': [0, 1, 2], 'vertexCount': 3, 'triangleCount': 1,
                'limitations': list(_LIMITATIONS)}
    return validate_preview(document, PreviewLimits())
