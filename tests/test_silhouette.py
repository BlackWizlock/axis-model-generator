from array import array
from types import SimpleNamespace
import unittest

from model_generator import silhouette
from model_generator.silhouette import outline
from model_generator.simplify import OverBudget
from test_simplify import box, mesh


def part(positions, triangles):
    return SimpleNamespace(positions=array('d', positions), triangles=array('I', triangles))


def tower(size=(40.0, 20.0, 60.0), cuts=24):
    return part(*mesh(*box(size, cuts)))


class OutlineTests(unittest.TestCase):
    def test_all_meshes_become_one_mesh_within_the_stated_triangles(self):
        source = [tower(), part([c + 100.0 if i % 3 == 0 else c for i, c in enumerate(tower().positions)], tower().triangles)]
        total = sum(len(m.triangles) // 3 for m in source)
        positions, triangles, cell = outline(source, 400)
        self.assertLessEqual(len(triangles) // 3, 400)
        self.assertLess(len(triangles) // 3, total)
        self.assertEqual(max(triangles), len(positions) // 3 - 1)
        # The shape of the whole stays: the bounds move by less than a cell.
        for axis, (low, high) in enumerate(((0.0, 140.0), (0.0, 20.0), (0.0, 60.0))):
            self.assertAlmostEqual(min(positions[axis::3]), low, delta=cell)
            self.assertAlmostEqual(max(positions[axis::3]), high, delta=cell)

    def test_a_mesh_already_within_the_triangles_keeps_its_first_cell(self):
        source = tower(cuts=2)
        _, triangles, cell = outline([source], 10_000)
        self.assertEqual(cell, 60.0 / silhouette.CELLS)
        self.assertEqual(len(triangles), len(source.triangles))

    def test_facing_is_kept_and_equal_triangles_are_kept_once(self):
        square = [0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0]
        front, back = [0, 1, 2, 0, 2, 3], [0, 2, 1, 0, 3, 2]
        positions, triangles, _ = outline([part(square, front), part(square, front), part(square, back)], 100)
        self.assertEqual(len(triangles) // 3, 4)
        def normal(t):
            a, b, c = (positions[v * 3:v * 3 + 3] for v in t)
            return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        signs = sorted(normal(triangles[i:i + 3]) > 0 for i in range(0, len(triangles), 3))
        self.assertEqual(signs, [False, False, True, True])

    def test_parts_smaller_than_the_cell_disappear(self):
        speck = part([5, 5, 70, 5.001, 5, 70, 5, 5.001, 70], [0, 1, 2])
        _, with_speck, _ = outline([tower(cuts=2), speck], 10_000)
        _, without, _ = outline([tower(cuts=2)], 10_000)
        self.assertEqual(len(with_speck), len(without))

    def test_no_surface_no_room_and_no_time_are_refused(self):
        for meshes, room, expired in (([], 100, None), ([tower()], 0, None), ([tower()], 100, lambda: True),
                                      ([part([0, 0, 0, 1, 0, 0, 0, 1, 0], [])], 100, None)):
            with self.assertRaises(OverBudget):
                outline(meshes, room, expired)
        # A flat square cannot go below two triangles however coarse the cell.
        with self.assertRaises(OverBudget):
            outline([part([0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 10, 0], [0, 1, 2, 0, 2, 3])], 1)


if __name__ == '__main__':
    unittest.main()
