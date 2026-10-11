"""Read flat, translated triangle meshes from an explicit Z-up FBX scene.

This initial reader deliberately refuses rotations, pivots, hierarchy and
deformers. It retains parsed source nodes (including UVs and texture bindings)
for later processing; it does not export or simplify a model.
"""

from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass
import math

from .fbx_inspection import inspect_fbx
from .limits import Limits, ReadError


@dataclass(frozen=True)
class FbxMesh:
    model_id: int
    geometry_id: int
    positions: array
    triangles: array
    material_ids: tuple
    material_indices: array


@dataclass(frozen=True)
class FbxScene:
    meshes: tuple
    bounds: tuple
    unit_meters: float
    source_nodes: tuple


def _reject(message):
    raise ReadError('fbx.scene_unsupported', message)


def _properties(node):
    container = node.child('Properties70') if node else None
    result = {}
    for prop in container.children if container else ():
        if prop.name != 'P':
            continue
        if len(prop.props) < 4 or not isinstance(prop.props[0], str):
            _reject('Malformed scene property')
        key = prop.props[0]
        if key in result:
            _reject('Duplicate scene property')
        result[key] = prop.props[4:]
    return result


def _finite(values, length):
    return (len(values) == length and
            all(type(v) in (int, float) and math.isfinite(v) for v in values))


def _template(roots):
    definitions = next((n for n in roots if n.name == 'Definitions'), None)
    templates = [template for obj in definitions.children if obj.name == 'ObjectType'
                 and obj.props == ['Model'] for template in obj.children
                 if template.name == 'PropertyTemplate'] if definitions else []
    if len(templates) > 1:
        _reject('Ambiguous model defaults')
    return _properties(templates[0]) if templates else {}


def _translation(model, defaults):
    props = defaults | _properties(model)
    for key, identity in (
            ('Lcl Rotation', [0, 0, 0]), ('Lcl Scaling', [1, 1, 1]),
            ('PreRotation', [0, 0, 0]), ('PostRotation', [0, 0, 0]),
            ('RotationPivot', [0, 0, 0]), ('RotationOffset', [0, 0, 0]),
            ('ScalingPivot', [0, 0, 0]), ('ScalingOffset', [0, 0, 0]),
            ('GeometricTranslation', [0, 0, 0]), ('GeometricRotation', [0, 0, 0]),
            ('GeometricScaling', [1, 1, 1])):
        values = props.get(key, identity)
        if not _finite(values, 3) or values != identity:
            _reject('Unsupported model transform: ' + key)
    translation = props.get('Lcl Translation', [0, 0, 0])
    if not _finite(translation, 3):
        _reject('Invalid model translation')
    return translation


def _material_indices(geometry, slots, faces):
    layers = [n for n in geometry.children if n.name == 'LayerElementMaterial']
    if not layers:
        # Missing face assignments remain unknown, even with one material slot.
        return array('i')
    if len(layers) != 1:
        _reject('Multiple material layers are unsupported')
    layer = layers[0]
    def value(name):
        node = layer.child(name)
        if node is None or len(node.props) != 1:
            _reject('Incomplete material layer')
        return node.props[0]
    mapping = value('MappingInformationType')
    if value('ReferenceInformationType') != 'IndexToDirect':
        _reject('Unsupported material reference')
    indices = value('Materials')
    if mapping not in ('AllSame', 'ByPolygon'):
        _reject('Unsupported material mapping')
    expected = 1 if mapping == 'AllSame' else faces
    if (not isinstance(indices, (list, tuple, array)) or len(indices) != expected
            or any(type(i) is not int or not 0 <= i < len(slots) for i in indices)):
        _reject('Invalid material slot assignment')
    return array('i', indices)


def read_scene(roots, limits=None):
    """Return placed geometry in metres without guessing units or recentering.

    Call parse_fbx with the same Limits before this function for wire budgets.
    Material slots follow connection order; their source layers and UV records
    are retained unchanged in source_nodes. No normative readiness is implied.
    """
    limits = limits or Limits()
    authoritative = {'Properties70', 'Vertices', 'PolygonVertexIndex',
                     'MappingInformationType', 'ReferenceInformationType', 'Materials'}
    def unique_nodes(nodes):
        for node in nodes:
            names = Counter(child.name for child in node.children)
            if any(names[name] > 1 for name in authoritative):
                _reject('Ambiguous authoritative scene node')
            unique_nodes(node.children)
    unique_nodes(roots)
    if sum(n.name == 'Definitions' for n in roots) > 1:
        _reject('Ambiguous definitions')
    if any(sum(n.name == name for n in roots) != 1
           for name in ('GlobalSettings', 'Objects', 'Connections')):
        _reject('Required scene sections must be unique')
    summary, findings = inspect_fbx(roots, 'scene.fbx')
    if any(f.status == 'fail' for f in findings):
        _reject('Scene failed structural inspection')
    unit = summary['unit_meters']
    if unit is None or not math.isfinite(unit) or unit <= 0:
        _reject('Explicit units are required')
    settings = _properties(next(n for n in roots if n.name == 'GlobalSettings'))
    expected_axes = {'UpAxis': 2, 'UpAxisSign': 1, 'FrontAxis': 1,
                     'FrontAxisSign': -1, 'CoordAxis': 0, 'CoordAxisSign': 1}
    if any(settings.get(key) != [value] or type(settings[key][0]) is not int
           for key, value in expected_axes.items()):
        _reject('Only explicit Z-up scene axes are supported')
    objects = next(n for n in roots if n.name == 'Objects')
    by_id = {n.props[0]: n for n in objects.children}
    if any(n.name not in ('Geometry', 'Model', 'Material', 'Texture', 'Video')
           for n in objects.children):
        _reject('Unsupported scene object')
    connections = next(n for n in roots if n.name == 'Connections')
    links = [tuple(n.props) for n in connections.children if n.name == 'C']
    for link, count in Counter(links).items():
        if link[0] == 'OO' and (by_id[link[1]].name in ('Geometry', 'Material', 'Model')):
            if count > 1:
                _reject('Duplicate placement or material slot connection')
    incoming, outgoing = defaultdict(list), defaultdict(list)
    for kind, source, target, *rest in links:
        if kind == 'OO':
            incoming[target].append(source)
            outgoing[source].append(target)
    meshes, placed, used_bytes = [], set(), 0
    minimum, maximum = [math.inf] * 3, [-math.inf] * 3
    defaults = _template(roots)
    for model_id, model in by_id.items():
        if model.name != 'Model':
            continue
        if len(model.props) != 3 or model.props[2] != 'Mesh':
            _reject('Only mesh models are supported')
        parents = outgoing[model_id]
        if any(target != 0 for target in parents) or len(parents) > 1:
            _reject('Model hierarchy is unsupported')
        translation = _translation(model, defaults)
        slots = tuple(source for source in incoming[model_id] if by_id[source].name == 'Material')
        geometries = [source for source in incoming[model_id] if by_id[source].name == 'Geometry']
        if len(geometries) != 1:
            _reject('Each mesh model must reference exactly one geometry')
        geometry_id = geometries[0]
        geometry = by_id[geometry_id]
        vertices = geometry.child('Vertices').props[0]
        indices = geometry.child('PolygonVertexIndex').props[0]
        used_bytes += len(vertices) * 8 + len(indices) * 8
        if used_bytes > limits.fbx_array_bytes:
            raise ReadError('fbx.budget', 'Placed geometry budget exceeded')
        positions = array('d')
        for offset in range(0, len(vertices), 3):
            for axis in range(3):
                value = (vertices[offset + axis] + translation[axis]) * unit
                if not math.isfinite(value):
                    _reject('Placed coordinate overflow')
                positions.append(value)
                minimum[axis] = min(minimum[axis], value)
                maximum[axis] = max(maximum[axis], value)
        triangles = array('I', (-i - 1 if i < 0 else i for i in indices))
        assignments = _material_indices(geometry, slots, len(indices) // 3)
        meshes.append(FbxMesh(model_id, geometry_id, positions, triangles, slots, assignments))
        placed.add(geometry_id)
    if placed != {i for i, n in by_id.items() if n.name == 'Geometry'} or not meshes:
        _reject('Every geometry must have a placement')
    return FbxScene(tuple(meshes), (tuple(minimum), tuple(maximum)), unit, tuple(roots))
