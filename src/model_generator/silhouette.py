"""The outline of a scene too large to reduce within its error: one coarse mesh.

Vertices that fall into one cell of a grid become one vertex at their mean.
Triangles that lose a corner this way disappear, and equal triangles are kept
once. The cell doubles until the mesh fits the stated number of triangles, so
the result shows the shape of the whole and nothing finer than its cell. It is
a view of the source, not a model of it: materials, texture coordinates and
the stated error of the reduction do not apply.
"""

from array import array
import math

from .simplify import OverBudget

BITS = 21                 # cells along one axis, and vertices of one pass
CELLS = 256               # cells along the longest side at the first pass
MIN_CELL = 0.05           # metres
PASSES = 16


def _faces(corners, triangles, faces):
    for start in range(0, len(triangles), 3):
        a, b, c = corners[triangles[start]], corners[triangles[start + 1]], corners[triangles[start + 2]]
        if a == b or b == c or a == c:
            continue
        # The facing is kept: the same corners the other way round are another triangle.
        if a < b:
            key = (a << BITS | b) << BITS | c if a < c else (c << BITS | a) << BITS | b
        else:
            key = (b << BITS | c) << BITS | a if b < c else (c << BITS | a) << BITS | b
        faces.add(key)


class _Grid:
    def __init__(self, low, cell):
        self.low, self.scale = low, 1.0 / cell
        self.cells, self.sums, self.counts = {}, array('d'), array('I')

    def vertex(self, x, y, z, weight=1):
        low, scale = self.low, self.scale
        key = (int((x - low[0]) * scale) << BITS | int((y - low[1]) * scale)) << BITS | int((z - low[2]) * scale)
        index = self.cells.get(key)
        if index is None:
            index = self.cells[key] = len(self.counts)
            if index >> BITS:
                raise OverBudget
            self.sums.extend((x * weight, y * weight, z * weight))
            self.counts.append(weight)
        else:
            base = index * 3
            self.sums[base] += x * weight
            self.sums[base + 1] += y * weight
            self.sums[base + 2] += z * weight
            self.counts[index] += weight
        return index


def outline(meshes, max_triangles, expired=None):
    """Return (positions, triangles, cell) for all `meshes` as one coarse mesh.

    `meshes` carry `positions` and `triangles` in shared coordinates. Raises
    OverBudget when `expired` says the time is spent or no cell gives a mesh
    within `max_triangles`.
    """
    meshes = [m for m in meshes if len(m.triangles)]
    if not meshes or max_triangles < 1:
        raise OverBudget
    low = [min(min(m.positions[axis::3]) for m in meshes) for axis in range(3)]
    high = [max(max(m.positions[axis::3]) for m in meshes) for axis in range(3)]
    if not all(math.isfinite(v) for v in low + high):
        raise OverBudget
    cell = max(MIN_CELL, max(h - l for h, l in zip(high, low)) / CELLS)
    grid, faces = _Grid(low, cell), set()
    for mesh in meshes:
        if expired is not None and expired():
            raise OverBudget
        points, vertex = mesh.positions, grid.vertex
        corners = [vertex(points[i], points[i + 1], points[i + 2]) for i in range(0, len(points), 3)]
        _faces(corners, mesh.triangles, faces)
    mask = (1 << BITS) - 1
    for _ in range(PASSES):
        if len(faces) <= max_triangles:
            break
        if expired is not None and expired():
            raise OverBudget
        cell *= 2
        coarse, sums, counts = _Grid(low, cell), grid.sums, grid.counts
        corners = [coarse.vertex(sums[i * 3] / n, sums[i * 3 + 1] / n, sums[i * 3 + 2] / n, n)
                   for i, n in enumerate(counts)]
        merged = set()
        _faces(corners, array('I', (v for key in faces for v in (key >> 2 * BITS, key >> BITS & mask, key & mask))),
               merged)
        grid, faces = coarse, merged
    if not faces or len(faces) > max_triangles:
        raise OverBudget
    # Only the vertices some triangle still uses are written.
    used, positions, triangles = {}, array('d'), array('I')
    for key in faces:
        for index in (key >> 2 * BITS, key >> BITS & mask, key & mask):
            place = used.get(index)
            if place is None:
                place = used[index] = len(used)
                n = grid.counts[index]
                positions.extend((grid.sums[index * 3] / n, grid.sums[index * 3 + 1] / n, grid.sums[index * 3 + 2] / n))
            triangles.append(place)
    return positions, triangles, cell
