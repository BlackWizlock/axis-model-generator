from array import array
from dataclasses import replace
import math
import random
import unittest

from model_generator.dxf_heights import HeightLine, HeightSample, HeightSurvey
from model_generator.fbx_scene import FbxMesh, FbxScene
from model_generator.limits import Limits, ReadError
from model_generator.terrain import (GRID, ControlPoint, Placement, TerrainIndex, _hull, _in_circle,
                                     _orient, build_terrain, place_building)


def survey(points=(), lines=()):
    samples = tuple(HeightSample(x, y, z, 'geometry', 'POINT', f'{i:X}', 'relief')
                    for i, (x, y, z) in enumerate(points))
    lines = tuple(HeightLine(tuple(points), False, 'elevation', 'LWPOLYLINE', f'L{i}', 'contours')
                  for i, points in enumerate(lines))
    return HeightSurvey('geometry' if samples or lines else 'none', 1.0, samples, lines, {}, (0.0, 0.0))


def plane(x, y):
    return 2 * x + 3 * y + 5


def corners(terrain):
    xy = [(round(terrain.positions[i] * GRID), round(terrain.positions[i + 1] * GRID))
          for i in range(0, len(terrain.positions), 3)]
    return xy, [tuple(terrain.triangles[i:i + 3]) for i in range(0, len(terrain.triangles), 3)]


def doubled_area(polygon):
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1]))


class TriangulationTests(unittest.TestCase):
    def check(self, points, brute=True):
        """Independent proof: the hull is covered once and no point is inside any circle."""
        terrain = build_terrain(survey(points))
        xy, triangles = corners(terrain)
        self.assertEqual(len(xy), len(points))
        self.assertEqual(set(v for t in triangles for v in t), set(range(len(xy))))
        for a, b, c in triangles:
            self.assertGreater(_orient(xy[a], xy[b], xy[c]), 0)
        self.assertEqual(sum(_orient(xy[a], xy[b], xy[c]) for a, b, c in triangles), doubled_area(_hull(xy)))
        edges = {}
        for t in triangles:
            for k in range(3):
                edge = (t[k], t[(k + 1) % 3])
                self.assertNotIn(edge, edges)
                edges[edge] = t
        boundary = [edge for edge in edges if edge[::-1] not in edges]
        self.assertEqual(len(triangles), 2 * len(xy) - 2 - len(boundary))
        if brute:
            for a, b, c in triangles:
                self.assertFalse(any(_in_circle(xy[a], xy[b], xy[c], p) > 0 for p in xy))
        return terrain

    def test_random_points_form_a_delaunay_triangulation(self):
        source = random.Random(20261011)
        points = {(source.randrange(0, 90000) / 100, source.randrange(0, 60000) / 100) for _ in range(300)}
        self.check([(x, y, plane(x, y)) for x, y in sorted(points)])

    def test_regular_grid_with_cocircular_points_is_covered_once(self):
        terrain = self.check([(x * 2.5, y * 2.5, x + y) for x in range(12) for y in range(12)])
        self.assertEqual(len(terrain.triangles) // 3, 2 * 11 * 11)

    def test_points_on_lines_and_edges_are_triangulated(self):
        points = [(x, 0.0, 1.0) for x in range(9)] + [(x, 4.0, 2.0) for x in range(0, 9, 2)]
        points += [(4.0, 2.0, 1.5), (2.0, 2.0, 1.5), (6.0, 2.0, 1.5), (0.0, 2.0, 1.5)]
        self.check(points)

    def test_survey_coordinates_far_from_the_origin_keep_millimetres(self):
        source = random.Random(7)
        base = (2_197_450.0, 481_230.0)
        points = {(base[0] + source.randrange(0, 400000) / 1000, base[1] + source.randrange(0, 400000) / 1000)
                  for _ in range(200)}
        terrain = self.check([(x, y, 150.0) for x, y in sorted(points)])
        self.assertEqual(terrain.bounds[2], (150.0, 150.0))

    def test_many_points_stay_consistent(self):
        source = random.Random(3)
        points = {(source.randrange(0, 10 ** 7) / 1000, source.randrange(0, 10 ** 7) / 1000) for _ in range(6000)}
        self.check([(x, y, 0.5) for x, y in sorted(points)], brute=False)

    def test_line_vertices_and_origins_reach_the_terrain(self):
        terrain = build_terrain(survey([(0, 0, 10), (10, 0, 11)], [[(0, 10, 12.0), (10, 10, 12.0), (5, 5, 12.0)]]))
        self.assertEqual(len(terrain.origins), 5)
        self.assertEqual(terrain.origins[0], ('geometry', 'POINT', '0', 'relief'))
        self.assertEqual(terrain.origins[4], ('elevation', 'LWPOLYLINE', 'L0', 'contours'))
        self.assertEqual(list(terrain.positions[12:15]), [5.0, 5.0, 12.0])
        self.assertEqual(terrain.bounds, ((0.0, 10.0), (0.0, 10.0), (10.0, 12.0)))

    def test_same_height_at_one_location_is_one_vertex(self):
        terrain = build_terrain(survey([(0, 0, 1), (4, 0, 1), (0, 4, 1), (4, 0, 1.0004), (4.00001, 0, 1)]))
        self.assertEqual(len(terrain.origins), 3)

    def test_refusals(self):
        cases = {'terrain.heights': survey(),
                 'terrain.conflict': survey([(0, 0, 1), (4, 0, 1), (0, 4, 1), (4, 0, 1.01)]),
                 'terrain.degenerate': survey([(0, 0, 1), (1, 1, 2), (2, 2, 3), (5, 5, 4)]),
                 'terrain.value': survey([(0, 0, 1), (4, 0, math.nan), (0, 4, 1)])}
        cases['terrain.degenerate.two'] = survey([(0, 0, 1), (1, 1, 2)])
        for rule, source in cases.items():
            with self.subTest(rule=rule), self.assertRaises(ReadError) as raised:
                build_terrain(source)
            self.assertEqual(raised.exception.rule, rule.removesuffix('.two'))
        with self.assertRaises(ReadError) as raised:
            build_terrain(survey([(0, 0, 1), (4, 0, 1), (0, 4, 1), (4, 4, 1)]), replace(Limits(), terrain_points=3))
        self.assertEqual(raised.exception.rule, 'terrain.budget')

    def test_label_heights_wait_for_confirmation(self):
        # Detail numbers of a real sheet arrive exactly like this: a few labelled flat points.
        labelled = tuple(HeightSample(x, y, z, 'label', 'INSERT', f'{i:X}', 'marks')
                         for i, (x, y, z) in enumerate([(0, 0, 1), (4, 0, 2), (0, 4, 3)]))
        mixed = survey([(0, 0, 1), (4, 0, 1), (0, 4, 1)])
        mixed = replace(mixed, samples=mixed.samples + labelled[1:2])
        for source in (HeightSurvey('label', 1.0, labelled, (), {}, (1.0, 3.0)), mixed):
            with self.assertRaises(ReadError) as raised:
                build_terrain(source)
            self.assertEqual(raised.exception.rule, 'terrain.labels')
        terrain = build_terrain(HeightSurvey('label', 1.0, labelled, (), {}, (1.0, 3.0)), accept_labels=True)
        self.assertEqual(len(terrain.triangles), 3)


class HeightTests(unittest.TestCase):
    def test_surface_reproduces_a_plane_and_every_surveyed_height(self):
        source = random.Random(11)
        points = [(0, 0), (50, 0), (0, 40), (50, 40)] + [(source.uniform(0, 50), source.uniform(0, 40))
                                                         for _ in range(150)]
        terrain = build_terrain(survey([(x, y, plane(x, y)) for x, y in points]))
        index = TerrainIndex(terrain)
        for _ in range(300):
            x, y = source.uniform(0, 50), source.uniform(0, 40)
            self.assertAlmostEqual(index.height(x, y), plane(x, y), places=9)
        for start in range(0, len(terrain.positions), 3):
            x, y, z = terrain.positions[start:start + 3]
            self.assertAlmostEqual(index.height(x, y), z, places=9)
        for x, y in ((-0.01, 5), (50.01, 5), (5, -0.01), (5, 40.01), (1e9, 1e9)):
            self.assertIsNone(index.height(x, y))

    def test_height_is_missing_inside_the_bounds_but_outside_the_surface(self):
        index = TerrainIndex(build_terrain(survey([(0, 0, 1), (10, 0, 1), (0, 10, 1)])))
        self.assertIsNone(index.height(9, 9))
        self.assertEqual(index.height(2, 2), 1.0)


def box(low, high):
    (x0, y0, z0), (x1, y1, z1) = low, high
    points = [(x, y, z) for z in (z0, z1) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    mesh = FbxMesh(1, 2, array('d', (c for p in points for c in p)), array('I', (0, 1, 2, 0, 2, 3, 4, 5, 6, 4, 6, 7)),
                   (), array('i'))
    return FbxScene((mesh,), (low, high), 1.0, ())


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.terrain = build_terrain(survey([(x, y, 100 + x / 100) for x in range(1000, 1201, 20)
                                             for y in range(500, 701, 20)]))
        self.scene = box((0, 0, -3), (30, 12, 20))
        # Model +Y points to survey +X: a quarter turn clockwise from north.
        self.placement = Placement(1100.0, 600.0, 101.5, 90.0)
        self.controls = [ControlPoint((0, 0, 0), (1100, 600, 101.5)), ControlPoint((30, 0, 0), (1100, 570, 101.5)),
                         ControlPoint((0, 12, 20), (1112, 600, 121.5))]

    def rule(self, **changes):
        arguments = {'scene': self.scene, 'terrain': self.terrain, 'placement': self.placement,
                     'controls': self.controls, **changes}
        with self.assertRaises(ReadError) as raised:
            place_building(**arguments)
        return raised.exception.rule

    def test_confirmed_placement_moves_the_building(self):
        original = list(self.scene.meshes[0].positions)
        placed = place_building(self.scene, self.terrain, self.placement, self.controls)
        self.assertEqual(list(self.scene.meshes[0].positions), original)
        for actual, expected in zip(placed.bounds, ((1100, 1112), (570, 600), (98.5, 121.5))):
            self.assertAlmostEqual(actual[0], expected[0], places=9)
            self.assertAlmostEqual(actual[1], expected[1], places=9)
        self.assertLess(max(placed.residuals), 1e-9)
        self.assertEqual(len(placed.residuals), 3)
        self.assertAlmostEqual(placed.ground_range[0], 111.0, places=9)
        self.assertAlmostEqual(placed.ground_range[1], 111.12, places=9)
        self.assertEqual(placed.meshes[0].triangles, self.scene.meshes[0].triangles)

    def test_bearing_is_clockwise_from_north(self):
        scene = box((0, 0, 0), (1, 10, 1))
        for bearing, far in ((0, (1100, 610)), (90, (1110, 600)), (180, (1100, 590)), (270, (1090, 600))):
            with self.subTest(bearing=bearing):
                controls = [ControlPoint((0, 0, 0), (1100, 600, 100)), ControlPoint((0, 10, 0), (*far, 100))]
                placed = place_building(scene, self.terrain, Placement(1100, 600, 100, bearing), controls)
                self.assertLess(max(placed.residuals), 1e-9)

    def test_placement_is_refused_without_confirmation(self):
        self.assertEqual(self.rule(controls=[]), 'placement.unverified')
        self.assertEqual(self.rule(controls=self.controls[:1]), 'placement.unverified')
        near = [self.controls[0], ControlPoint((0.5, 0, 0), (1100, 599.5, 101.5))]
        self.assertEqual(self.rule(controls=near), 'placement.unverified')
        vertical = [self.controls[0], ControlPoint((0, 0, 20), (1100, 600, 121.5))]
        self.assertEqual(self.rule(controls=vertical), 'placement.unverified')

    def test_contradicting_control_points_refuse_the_placement(self):
        for change in ({'bearing_degrees': 89.9}, {'zero_elevation': 101.52}, {'east': 1100.02},
                       {'bearing_degrees': -90.0}):
            with self.subTest(change=change):
                self.assertEqual(self.rule(placement=replace(self.placement, **change)), 'placement.control')
        moved = self.controls[:2] + [ControlPoint((0, 12, 20), (1112, 600, 121.52))]
        self.assertEqual(self.rule(controls=moved), 'placement.control')

    def test_building_outside_the_terrain_is_refused(self):
        placement = replace(self.placement, east=1195.0)
        controls = [ControlPoint(c.model, (c.survey[0] + 95, *c.survey[1:])) for c in self.controls]
        self.assertEqual(self.rule(placement=placement, controls=controls), 'placement.outside')

    def test_eaves_past_the_edge_of_the_survey_are_measured(self):
        # The survey ends at x = 1200; the building is 12 m long along survey +X.
        def shifted(start):
            placement = replace(self.placement, east=start)
            controls = [ControlPoint(c.model, (c.survey[0] + start - 1100, *c.survey[1:])) for c in self.controls]
            return {'placement': placement, 'controls': controls}
        placed = place_building(self.scene, self.terrain, **shifted(1188.3))
        self.assertAlmostEqual(placed.overhang, 0.3, places=9)
        self.assertAlmostEqual(placed.ground_range[0], 111.883, places=9)
        self.assertEqual(place_building(self.scene, self.terrain, self.placement, self.controls).overhang, 0.0)
        self.assertEqual(self.rule(**shifted(1188.6)), 'placement.outside')
        self.assertEqual(self.rule(**shifted(1188.3), overhang=0.0), 'placement.outside')
        self.assertEqual(self.rule(overhang=-1.0), 'placement.value')

    def test_invalid_values_are_refused(self):
        self.assertEqual(self.rule(placement=replace(self.placement, north=math.inf)), 'placement.value')
        self.assertEqual(self.rule(tolerance=0), 'placement.value')
        self.assertEqual(self.rule(scene=FbxScene((), ((0, 0, 0), (0, 0, 0)), 1.0, ())), 'placement.value')
        broken = self.controls[:2] + [ControlPoint((0, 0, math.nan), (1100, 600, 101.5))]
        self.assertEqual(self.rule(controls=broken), 'placement.value')


if __name__ == '__main__':
    unittest.main()
