"""Build a triangulated terrain from surveyed heights and place a building on it.

The terrain is the Delaunay triangulation of every surveyed point and line
vertex; each vertex keeps the origin of its height. Coordinates are snapped to
0.1 mm and the triangulation runs on integers, so its predicates are exact.
The building is placed only by a stated origin, bearing and absolute elevation
of its zero level, and only when control points confirm that statement.
Nothing is fitted or guessed.
"""

from array import array
from dataclasses import dataclass, replace
import math

from .limits import Limits, ReadError

GRID = 10_000            # integer units per metre
Z_TOLERANCE = 0.001      # metres; larger disagreement at one location is a conflict
CONTROL_BASELINE = 1.0   # metres between control points needed to confirm the bearing
OVERHANG = 0.5           # metres a part of the building may reach past the edge of the survey


@dataclass(frozen=True)
class Terrain:
    positions: array     # x, y, z in metres, survey coordinates
    triangles: array     # counter-clockwise seen from above
    origins: tuple       # per vertex: (source, entity, handle, layer)
    bounds: tuple


@dataclass(frozen=True)
class Placement:
    east: float            # survey X of the model origin, metres
    north: float           # survey Y of the model origin, metres
    zero_elevation: float  # absolute elevation of model Z = 0, metres
    bearing_degrees: float # direction of model +Y, clockwise from survey +Y


@dataclass(frozen=True)
class ControlPoint:
    model: tuple   # x, y, z in model metres
    survey: tuple  # x, y, z in survey metres


@dataclass(frozen=True)
class PlacedBuilding:
    meshes: tuple        # FbxMesh with positions in survey coordinates
    bounds: tuple
    residuals: tuple     # metres, one per control point
    ground_range: tuple  # lowest and highest terrain height under the footprint
    overhang: float = 0.0  # metres the footprint reaches past the edge of the survey


def _reject(rule, message):
    raise ReadError(rule, message)


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _in_circle(a, b, c, d):
    """Positive when d lies strictly inside the circle through counter-clockwise a, b, c."""
    ax, ay, bx, by, cx, cy = a[0] - d[0], a[1] - d[1], b[0] - d[0], b[1] - d[1], c[0] - d[0], c[1] - d[1]
    return ((ax * ax + ay * ay) * (bx * cy - cx * by) - (bx * bx + by * by) * (ax * cy - cx * ay)
            + (cx * cx + cy * cy) * (ax * by - bx * ay))


def _delaunay(points):
    """Triangles over distinct integer points, as index triples, counter-clockwise."""
    count = len(points)
    low_x, low_y = min(p[0] for p in points), min(p[1] for p in points)
    span = max(max(p[0] for p in points) - low_x, max(p[1] for p in points) - low_y, 1)
    # Three far vertices enclose the points. A circle through three integer points
    # of this extent has a radius below 2 * span**3, so vertices this far away lie
    # outside every such circle and cannot change the triangulation of the points.
    far = 32 * span ** 3
    xy = [(p[0] - low_x, p[1] - low_y) for p in points] + [(-far, -far), (far, -far), (0, far)]
    vertices, neighbours = [[count, count + 1, count + 2]], [[-1, -1, -1]]
    cell = max(1, round(span / math.sqrt(count)))
    order = sorted(range(count), key=lambda i: (xy[i][0] // cell,
                                               xy[i][1] if xy[i][0] // cell % 2 == 0 else -xy[i][1]))
    last = 0
    for index in order:
        point = xy[index]
        # Walk towards the point; exact predicates make the walk terminate.
        current = last
        while True:
            a, b, c = vertices[current]
            if _orient(xy[b], xy[c], point) < 0:
                current = neighbours[current][0]
            elif _orient(xy[c], xy[a], point) < 0:
                current = neighbours[current][1]
            elif _orient(xy[a], xy[b], point) < 0:
                current = neighbours[current][2]
            else:
                break
        cavity, stack, border = {current}, [current], []
        while stack:
            triangle = stack.pop()
            corners = vertices[triangle]
            for k in range(3):
                other = neighbours[triangle][k]
                if other in cavity:
                    continue
                if other >= 0 and _in_circle(*(xy[v] for v in vertices[other]), point) > 0:
                    cavity.add(other)
                    stack.append(other)
                else:
                    border.append((corners[(k + 1) % 3], corners[(k + 2) % 3], other, triangle))
        starts, ends = {}, {}
        for a, b, other, old in border:
            new = len(vertices)
            vertices.append([index, a, b])
            neighbours.append([other, -1, -1])
            starts[a], ends[b] = new, new
            if other >= 0:
                neighbours[other][neighbours[other].index(old)] = new
        for a, b, _, _ in border:
            new = starts[a]
            neighbours[new][1], neighbours[new][2] = starts[b], ends[a]
        for triangle in cavity:
            vertices[triangle] = neighbours[triangle] = None
        last = new
    return [tuple(t) for t in vertices if t is not None and max(t) < count]


def build_terrain(survey, limits=None, accept_labels=False):
    """Return the terrain of a HeightSurvey, or refuse with a ReadError.

    Heights read from numeric labels are a guess about the drawing: detail
    sheets of a real project gave the numbers of their details as heights.
    Such a survey builds a surface only after the caller states that a person
    confirmed the labels.
    """
    limits = limits or Limits()
    if not accept_labels and (any(s.source == 'label' for s in survey.samples)
                              or any(line.source == 'label' for line in survey.lines)):
        _reject('terrain.labels', 'Heights come from labels and were not confirmed')
    located = {}

    def add(x, y, z, origin):
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            _reject('terrain.value', 'Height is not a finite number')
        key = (round(x * GRID), round(y * GRID))
        known = located.get(key)
        if known is None:
            if len(located) >= limits.terrain_points:
                _reject('terrain.budget', 'Terrain point budget exceeded')
            located[key] = (x, y, z, origin)
        elif abs(known[2] - z) > Z_TOLERANCE:
            _reject('terrain.conflict', 'Two different heights at one location')

    for sample in survey.samples:
        add(sample.x, sample.y, sample.z, (sample.source, sample.entity, sample.handle, sample.layer))
    for line in survey.lines:
        for x, y, z in line.points:
            add(x, y, z, (line.source, line.entity, line.handle, line.layer))
    if not located:
        _reject('terrain.heights', 'The drawing has no heights')
    keys = list(located)
    triangles = _delaunay(keys) if len(keys) >= 3 else []
    if not triangles:
        _reject('terrain.degenerate', 'Heights do not span a surface')
    values = list(located.values())
    positions = array('d', (c for x, y, z, _ in values for c in (x, y, z)))
    bounds = tuple((min(positions[axis::3]), max(positions[axis::3])) for axis in range(3))
    return Terrain(positions, array('I', (v for t in triangles for v in t)),
                   tuple(origin for *_, origin in values), bounds)


class TerrainIndex:
    """Height of the terrain surface at a plan location."""

    def __init__(self, terrain):
        self._positions, self._triangles = terrain.positions, terrain.triangles
        (self._x, self._high_x), (self._y, self._high_y) = terrain.bounds[0], terrain.bounds[1]
        self._size = max(1, math.isqrt(len(terrain.triangles) // 3))
        self._step_x = (self._high_x - self._x) / self._size or 1.0
        self._step_y = (self._high_y - self._y) / self._size or 1.0
        self._cells = {}
        for start in range(0, len(self._triangles), 3):
            corners = [self._corner(self._triangles[start + k]) for k in range(3)]
            columns = [self._column(c[0], self._x, self._step_x) for c in corners]
            rows = [self._column(c[1], self._y, self._step_y) for c in corners]
            for column in range(min(columns), max(columns) + 1):
                for row in range(min(rows), max(rows) + 1):
                    self._cells.setdefault((column, row), []).append(start)

    def _corner(self, vertex):
        return self._positions[vertex * 3:vertex * 3 + 3]

    def _column(self, value, low, step):
        return min(self._size - 1, max(0, int((value - low) / step)))

    def height(self, x, y):
        """Interpolated height in metres, or None outside the surveyed surface."""
        if not (self._x <= x <= self._high_x and self._y <= y <= self._high_y):
            return None
        cell = (self._column(x, self._x, self._step_x), self._column(y, self._y, self._step_y))
        for start in self._cells.get(cell, ()):
            a, b, c = (self._corner(self._triangles[start + k]) for k in range(3))
            area = _orient(a, b, c)
            weights = (_orient(b, c, (x, y)) / area, _orient(c, a, (x, y)) / area, _orient(a, b, (x, y)) / area)
            if min(weights) >= -1e-9:
                return weights[0] * a[2] + weights[1] * b[2] + weights[2] * c[2]
        return None


def _hull(points):
    points = sorted(set(points))
    if len(points) < 3:
        return points
    def half(sequence):
        chain = []
        for point in sequence:
            while len(chain) >= 2 and _orient(chain[-2], chain[-1], point) <= 0:
                chain.pop()
            chain.append(point)
        return chain[:-1]
    return half(points) + half(reversed(points))


def _beyond(edge, point):
    """Metres from a plan point to the closed edge it lies outside of."""
    def reach(a, b):
        along = ((point[0] - a[0]) * (b[0] - a[0]) + (point[1] - a[1]) * (b[1] - a[1])) / math.dist(a, b) ** 2
        along = min(1.0, max(0.0, along))
        return math.dist(point, (a[0] + along * (b[0] - a[0]), a[1] + along * (b[1] - a[1])))
    return min(reach(a, b) for a, b in zip(edge, edge[1:] + edge[:1]))


def place_building(scene, terrain, placement, controls, tolerance=0.01, overhang=OVERHANG):
    """Move the building into survey coordinates, or refuse with a ReadError.

    The placement is taken as stated and must be confirmed by at least two
    control points whose plan distance is enough to confirm the bearing.
    A real building reaches a little past the edge of its ground model with
    eaves and canopies; such a reach within `overhang` is measured and kept,
    a longer one is a building that stands outside the survey.
    """
    stated = (placement.east, placement.north, placement.zero_elevation, placement.bearing_degrees)
    numbers = [*stated, tolerance, overhang] + [c for control in controls for c in (*control.model, *control.survey)]
    if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in numbers) or tolerance <= 0 or overhang < 0:
        _reject('placement.value', 'Placement is not a finite number')
    if not scene.meshes:
        _reject('placement.value', 'The building has no geometry')
    bearing = math.radians(placement.bearing_degrees)
    cos, sin = math.cos(bearing), math.sin(bearing)

    def move(x, y, z):
        return (placement.east + x * cos + y * sin, placement.north - x * sin + y * cos,
                placement.zero_elevation + z)

    baseline = max((math.dist(a.model[:2], b.model[:2]) for a in controls for b in controls), default=0.0)
    if len(controls) < 2 or baseline < CONTROL_BASELINE:
        _reject('placement.unverified', 'Placement is not confirmed by control points')
    residuals = tuple(math.dist(move(*control.model), control.survey) for control in controls)
    if max(residuals) > tolerance:
        _reject('placement.control', 'Control points contradict the stated placement')

    meshes, plan = [], []
    for mesh in scene.meshes:
        moved = array('d')
        for start in range(0, len(mesh.positions), 3):
            point = move(*mesh.positions[start:start + 3])
            moved.extend(point)
            plan.append(point[:2])
        meshes.append(replace(mesh, positions=moved))
    index = TerrainIndex(terrain)
    footprint = _hull(plan)
    heights = [index.height(x, y) for x, y in footprint]
    ground = [height for height in heights if height is not None]
    reach = 0.0
    if len(ground) < len(heights):
        edge = _hull([(terrain.positions[i], terrain.positions[i + 1]) for i in range(0, len(terrain.positions), 3)])
        reach = max(_beyond(edge, point) for point, height in zip(footprint, heights) if height is None)
    if not ground or reach > overhang:
        _reject('placement.outside', 'The building stands outside the surveyed terrain')
    bounds = tuple((min(min(m.positions[axis::3]) for m in meshes),
                    max(max(m.positions[axis::3]) for m in meshes)) for axis in range(3))
    return PlacedBuilding(tuple(meshes), bounds, residuals, (min(ground), max(ground)), reach)
