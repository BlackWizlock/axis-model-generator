from array import array
import math
import random
import tracemalloc
import unittest

from model_generator.limits import ReadError
from model_generator.simplify import BUDGET_STEP, STAGE_STEP, OverBudget, simplify


def mesh(points, faces):
    return array('d', (c for p in points for c in p)), array('I', (i for f in faces for i in f))


def sheet(columns, rows, place, skip=lambda column, row: False):
    """A grid of quads split into triangles; `place` maps grid to space."""
    points = [place(column, row) for row in range(rows + 1) for column in range(columns + 1)]
    faces = []
    for row in range(rows):
        for column in range(columns):
            if skip(column, row):
                continue
            a = row * (columns + 1) + column
            b, c, d = a + 1, a + columns + 2, a + columns + 1
            faces += [(a, b, c), (a, c, d)]
    return points, faces


def join(*parts):
    points, faces = [], []
    for part_points, part_faces in parts:
        faces += [tuple(i + len(points) for i in face) for face in part_faces]
        points += part_points
    return points, faces


def triples(positions, triangles):
    points = [tuple(positions[i:i + 3]) for i in range(0, len(positions), 3)]
    return [tuple(points[i] for i in triangles[k:k + 3]) for k in range(0, len(triangles), 3)]


def corner_uvs(positions, triangles, picture):
    """u, v of every triangle corner for a picture given as a function of the place."""
    return array('d', (c for i in triangles for c in picture(*positions[i * 3:i * 3 + 3])))


def cross(a, b, c):
    u, v = [b[k] - a[k] for k in range(3)], [c[k] - a[k] for k in range(3)]
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def area(faces):
    return sum(math.sqrt(sum(x * x for x in cross(*face))) / 2 for face in faces)


def volume(faces):
    return sum(sum(a[k] * n for k, n in enumerate(cross((0, 0, 0), b, c))) for a, b, c in faces) / 6


def bounds(positions):
    return [(min(positions[k::3]), max(positions[k::3])) for k in range(3)]


def open_edges(triangles):
    edges = {}
    for k in range(0, len(triangles), 3):
        a, b, c = triangles[k:k + 3]
        for edge in ((a, b), (b, c), (c, a)):
            edges[tuple(sorted(edge))] = edges.get(tuple(sorted(edge)), 0) + 1
    return [edge for edge, count in edges.items() if count == 1]


def distance(point, face):
    """Distance from a point to a triangle."""
    a, b, c = face
    ab, ac, ap = [[q[k] - a[k] for k in range(3)] for q in (b, c, point)]
    dot = lambda u, v: sum(x * y for x, y in zip(u, v))
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return math.dist(point, a)
    bp = [point[k] - b[k] for k in range(3)]
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return math.dist(point, b)
    cp = [point[k] - c[k] for k in range(3)]
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return math.dist(point, c)
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        t = d1 / (d1 - d3)
        return math.dist(point, [a[k] + t * ab[k] for k in range(3)])
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        t = d2 / (d2 - d6)
        return math.dist(point, [a[k] + t * ac[k] for k in range(3)])
    if va <= 0 and d4 - d3 >= 0 and d5 - d6 >= 0:
        t = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        return math.dist(point, [b[k] + t * (c[k] - b[k]) for k in range(3)])
    v, w = vb / (va + vb + vc), vc / (va + vb + vc)
    return math.dist(point, [a[k] + ab[k] * v + ac[k] * w for k in range(3)])


def deviation(positions, result):
    """Largest distance from an original vertex to the reduced surface."""
    faces = triples(result.positions, result.triangles)
    return max(min(distance(tuple(positions[i:i + 3]), face) for face in faces)
               for i in range(0, len(positions), 3))


def box(size, cells):
    sx, sy, sz = size
    return join(sheet(cells, cells, lambda i, j: (sx * i / cells, sy * (cells - j) / cells, 0.0)),
                sheet(cells, cells, lambda i, j: (sx * i / cells, sy * j / cells, sz)),
                sheet(cells, cells, lambda i, j: (sx * i / cells, 0.0, sz * j / cells)),
                sheet(cells, cells, lambda i, j: (sx * (cells - i) / cells, sy, sz * j / cells)),
                sheet(cells, cells, lambda i, j: (0.0, sy * (cells - i) / cells, sz * j / cells)),
                sheet(cells, cells, lambda i, j: (sx, sy * i / cells, sz * j / cells)))


class SimplifyTests(unittest.TestCase):
    def test_tables_of_finished_passes_do_not_stay_until_the_end(self):
        # A wavy sheet of separate triangles: nothing welds away and nothing
        # collapses, so every table reaches its full size.
        positions, triangles = array('d'), array('I')
        for column in range(60):
            for row in range(50):
                for face in (((0, 0), (1, 0), (0, 1)), ((1, 0), (1, 1), (0, 1))):
                    for dx, dy in face:
                        x, y = column * 3 + dx, row * 3 + dy
                        positions.extend((x, y, math.sin(x * 1.7) * math.cos(y * 2.3)))
                        triangles.append(len(triangles))
        tracemalloc.start()
        try:
            result = simplify(positions, triangles, max_error=0.02)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result.report['output_triangles'], 6000)
        # 3.4 KiB a triangle with every table kept to the end, 2.5 KiB without.
        self.assertLess(peak / 6000, 3000)

    def test_flat_wall_becomes_two_triangles(self):
        positions, triangles = mesh(*sheet(20, 12, lambda i, j: (1000 + i * 0.5, 7.0, j * 0.25)))
        result = simplify(positions, triangles, max_error=0.001)
        self.assertEqual((result.report['input_triangles'], result.report['output_triangles']), (480, 2))
        self.assertEqual(bounds(result.positions), bounds(positions))
        self.assertAlmostEqual(area(triples(result.positions, result.triangles)), 30.0, places=9)
        self.assertTrue(all(cross(*face)[1] < 0 for face in triples(result.positions, result.triangles)))
        self.assertLess(result.report['reached_error'], 1e-6)

    def test_box_keeps_corners_volume_and_closed_surface(self):
        positions, triangles = mesh(*box((12.0, 8.0, 30.0), 6))
        result = simplify(positions, triangles, max_error=0.001)
        faces = triples(result.positions, result.triangles)
        self.assertEqual(len(faces), 12)
        self.assertEqual(len(result.positions) // 3, 8)
        self.assertAlmostEqual(volume(faces), 12 * 8 * 30, places=6)
        self.assertEqual(open_edges(result.triangles), [])
        self.assertEqual(bounds(result.positions), [(0.0, 12.0), (0.0, 8.0), (0.0, 30.0)])

    def test_opening_in_a_wall_keeps_its_outline(self):
        hole = lambda column, row: 8 <= column < 14 and 4 <= row < 10
        positions, triangles = mesh(*sheet(24, 16, lambda i, j: (i * 0.25, 0.0, j * 0.25), hole))
        result = simplify(positions, triangles, max_error=0.001)
        faces = triples(result.positions, result.triangles)
        self.assertAlmostEqual(area(faces), 6 * 4 - 1.5 * 1.5, places=9)
        self.assertLess(len(faces), 20)
        corners = {tuple(result.positions[i:i + 3]) for i in range(0, len(result.positions), 3)}
        self.assertEqual(corners, {(0.0, 0.0, 0.0), (6.0, 0.0, 0.0), (6.0, 0.0, 4.0), (0.0, 0.0, 4.0),
                                   (2.0, 0.0, 1.0), (3.5, 0.0, 1.0), (3.5, 0.0, 2.5), (2.0, 0.0, 2.5)})
        self.assertEqual(len(open_edges(result.triangles)), 8)
        # No triangle covers the opening.
        self.assertTrue(all(distance((2.75, 0.0, 1.75), face) > 0.7 for face in faces))

    def test_material_areas_and_locked_materials_are_kept(self):
        positions, triangles = mesh(*sheet(12, 8, lambda i, j: (i * 0.5, j * 0.5, 0.0)))
        slots = array('i', (0 if column < 5 else 1 for row in range(8) for column in range(12) for _ in range(2)))
        result = simplify(positions, triangles, slots, max_error=0.001)
        faces = triples(result.positions, result.triangles)
        self.assertEqual(len(result.material_indices), len(faces))
        for slot, expected in ((0, 10.0), (1, 14.0)):
            part = [face for face, used in zip(faces, result.material_indices) if used == slot]
            self.assertAlmostEqual(area(part), expected, places=9)
        self.assertEqual(len(faces), 4)
        locked = simplify(positions, triangles, slots, max_error=0.001, locked_materials=(1,))
        kept = [face for face, used in zip(triples(locked.positions, locked.triangles), locked.material_indices)
                if used == 1]
        original = [face for face, used in zip(triples(positions, triangles), slots) if used == 1]
        self.assertEqual(sorted(kept), sorted(original))
        self.assertLess(len(locked.triangles) // 3, len(triangles) // 3)

    def test_curved_surface_stays_within_the_stated_deviation(self):
        radius, segments = 5.0, 96
        place = lambda i, j: (radius * math.cos(2 * math.pi * i / segments),
                              radius * math.sin(2 * math.pi * i / segments), j * 0.5)
        points, faces = sheet(segments, 8, place)
        positions, triangles = mesh(points, faces)
        exact = simplify(positions, triangles, max_error=0.0001)
        self.assertEqual(exact.report['output_triangles'], segments * 2)
        self.assertLess(deviation(positions, exact), 1e-9)
        previous = segments * 2
        for max_error in (0.05, 0.15, 0.4):
            with self.subTest(max_error=max_error):
                result = simplify(positions, triangles, max_error=max_error)
                self.assertLess(result.report['output_triangles'], previous)
                previous = result.report['output_triangles']
                self.assertLessEqual(result.report['reached_error'], max_error)
                self.assertLessEqual(deviation(positions, result), max_error)
                self.assertEqual(bounds(result.positions)[2], (0.0, 4.0))

    def test_rough_terrain_is_reduced_within_the_deviation(self):
        source = random.Random(5)
        height = lambda i, j: 150 + 0.04 * i + 2.0 * math.sin(i / 9) * math.cos(j / 7) + source.uniform(-0.01, 0.01)
        positions, triangles = mesh(*sheet(40, 40, lambda i, j: (2_197_000 + i * 2.0, 481_000 + j * 2.0, height(i, j))))
        result = simplify(positions, triangles, max_error=0.05)
        self.assertLess(result.report['output_triangles'], 3200 * 0.6)
        self.assertLessEqual(deviation(positions, result), 0.05)
        self.assertEqual(bounds(result.positions)[:2], bounds(positions)[:2])
        self.assertTrue(all(cross(*face)[2] > 0 for face in triples(result.positions, result.triangles)))
        self.assertAlmostEqual(sum(cross(*f)[2] for f in triples(result.positions, result.triangles)) / 2, 6400.0,
                               places=3)
        originals = {tuple(positions[i:i + 3]) for i in range(0, len(positions), 3)}
        self.assertTrue(all(tuple(result.positions[i:i + 3]) in originals for i in range(0, len(result.positions), 3)))

    def test_zero_deviation_and_welding(self):
        positions, triangles = mesh(*box((2.0, 2.0, 2.0), 1))
        result = simplify(positions, triangles, max_error=0)
        self.assertEqual((result.report['input_vertices'], result.report['output_vertices']), (24, 8))
        self.assertEqual(result.report['output_triangles'], 12)
        source = random.Random(9)
        noisy = simplify(*mesh(*sheet(6, 6, lambda i, j: (i, j, source.uniform(0, 1)))), max_error=0)
        self.assertEqual(noisy.report['collapses'], 0)

    def test_edges_shared_by_three_triangles_and_empty_triangles_are_handled(self):
        points = [(0, 0, 0), (4, 0, 0), (2, 3, 0), (2, -3, 0), (2, 0, 3), (4, 0, 0), (9, 9, 9)]
        positions, triangles = mesh(points, [(0, 1, 2), (1, 0, 3), (0, 1, 4), (1, 5, 6), (0, 0, 2)])
        result = simplify(positions, triangles, array('i', (0, 1, 2, 3, 4)), max_error=0.5)
        # The fourth triangle has two corners at one place and the fifth repeats a corner.
        self.assertEqual(result.report['output_triangles'], 3)
        self.assertEqual(sorted(result.material_indices), [0, 1, 2])
        self.assertEqual(sorted(triples(result.positions, result.triangles)),
                         sorted(triples(positions, triangles)[:3]))

    def test_detail_smaller_than_the_deviation_may_disappear_but_not_the_whole_mesh(self):
        wall = sheet(4, 4, lambda i, j: (i * 2.0, 0.0, j * 2.0))
        chip = ([(20.0, 0.0, 0.0), (20.004, 0.0, 0.0), (20.0, 0.0, 0.004)], [(0, 1, 2)])
        result = simplify(*mesh(*join(wall, chip)), max_error=0.01)
        self.assertEqual(result.report['output_triangles'], 2)
        self.assertEqual(bounds(result.positions)[0], (0.0, 8.0))
        with self.assertRaises(ReadError):
            simplify(*mesh(*chip), max_error=0.01)

    def test_back_side_copy_of_a_triangle_is_kept_once(self):
        points, faces = sheet(4, 4, lambda i, j: (i * 1.0, 0.0, j * 1.0))
        back = [(a, c, b) for a, b, c in faces]
        positions, triangles = mesh(*join((points, faces), (points, back)))
        result = simplify(positions, triangles, max_error=0.001)
        self.assertEqual((result.report['coincident_triangles'], result.report['output_triangles']), (32, 2))
        self.assertTrue(all(cross(*face)[1] < 0 for face in triples(result.positions, result.triangles)))

    def test_spent_time_stops_the_work_and_spare_time_changes_nothing(self):
        positions, triangles = mesh(*sheet(24, 24, lambda i, j: (i * 1.0, j * 1.0, 0.0)))
        asked = []
        with self.assertRaises(OverBudget):
            simplify(positions, triangles, expired=lambda: asked.append(1) or len(asked) > 1)
        self.assertGreater(24 * 24 * 2, 2 * BUDGET_STEP)
        with self.assertRaises(OverBudget):
            simplify(positions, triangles, expired=lambda: True)

    def test_mesh_with_nothing_to_collapse_still_looks_at_the_time(self):
        # A rough surface offers no collapse within the deviation: the passes
        # before and after the collapse loop are the whole work.
        rough = random.Random(7)
        positions, triangles = mesh(*sheet(100, 100, lambda i, j: (i * 1.0, j * 1.0, rough.uniform(0.0, 5.0))))
        self.assertGreater(100 * 100 * 2, 2 * STAGE_STEP)
        for allowed in (1, 2, 3, 5, 8, 12):
            asked = []
            with self.subTest(allowed=allowed), self.assertRaises(OverBudget):
                simplify(positions, triangles, max_error=0.0, expired=lambda: asked.append(1) or len(asked) > allowed)
        asked = []
        result = simplify(positions, triangles, max_error=0.0, expired=lambda: asked.append(1) and False)
        self.assertEqual(result.report['collapses'], 0)
        self.assertGreater(len(asked), 12)
        free, watched = simplify(positions, triangles), simplify(positions, triangles, expired=lambda: False)
        self.assertEqual((free.triangles, free.positions, free.report), (watched.triangles, watched.positions, watched.report))

    def test_flat_wall_keeps_its_picture_through_the_reduction(self):
        positions, triangles = mesh(*sheet(20, 12, lambda i, j: (1000 + i * 0.5, 7.0, j * 0.25)))
        picture = lambda x, y, z: ((x - 1000) / 10, z / 3)
        result = simplify(positions, triangles, max_error=0.001, uvs=corner_uvs(positions, triangles, picture))
        plain = simplify(positions, triangles, max_error=0.001)
        self.assertEqual((result.triangles, result.positions), (plain.triangles, plain.positions))
        self.assertEqual(result.report['output_triangles'], 2)
        # Every corner still shows the point of the picture it showed before.
        self.assertEqual(result.uvs, corner_uvs(result.positions, result.triangles, picture))
        self.assertLess(result.report['texture_slide'], 1e-9)
        self.assertEqual((len(plain.uvs), 'texture_slide' in plain.report), (0, False))
        # One point of the picture over the whole wall holds nothing back either.
        same = simplify(positions, triangles, max_error=0.001, uvs=array('d', (0.25, 0.5) * len(triangles)))
        self.assertEqual((same.report['output_triangles'], set(zip(same.uvs[::2], same.uvs[1::2]))), (2, {(0.25, 0.5)}))

    def test_seam_between_two_pieces_of_the_unwrapping_stays_in_place(self):
        positions, triangles = mesh(*sheet(20, 12, lambda i, j: (1000 + i * 0.5, 7.0, j * 0.25)))
        left, right = lambda x, y, z: ((x - 1000) / 20, z / 6), lambda x, y, z: (0.5 + z / 6, (x - 1000) / 20)
        side = lambda face: left if sum(p[0] for p in face) / 3 < 1005 else right
        faces = triples(positions, triangles)
        uvs = array('d', (c for face in faces for point in face for c in side(face)(*point)))
        result = simplify(positions, triangles, max_error=0.001, uvs=uvs)
        reduced = triples(result.positions, result.triangles)
        self.assertEqual(len(reduced), 4)
        self.assertEqual(result.uvs, array('d', (c for face in reduced for point in face for c in side(face)(*point))))
        # Both pieces meet on the line of the seam and nowhere else.
        self.assertEqual({p[0] for face in reduced for p in face}, {1000.0, 1005.0, 1010.0})

    def test_stretched_picture_holds_the_triangles_that_carry_it(self):
        positions, triangles = mesh(*sheet(20, 12, lambda i, j: (1000 + i * 0.5, 7.0, j * 0.25)))
        picture = lambda x, y, z: (((x - 1000) / 10) ** 2, z / 3)
        result = simplify(positions, triangles, max_error=0.001, uvs=corner_uvs(positions, triangles, picture))
        # The picture changes its scale along the wall: only collapses across it are free.
        self.assertEqual(result.report['output_triangles'], 40)
        self.assertLessEqual(result.report['texture_slide'], 0.001)
        self.assertEqual(result.uvs, corner_uvs(result.positions, result.triangles, picture))
        loose = simplify(positions, triangles, max_error=1.0, uvs=corner_uvs(positions, triangles, picture))
        self.assertLess(loose.report['output_triangles'], 40)

    def test_invalid_input_is_refused(self):
        positions, triangles = mesh(*sheet(2, 2, lambda i, j: (i, j, 0)))
        for uvs in (array('d', (0.0,) * 47), array('d', (math.nan,) * 48)):
            with self.subTest(uvs=len(uvs)), self.assertRaises(ReadError):
                simplify(positions, triangles, uvs=uvs)
        cases = [(positions[:-1], triangles), (positions, triangles[:-1]), (positions, array('I')),
                 (positions, array('I', (0, 1, 99))), (array('d', (math.inf,) * 27), triangles),
                 (array('d', (1.0, 1.0, 1.0) * 9), triangles)]
        for index, (points, faces) in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ReadError) as raised:
                simplify(points, faces)
            self.assertEqual(raised.exception.rule, 'simplify.input')
        for options in ({'max_error': -1}, {'max_error': math.nan}, {'material_indices': array('i', (0,))}):
            with self.subTest(options=options), self.assertRaises(ReadError):
                simplify(positions, triangles, **options)


if __name__ == '__main__':
    unittest.main()
