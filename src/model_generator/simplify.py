"""Reduce the triangle count of a mesh within a stated deviation.

Edges are collapsed onto one of their own end points, so every remaining
vertex is an original vertex. A collapse is accepted only while the moved
vertex stays within `max_error` metres of the plane of every original
triangle it has absorbed. Open borders, borders between materials and seams
of the texture coordinates add their own planes, so outlines, openings,
material areas and the pieces of the unwrapping keep their shape.
Texture coordinates stay with the corners of the triangles. A collapse is
accepted only while the picture on every triangle it reshapes has slid, in
total, no further than `max_error` metres along the surface.
Collapses that would fold the surface or change its topology are skipped.
Of several triangles on the same three vertices one is kept: the result is
meant to be drawn two-sided.
"""

from array import array
from dataclasses import dataclass, field
import heapq
import math

from .limits import ReadError

GRID = 10_000        # vertices closer than 0.1 mm are one vertex
BUDGET_STEP = 256     # collapse attempts between two looks at the caller's time
STAGE_STEP = 8192     # vertices, triangles or edges of a plain pass between two looks
FOLD_COSINE = 0.2    # a triangle may not turn further than this from its facing


@dataclass(frozen=True)
class Simplified:
    positions: array
    triangles: array
    material_indices: array
    report: dict
    uvs: array = field(default_factory=lambda: array('d'))  # u, v of every triangle corner, or empty


class OverBudget(Exception):
    """The reduction was stopped: the caller's time is spent."""


def _reject(message):
    raise ReadError('simplify.input', message)


def _cross(a, b, c):
    ux, uy, uz, vx, vy, vz = b[0] - a[0], b[1] - a[1], b[2] - a[2], c[0] - a[0], c[1] - a[1], c[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


def _plane(normal, point):
    """Quadric of the squared distance to a plane, or None for a zero normal."""
    length = math.sqrt(normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2)
    if length == 0:
        return None
    x, y, z = normal[0] / length, normal[1] / length, normal[2] / length
    d = -(x * point[0] + y * point[1] + z * point[2])
    return (x * x, x * y, x * z, x * d, y * y, y * z, y * d, z * z, z * d, d * d)


def _distance(q, p):
    x, y, z = p
    return (q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x + q[4] * y * y
            + 2 * q[5] * y * z + 2 * q[6] * y + q[7] * z * z + 2 * q[8] * z + q[9])


def _slide(pu, pa, pb, tu, ta, tb, pv, tv):
    """Metres the picture slides on triangle u, a, b once corner u sits at pv with tv.

    The old picture puts the texture point tv at one place of the old triangle;
    the new one puts it at pv. Their distance is the largest slide over the new
    triangle, because corners a and b keep both their place and their picture.
    None: the old triangle shows a single line or point of the picture and tv
    is not on it.
    """
    ax, ay, bx, by = ta[0] - tu[0], ta[1] - tu[1], tb[0] - tu[0], tb[1] - tu[1]
    x, y = tv[0] - tu[0], tv[1] - tu[1]
    area = ax * by - ay * bx
    if abs(area) <= 1e-14 * (abs(ax * by) + abs(ay * bx)) or area == 0:
        return 0.0 if tv in (tu, ta, tb) else None
    wa, wb = (x * by - y * bx) / area, (ax * y - ay * x) / area
    return math.sqrt(sum((pu[k] + wa * (pa[k] - pu[k]) + wb * (pb[k] - pu[k]) - pv[k]) ** 2 for k in range(3)))


def simplify(positions, triangles, material_indices=(), max_error=0.02, locked_materials=(), expired=None, uvs=()):
    """Return the reduced mesh and a report, or refuse with a ReadError.

    `material_indices` holds one slot per triangle or is empty. Triangles of
    `locked_materials` and their vertices are left untouched. `expired` is
    asked on entry and from time to time in every pass; once it answers yes
    the work stops with OverBudget. `uvs` holds u and v of every triangle
    corner, six numbers per triangle, or is empty.
    """
    def look(step=0):
        # Every pass over the mesh looks at the time, the first time on entry.
        if expired is not None and not step % STAGE_STEP and expired():
            raise OverBudget

    look()
    faces = len(triangles) // 3
    if len(positions) % 3 or len(triangles) % 3 or not faces:
        _reject('A mesh needs whole vertices and triangles')
    if len(material_indices) not in (0, faces):
        _reject('Material assignment must cover every triangle')
    if len(uvs) not in (0, faces * 6) or not all(math.isfinite(c) for c in uvs):
        _reject('Texture coordinates must cover every triangle corner')
    if not (isinstance(max_error, (int, float)) and math.isfinite(max_error) and max_error >= 0):
        _reject('Deviation must be a finite number that is not negative')
    if not all(math.isfinite(c) for c in positions):
        _reject('Coordinates must be finite')
    count = len(positions) // 3
    if not all(0 <= i < count for i in triangles):
        _reject('Triangle refers to a missing vertex')

    # Work near the origin: quadrics lose precision at survey distances.
    centre = [(min(positions[axis::3]) + max(positions[axis::3])) / 2 for axis in range(3)]
    welded, source, points, remap = {}, [], [], []
    for start in range(0, len(positions), 3):
        look(start // 3)
        key = tuple(round(c * GRID) for c in positions[start:start + 3])
        if key not in welded:
            welded[key] = len(points)
            source.append(start)
            points.append(tuple(positions[start + axis] - centre[axis] for axis in range(3)))
        remap.append(welded[key])
    # Each table is let go as soon as its pass is over: the passes that follow
    # need the memory, and a large mesh fits the process only this way.
    del welded

    corners, materials, around, coords = [], [], [set() for _ in points], []
    seen, coincident = set(), 0
    for face in range(faces):
        look(face)
        a, b, c = (remap[i] for i in triangles[face * 3:face * 3 + 3])
        # Triangles without area carry no surface and are dropped.
        if a == b or b == c or a == c or _cross(points[a], points[b], points[c]) == (0.0, 0.0, 0.0):
            continue
        # A second triangle on the same three vertices repeats the first one,
        # usually as its back side; one triangle is kept.
        if frozenset((a, b, c)) in seen:
            coincident += 1
            continue
        seen.add(frozenset((a, b, c)))
        for vertex in (a, b, c):
            around[vertex].add(len(corners))
        corners.append([a, b, c])
        materials.append(material_indices[face] if len(material_indices) else 0)
        if len(uvs):
            coords.append([tuple(uvs[face * 6 + k:face * 6 + k + 2]) for k in (0, 2, 4)])
    del remap, seen

    quadrics = [[0.0] * 10 for _ in points]
    def absorb(vertex, quadric):
        if quadric is not None:
            target = quadrics[vertex]
            for k in range(10):
                target[k] += quadric[k]

    edges = {}
    for face, (a, b, c) in enumerate(corners):
        look(face)
        normal = _cross(points[a], points[b], points[c])
        quadric = _plane(normal, points[a])
        for vertex in (a, b, c):
            absorb(vertex, quadric)
        for u, v in ((a, b), (b, c), (c, a)):
            edges.setdefault((min(u, v), max(u, v)), []).append(face)
    def seam(u, v, first, second):
        # The two triangles disagree about the picture at an end of their edge.
        return bool(coords) and any(coords[first][corners[first].index(w)] != coords[second][corners[second].index(w)]
                                    for w in (u, v))
    locked = {v for face, slot in enumerate(materials) if slot in locked_materials for v in corners[face]}
    border = set()
    for step, ((u, v), sharing) in enumerate(edges.items()):
        look(step)
        if len(sharing) > 2:
            # More than two triangles on an edge: keep such places as they are.
            locked.update((u, v))
        elif len(sharing) == 1 or materials[sharing[0]] != materials[sharing[1]] or seam(u, v, *sharing):
            if len(sharing) == 1:
                border.update((u, v))
            for face in sharing:
                # A plane through the edge, square to the triangle, holds the outline.
                normal = _cross(points[corners[face][0]], points[corners[face][1]], points[corners[face][2]])
                edge = tuple(points[v][axis] - points[u][axis] for axis in range(3))
                side = (edge[1] * normal[2] - edge[2] * normal[1], edge[2] * normal[0] - edge[0] * normal[2],
                        edge[0] * normal[1] - edge[1] * normal[0])
                quadric = _plane(side, points[u])
                absorb(u, quadric)
                absorb(v, quadric)

    limit = max_error * max_error
    version, alive, heap = [0] * len(points), [True] * len(points), []
    def offer(u, v):
        if u in locked:
            return
        cost = _distance([p + q for p, q in zip(quadrics[u], quadrics[v])], points[v])
        if cost <= limit:
            heapq.heappush(heap, (max(cost, 0.0), u, v, version[u], version[v]))
    for step, (u, v) in enumerate(edges):
        look(step)
        offer(u, v)
        offer(v, u)
    del edges

    reached, collapses, turns = 0.0, 0, 0
    slid, slide_reached = [0.0] * len(coords), 0.0
    while heap:
        turns += 1
        if expired is not None and not turns % BUDGET_STEP and expired():
            raise OverBudget
        cost, u, v, stamp_u, stamp_v = heapq.heappop(heap)
        if not (alive[u] and alive[v]) or version[u] != stamp_u or version[v] != stamp_v:
            continue
        shared = around[u] & around[v]
        ring_u = {w for face in around[u] for w in corners[face]} - {u, v}
        ring_v = {w for face in around[v] for w in corners[face]} - {u, v}
        opposite = {w for face in shared for w in corners[face]} - {u, v}
        # The collapse keeps the surface a surface only when the two rings meet
        # exactly at the corners opposite the edge.
        if not shared or len(shared) > 2 or ring_u & ring_v != opposite or len(opposite) != len(shared):
            continue
        if u in border and len(shared) != 1:
            continue
        folded = False
        for face in around[u] - shared:
            a, b, c = (points[w] for w in corners[face])
            before = _cross(a, b, c)
            after = _cross(*(points[v] if w == u else points[w] for w in corners[face]))
            dot = before[0] * after[0] + before[1] * after[1] + before[2] * after[2]
            sizes = math.sqrt(sum(x * x for x in before) * sum(x * x for x in after))
            if sizes == 0 or dot < FOLD_COSINE * sizes:
                folded = True
                break
        if folded:
            continue
        moved = {}
        if coords:
            # A triangle that stays takes the picture of the far end from a
            # vanishing triangle of its own piece of the unwrapping.
            known = {}
            for face in shared:
                known.setdefault(coords[face][corners[face].index(u)], set()).add(coords[face][corners[face].index(v)])
            for face in around[u] - shared:
                at = corners[face].index(u)
                tu, far = coords[face][at], None
                if len(known.get(tu, ())) == 1:
                    far, = known[tu]
                    a, b = (at + 1) % 3, (at + 2) % 3
                    slide = _slide(points[u], points[corners[face][a]], points[corners[face][b]],
                                   tu, coords[face][a], coords[face][b], points[v], far)
                if far is None or slide is None or slid[face] + slide > max_error:
                    moved = None
                    break
                moved[face] = (at, far, slide)
            if moved is None:
                continue
        for face, (at, far, slide) in moved.items():
            coords[face][at] = far
            slid[face] += slide
            slide_reached = max(slide_reached, slid[face])
        for face in shared:
            for w in corners[face]:
                around[w].discard(face)
            corners[face] = None
        for face in around[u]:
            corners[face][corners[face].index(u)] = v
            around[v].add(face)
        around[u] = set()
        alive[u] = False
        quadrics[v] = [p + q for p, q in zip(quadrics[u], quadrics[v])]
        if u in border:
            border.add(v)
        version[v] += 1
        reached, collapses = max(reached, cost), collapses + 1
        for w in {w for face in around[v] for w in corners[face]} - {v}:
            offer(v, w)
            offer(w, v)

    del heap, quadrics, around, version, alive, locked, border, slid
    kept, out_positions, out_triangles, out_materials, out_uvs = {}, array('d'), array('I'), array('i'), array('d')
    seen = set()
    for face, triple in enumerate(corners):
        look(face)
        if triple is None:
            continue
        if frozenset(triple) in seen:
            coincident += 1
            continue
        seen.add(frozenset(triple))
        for vertex in triple:
            if vertex not in kept:
                kept[vertex] = len(kept)
                out_positions.extend(positions[source[vertex]:source[vertex] + 3])
            out_triangles.append(kept[vertex])
        if len(material_indices):
            out_materials.append(materials[face])
        if coords:
            for pair in coords[face]:
                out_uvs.extend(pair)
    if not out_triangles:
        _reject('The mesh has no surface')
    report = {'input_triangles': faces, 'output_triangles': len(out_triangles) // 3,
              'input_vertices': count, 'output_vertices': len(kept), 'collapses': collapses,
              'coincident_triangles': coincident,
              'max_error': max_error, 'reached_error': math.sqrt(reached)}
    if coords:
        report['texture_slide'] = slide_reached
    return Simplified(out_positions, out_triangles, out_materials, report, out_uvs)
