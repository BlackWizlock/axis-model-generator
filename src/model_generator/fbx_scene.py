"""Read placed triangle meshes from a binary FBX scene, in metres and Z-up.

The reader composes the documented FBX node transform (translation, rotation
offset and pivot, pre and post rotation, rotation order, scaling offset and
pivot, scaling), walks the model hierarchy, applies geometric transforms to
geometry only and converts the declared scene axes to X right, Y back, Z up.
Convex polygons are split into a fan. Deformers, skeletons, animation,
concave polygons and scale inheritance other than the plain matrix product
are refused. Parsed source nodes (including UVs and texture bindings) are
retained for later processing; nothing is exported or simplified here.
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
    mirrored: bool = False


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


IDENTITY = ((1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0))
# FBX RotationOrder enum; each entry lists the axes in order of application.
ROTATION_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 2, 0), (1, 0, 2), (2, 0, 1), (2, 1, 0))
HIERARCHY_DEPTH = 64
PLACEABLE = ('Mesh', 'Null', 'Camera', 'Light')


def _linear(a, b):
    return tuple(sum(a[row * 3 + k] * b[k * 3 + col] for k in range(3))
                 for row in range(3) for col in range(3))


def _apply(matrix, vector):
    return tuple(sum(matrix[row * 3 + k] * vector[k] for k in range(3)) for row in range(3))


def _compose(outer, inner):
    """Affine product: the inner transform is applied first."""
    moved = _apply(outer[0], inner[1])
    return _linear(outer[0], inner[0]), tuple(moved[i] + outer[1][i] for i in range(3))


def _determinant(m):
    return (m[0] * (m[4] * m[8] - m[5] * m[7]) - m[1] * (m[3] * m[8] - m[5] * m[6])
            + m[2] * (m[3] * m[7] - m[4] * m[6]))


def _axis_rotation(axis, degrees):
    if degrees == 0:
        return IDENTITY[0]
    angle = math.radians(degrees)
    c, s = math.cos(angle), math.sin(angle)
    return ((1, 0, 0, 0, c, -s, 0, s, c), (c, 0, s, 0, 1, 0, -s, 0, c),
            (c, -s, 0, s, c, 0, 0, 0, 1))[axis]


def _euler(degrees, order=ROTATION_ORDERS[0]):
    result = IDENTITY[0]
    for axis in order:
        result = _linear(_axis_rotation(axis, degrees[axis]), result)
    return result


def _transpose(m):
    return (m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8])


def _vector(props, key, identity):
    values = props.get(key, identity)
    if not _finite(values, 3):
        _reject('Invalid model transform: ' + key)
    return tuple(float(v) for v in values)


def _integer(props, key, default):
    values = props.get(key, [default])
    if len(values) != 1 or type(values[0]) is not int:
        _reject('Invalid model property: ' + key)
    return values[0]


def _scaling(values):
    return (values[0], 0.0, 0.0, 0.0, values[1], 0.0, 0.0, 0.0, values[2])


def _local(props):
    """Local node transform and geometric transform of one model.

    T * Roff * Rp * Rpre * R * Rpost^-1 * Rp^-1 * Soff * Sp * S * Sp^-1.
    Pre and post rotation and the rotation order apply only while
    RotationActive is set; otherwise the rotation is plain XYZ.
    """
    zero, one = [0, 0, 0], [1, 1, 1]
    active = _integer(props, 'RotationActive', 0)
    order = _integer(props, 'RotationOrder', 0) if active else 0
    if active not in (0, 1) or not 0 <= order < len(ROTATION_ORDERS):
        _reject('Unsupported rotation order')
    rotation = _euler(_vector(props, 'Lcl Rotation', zero), ROTATION_ORDERS[order])
    pre = _euler(_vector(props, 'PreRotation', zero)) if active else IDENTITY[0]
    post = _euler(_vector(props, 'PostRotation', zero)) if active else IDENTITY[0]
    scale = _vector(props, 'Lcl Scaling', one)
    pivot_r, pivot_s = _vector(props, 'RotationPivot', zero), _vector(props, 'ScalingPivot', zero)
    steps = ((IDENTITY[0], _vector(props, 'Lcl Translation', zero)),
             (IDENTITY[0], _vector(props, 'RotationOffset', zero)),
             (IDENTITY[0], pivot_r), (pre, IDENTITY[1]), (rotation, IDENTITY[1]),
             (_transpose(post), IDENTITY[1]), (IDENTITY[0], tuple(-v for v in pivot_r)),
             (IDENTITY[0], _vector(props, 'ScalingOffset', zero)), (IDENTITY[0], pivot_s),
             (_scaling(scale), IDENTITY[1]), (IDENTITY[0], tuple(-v for v in pivot_s)))
    local = IDENTITY
    for step in steps:
        if step != IDENTITY:
            local = _compose(local, step)
    geometric = IDENTITY
    for step in ((IDENTITY[0], _vector(props, 'GeometricTranslation', zero)),
                 (_euler(_vector(props, 'GeometricRotation', zero)), IDENTITY[1]),
                 (_scaling(_vector(props, 'GeometricScaling', one)), IDENTITY[1])):
        if step != IDENTITY:
            geometric = _compose(geometric, step)
    return local, geometric, scale == (1.0, 1.0, 1.0)


def _axes(settings):
    """Matrix from the declared scene axes to X right, Y back, Z up."""
    values = {}
    for key in ('UpAxis', 'UpAxisSign', 'FrontAxis', 'FrontAxisSign', 'CoordAxis', 'CoordAxisSign'):
        value = settings.get(key)
        if value is None or len(value) != 1 or type(value[0]) is not int:
            _reject('Explicit scene axes are required')
        values[key] = value[0]
    axes = [values[k] for k in ('CoordAxis', 'FrontAxis', 'UpAxis')]
    signs = [values[k] for k in ('CoordAxisSign', 'FrontAxisSign', 'UpAxisSign')]
    if sorted(axes) != [0, 1, 2] or any(sign not in (1, -1) for sign in signs):
        _reject('Scene axes are not a signed permutation')
    matrix = [0.0] * 9
    matrix[axes[0]] = float(signs[0])
    matrix[3 + axes[1]] = float(-signs[1])
    matrix[6 + axes[2]] = float(signs[2])
    if _determinant(matrix) != 1:
        _reject('Left-handed scene axes are unsupported')
    return tuple(matrix)


def _triangulate(vertices, indices):
    """Triangles and the triangle count of each polygon; convex polygons become a fan."""
    triangles, counts, polygon = array('I'), array('I'), []
    for encoded in indices:
        polygon.append(-encoded - 1 if encoded < 0 else encoded)
        if encoded >= 0:
            continue
        if len(polygon) < 3:
            _reject('Polygon with fewer than three vertices')
        if len(polygon) > 3:
            points = [vertices[i * 3:i * 3 + 3] for i in polygon]
            normal = [0.0, 0.0, 0.0]
            for a, b in zip(points, points[1:] + points[:1]):
                normal[0] += (a[1] - b[1]) * (a[2] + b[2])
                normal[1] += (a[2] - b[2]) * (a[0] + b[0])
                normal[2] += (a[0] - b[0]) * (a[1] + b[1])
            size = math.sqrt(sum(v * v for v in normal))
            if not math.isfinite(size) or size == 0:
                _reject('Degenerate polygon')
            count = len(points)
            for i in range(count):
                a, b, c = points[i], points[(i + 1) % count], points[(i + 2) % count]
                u, v = [b[k] - a[k] for k in range(3)], [c[k] - b[k] for k in range(3)]
                turn = ((u[1] * v[2] - u[2] * v[1]) * normal[0] + (u[2] * v[0] - u[0] * v[2]) * normal[1]
                        + (u[0] * v[1] - u[1] * v[0]) * normal[2])
                # A reflex corner turns against the polygon normal; a fan would leave the outline.
                if turn < -1e-9 * size * size:
                    _reject('Concave polygon is unsupported')
        for i in range(1, len(polygon) - 1):
            triangles.extend((polygon[0], polygon[i], polygon[i + 1]))
        counts.append(len(polygon) - 2)
        polygon = []
    return triangles, counts


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
    """Return placed geometry in metres, Z-up, without guessing units or recentering.

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
    # Polygons are split below; triangulation is a rule of the result, not of this input.
    if any(f.status == 'fail' and f.rule_id != 'fbx.triangulation' for f in findings):
        _reject('Scene failed structural inspection')
    unit = summary['unit_meters']
    if unit is None or not math.isfinite(unit) or unit <= 0:
        _reject('Explicit units are required')
    settings = _properties(next(n for n in roots if n.name == 'GlobalSettings'))
    axes = _axes(settings)
    objects = next(n for n in roots if n.name == 'Objects')
    by_id = {n.props[0]: n for n in objects.children}
    if any(n.name not in ('Geometry', 'Model', 'Material', 'Texture', 'Video', 'NodeAttribute')
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
    defaults = _template(roots)
    nodes = {}
    for model_id, model in by_id.items():
        if model.name != 'Model':
            continue
        if len(model.props) != 3 or model.props[2] not in PLACEABLE:
            _reject('Unsupported model class')
        parents = outgoing[model_id]
        if len(parents) > 1 or any(target != 0 and by_id[target].name != 'Model' for target in parents):
            _reject('Ambiguous model parent')
        props = defaults | _properties(model)
        inherit = _integer(props, 'InheritType', 0)
        if inherit not in (0, 1, 2):
            _reject('Unsupported scale inheritance')
        nodes[model_id] = (parents[0] if parents else 0, inherit, *_local(props))
    world = {0: (IDENTITY, True)}
    def placed_transform(model_id):
        chain = []
        while model_id not in world:
            if model_id in chain or len(chain) >= HIERARCHY_DEPTH:
                _reject('Model hierarchy is cyclic or too deep')
            chain.append(model_id)
            model_id = nodes[model_id][0]
        for current in reversed(chain):
            parent, inherit, local, geometric, unscaled = nodes[current]
            matrix, plain = world[parent]
            # Types 0 and 2 differ from the matrix product only under an ancestor scale.
            if inherit != 1 and not plain:
                _reject('Unsupported scale inheritance')
            world[current] = (_compose(matrix, local), plain and unscaled)
        return world[chain[0]][0] if chain else world[model_id][0]
    meshes, placed, used_bytes = [], set(), 0
    minimum, maximum = [math.inf] * 3, [-math.inf] * 3
    for model_id, node in nodes.items():
        model = by_id[model_id]
        matrix = placed_transform(model_id)
        geometries = [source for source in incoming[model_id] if by_id[source].name == 'Geometry']
        if model.props[2] != 'Mesh':
            if geometries:
                _reject('Geometry attached to a model that is not a mesh')
            continue
        slots = tuple(source for source in incoming[model_id] if by_id[source].name == 'Material')
        if len(geometries) != 1:
            _reject('Each mesh model must reference exactly one geometry')
        geometry_id = geometries[0]
        geometry = by_id[geometry_id]
        vertices = geometry.child('Vertices').props[0]
        indices = geometry.child('PolygonVertexIndex').props[0]
        used_bytes += len(vertices) * 8 + len(indices) * 8
        if used_bytes > limits.fbx_array_bytes:
            raise ReadError('fbx.budget', 'Placed geometry budget exceeded')
        linear, offset = _compose((axes, IDENTITY[1]), _compose(matrix, node[3]))
        orientation = _determinant(linear)
        if not math.isfinite(orientation) or orientation == 0:
            _reject('Singular model transform')
        positions = array('d')
        plain = linear == IDENTITY[0]
        for start in range(0, len(vertices), 3):
            point = vertices[start:start + 3]
            # A pure translation keeps the exact sums of the source coordinates.
            moved = point if plain else _apply(linear, point)
            for axis in range(3):
                value = (moved[axis] + offset[axis]) * unit
                if not math.isfinite(value):
                    _reject('Placed coordinate overflow')
                positions.append(value)
                minimum[axis] = min(minimum[axis], value)
                maximum[axis] = max(maximum[axis], value)
        triangles, counts = _triangulate(vertices, indices)
        mirrored = orientation < 0
        if mirrored:
            # A reflection reverses the facing; swap two corners to keep it.
            for start in range(0, len(triangles), 3):
                triangles[start + 1], triangles[start + 2] = triangles[start + 2], triangles[start + 1]
        assignments = _material_indices(geometry, slots, len(counts))
        if len(assignments) > 1 and len(triangles) // 3 != len(counts):
            assignments = array('i', (slot for slot, count in zip(assignments, counts)
                                      for _ in range(count)))
        meshes.append(FbxMesh(model_id, geometry_id, positions, triangles, slots, assignments, mirrored))
        placed.add(geometry_id)
    if placed != {i for i, n in by_id.items() if n.name == 'Geometry'} or not meshes:
        _reject('Every geometry must have a placement')
    return FbxScene(tuple(meshes), (tuple(minimum), tuple(maximum)), unit, tuple(roots))
