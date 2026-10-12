import unittest

from model_generator.dxf_heights import HeightLine, HeightSurvey
from model_generator.limits import ReadError
from model_generator.site_plan import inspect_plan, plan_mesh
from test_dxf_heights import dxf


def survey(*lines):
    return HeightSurvey('elevation', 1.0, (), tuple(lines), {}, (0.0, 0.0))


def line(points, closed=False, layer='contours'):
    return HeightLine(tuple(points), closed, 'elevation', 'LWPOLYLINE', '1', layer)


class PlanMeshTests(unittest.TestCase):
    def test_each_segment_is_a_ribbon_at_its_own_heights(self):
        mesh = plan_mesh(survey(line([(0, 0, 130), (10, 0, 130), (10, 5, 131)])), width=0.2)
        self.assertEqual((len(mesh.positions) // 3, len(mesh.triangles) // 3), (8, 4))
        self.assertEqual(list(mesh.positions[:12]), [0, 0.1, 130, 0, -0.1, 130, 10, -0.1, 130, 10, 0.1, 130])
        self.assertEqual(sorted(set(mesh.positions[2::3])), [130, 131])
        self.assertEqual(list(mesh.triangles[:6]), [0, 1, 2, 0, 2, 3])

    def test_a_closed_line_returns_to_its_start_and_a_vertical_step_is_left_out(self):
        closed = plan_mesh(survey(line([(0, 0, 1), (4, 0, 1), (4, 4, 1)], closed=True)))
        self.assertEqual(len(closed.triangles) // 3, 6)
        stepped = plan_mesh(survey(line([(0, 0, 1), (0, 0, 2), (4, 0, 2)])))
        self.assertEqual(len(stepped.triangles) // 3, 2)

    def test_nothing_to_show_and_too_much_are_refused(self):
        for lines, room, rule in (((), 10, 'plan.heights'), ((line([(0, 0, 1), (0, 0, 2)]),), 10, 'plan.heights'),
                                  ((line([(0, 0, 1), (1, 0, 1), (2, 0, 1)]),), 1, 'plan.budget')):
            with self.assertRaises(ReadError) as caught:
                plan_mesh(survey(*lines), max_segments=room)
            self.assertEqual(caught.exception.rule, rule)


class InspectPlanTests(unittest.TestCase):
    def test_facts_state_layers_and_numbers(self):
        facts, findings, mesh = inspect_plan(PLAN)
        self.assertEqual((facts['lines'], facts['layers_total'], facts['unit_meters']), (2, 1, 1.0))
        self.assertEqual(facts['layers'], [{'name': 'relief', 'lines': 2, 'low': 131.5, 'high': 139.0}])
        self.assertEqual((facts['z_range'], facts['bounds']), ([131.5, 139.0], [[0.0, 0.0], [10.0, 10.0]]))
        self.assertEqual([f.rule_id for f in findings], ['plan.heights'])
        self.assertEqual(len(mesh.triangles) // 3, 10)

    def test_a_drawing_without_heights_is_refused(self):
        with self.assertRaises(ReadError) as caught:
            inspect_plan(dxf([polyline(0, [(0, 0), (1, 1)])]))
        self.assertEqual(caught.exception.rule, 'plan.heights')


def polyline(elevation, points):
    return ('LWPOLYLINE', [(38, elevation), (70, 0), *((code, value) for x, y in points for code, value in ((10, x), (20, y)))])


PLAN = dxf([polyline(131.5, [(0, 0), (10, 0), (10, 10)]), polyline(139, [(0, 5), (5, 5), (5, 9), (2, 9)])])

if __name__ == '__main__':
    unittest.main()
