"""Write triangle meshes as a binary FBX 7400 scene in metres, Z up.

The writer states explicit units and axes, one model per mesh without any
transform, and material slots with per-triangle assignments. It writes no
normals, UVs, textures or animation. Output is deterministic.
"""

from array import array
from dataclasses import dataclass, field
import math
import struct
import sys
import zlib

from .fbx_binary import MAGIC
from .limits import ReadError

# Single precision readers keep about 1 cm at this distance from the origin.
COORDINATE_REACH = 100_000.0
FILE_ID = bytes.fromhex('28b32aebb624ccc2bfc8b02aa92bfcf1')
FOOT_ID = bytes.fromhex('fabcab09d0c8d466b176fb831cf7267e')
FOOT_MAGIC = bytes.fromhex('f85a8c6adef5d97eece90ce3758f290b')


@dataclass(frozen=True)
class ExportMaterial:
    name: str
    color: tuple = None  # diffuse red, green, blue in 0..1, or None when unknown


@dataclass(frozen=True)
class ExportMesh:
    name: str
    positions: array         # x, y, z in metres
    triangles: array         # vertex indices, three per triangle
    materials: tuple = ()
    material_indices: array = field(default_factory=lambda: array('i'))  # one slot per triangle, or empty


class _Long(int):
    """An integer written as 64 bits: FBX object identifiers."""


def _reject(message):
    raise ReadError('export.fbx', message)


def _property(value):
    if isinstance(value, _Long):
        return b'L' + struct.pack('<q', value)
    if isinstance(value, int):
        return b'I' + struct.pack('<i', value)
    if isinstance(value, float):
        return b'D' + struct.pack('<d', value)
    if isinstance(value, str):
        value = value.encode('utf-8')
        return b'S' + struct.pack('<I', len(value)) + value
    if isinstance(value, bytes):
        return b'R' + struct.pack('<I', len(value)) + value
    values = array(value.typecode, value)
    if sys.byteorder != 'little':
        values.byteswap()
    raw = values.tobytes()
    packed = zlib.compress(raw, 6) if len(raw) >= 128 else raw
    return (value.typecode.encode() + struct.pack('<III', len(value), int(packed is not raw), len(packed))
            + packed)


def _node(offset, name, props=(), children=()):
    name = name.encode('ascii')
    body = b''.join(_property(p) for p in props)
    parts, position = [], offset + 13 + len(name) + len(body)
    for child in children:
        parts.append(_node(position, *child))
        position += len(parts[-1])
    if children:
        parts.append(bytes(13))
        position += 13
    if position >= 2 ** 32:
        _reject('The scene exceeds the FBX 7400 size range')
    return struct.pack('<IIIB', position, len(props), len(body), len(name)) + name + body + b''.join(parts)


def _properties(*entries):
    return ('Properties70', (), tuple(('P', entry) for entry in entries))


def _label(name, kind):
    return name + '\x00\x01' + kind


def _check(meshes):
    if not meshes:
        _reject('Nothing to export')
    names = [mesh.name for mesh in meshes] + [m.name for mesh in meshes for m in mesh.materials]
    if any(not isinstance(n, str) or not n or any(ord(c) < 32 for c in n) for n in names):
        _reject('Object names must be printable and not empty')
    if len({mesh.name for mesh in meshes}) != len(meshes):
        _reject('Mesh names must be unique')
    for mesh in meshes:
        vertices, faces = len(mesh.positions) // 3, len(mesh.triangles) // 3
        if not vertices or len(mesh.positions) % 3 or not faces or len(mesh.triangles) % 3:
            _reject('A mesh needs whole vertices and triangles')
        if not all(math.isfinite(c) and abs(c) <= COORDINATE_REACH for c in mesh.positions):
            _reject('Coordinates need a local origin within the single precision reach')
        if not all(0 <= i < vertices for i in mesh.triangles):
            _reject('Triangle refers to a missing vertex')
        if any(len(set(mesh.triangles[i:i + 3])) != 3 for i in range(0, len(mesh.triangles), 3)):
            _reject('Triangle repeats a vertex')
        if len(mesh.material_indices) not in (0, faces) or (mesh.material_indices and not mesh.materials):
            _reject('Material assignment must cover every triangle')
        if not all(0 <= i < len(mesh.materials) for i in mesh.material_indices):
            _reject('Triangle refers to a missing material slot')
        if len({m.name for m in mesh.materials}) != len(mesh.materials):
            _reject('Material slots of one mesh must have unique names')
        for material in mesh.materials:
            color = material.color
            if color is not None and (len(color) != 3 or not all(isinstance(c, (int, float))
                                                                 and 0 <= c <= 1 for c in color)):
                _reject('Material colour must be three values within 0..1')


def write_fbx(meshes):
    """Return the bytes of a binary FBX 7400 scene, or refuse with a ReadError."""
    meshes = tuple(meshes)
    _check(meshes)
    objects, links, next_id = [], [], 1_000_000
    shared = {}
    for mesh in meshes:
        geometry_id, model_id = _Long(next_id), _Long(next_id + 1)
        next_id += 2
        indices = array('i', mesh.triangles)
        for last in range(2, len(indices), 3):
            indices[last] = -indices[last] - 1
        layers = []
        if mesh.materials:
            # Without assignments every triangle uses the first slot of the mesh.
            slots = array('i', mesh.material_indices) or array('i', [0])
            layers = [('LayerElementMaterial', (0,), (
                          ('Version', (101,)), ('Name', ('',)),
                          ('MappingInformationType', ('ByPolygon' if mesh.material_indices else 'AllSame',)),
                          ('ReferenceInformationType', ('IndexToDirect',)), ('Materials', (slots,)))),
                      ('Layer', (0,), (('Version', (100,)), ('LayerElement', (), (
                          ('Type', ('LayerElementMaterial',)), ('TypedIndex', (0,))))))]
        objects.append(('Geometry', (geometry_id, _label(mesh.name, 'Geometry'), 'Mesh'), (
            ('Vertices', (array('d', mesh.positions),)), ('PolygonVertexIndex', (indices,)),
            ('GeometryVersion', (124,)), *layers)))
        objects.append(('Model', (model_id, _label(mesh.name, 'Model'), 'Mesh'), (
            ('Version', (232,)),
            _properties(('Lcl Translation', 'Lcl Translation', '', 'A', 0.0, 0.0, 0.0),
                        ('Lcl Rotation', 'Lcl Rotation', '', 'A', 0.0, 0.0, 0.0),
                        ('Lcl Scaling', 'Lcl Scaling', '', 'A', 1.0, 1.0, 1.0)),
            ('Culling', ('CullingOff',)))))
        links += [('OO', model_id, _Long(0)), ('OO', geometry_id, model_id)]
        for material in mesh.materials:
            # One material object serves every mesh that names the same material.
            if material not in shared:
                if any(known.name == material.name for known in shared):
                    _reject('One material name has two definitions')
                shared[material] = _Long(next_id)
                next_id += 1
                color = () if material.color is None else (
                    ('DiffuseColor', 'Color', '', 'A', *(float(c) for c in material.color)),)
                objects.append(('Material', (shared[material], _label(material.name, 'Material'), ''), (
                    ('Version', (102,)), ('ShadingModel', ('lambert',)), ('MultiLayer', (0,)),
                    _properties(*color))))
            links.append(('OO', shared[material], model_id))
    counts = {kind: sum(o[0] == kind for o in objects) for kind in ('Model', 'Geometry', 'Material')}
    roots = [
        ('FBXHeaderExtension', (), (('FBXHeaderVersion', (1003,)), ('FBXVersion', (7400,)),
                                    ('Creator', ('model-generator',)))),
        ('FileId', (FILE_ID,)), ('CreationTime', ('1970-01-01 10:00:00:000',)), ('Creator', ('model-generator',)),
        ('GlobalSettings', (), (('Version', (1000,)), _properties(
            ('UpAxis', 'int', 'Integer', '', 2), ('UpAxisSign', 'int', 'Integer', '', 1),
            ('FrontAxis', 'int', 'Integer', '', 1), ('FrontAxisSign', 'int', 'Integer', '', -1),
            ('CoordAxis', 'int', 'Integer', '', 0), ('CoordAxisSign', 'int', 'Integer', '', 1),
            ('OriginalUpAxis', 'int', 'Integer', '', 2), ('OriginalUpAxisSign', 'int', 'Integer', '', 1),
            ('UnitScaleFactor', 'double', 'Number', '', 100.0),
            ('OriginalUnitScaleFactor', 'double', 'Number', '', 100.0)))),
        ('Definitions', (), (('Version', (100,)), ('Count', (sum(counts.values()),)), *(
            ('ObjectType', (kind,), (('Count', (count,)),)) for kind, count in counts.items() if count))),
        ('Objects', (), tuple(objects)),
        ('Connections', (), tuple(('C', link) for link in links)),
    ]
    data = bytearray(MAGIC + struct.pack('<I', 7400))
    for root in roots:
        data += _node(len(data), *root)
    data += bytes(13) + FOOT_ID + bytes(4)
    data += bytes(16 - len(data) % 16)
    data += struct.pack('<I', 7400) + bytes(120) + FOOT_MAGIC
    return bytes(data)
