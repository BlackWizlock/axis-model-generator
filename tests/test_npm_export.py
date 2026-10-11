from array import array
import unittest

from model_generator.dxf_heights import HeightSample, HeightSurvey
from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_scene import read_scene
from model_generator.fbx_write import ExportMaterial, ExportMesh, write_fbx
from model_generator.npm_export import export_npm, export_scenes
from model_generator.terrain import ControlPoint, Placement, TerrainIndex, build_terrain, place_building
from test_simplify import area, bounds, box, join, mesh, sheet, triples, volume

EAST, NORTH, ZERO = 2_197_450.25, 481_230.75, 151.5


def building():
    """A source scene as the reader returns it: a subdivided block and a flat canopy."""
    positions, triangles = mesh(*box((24.0, 12.0, 30.0), 6))
    half = len(triangles) // 6
    block = ExportMesh('Корпус 1', positions, triangles,
                       (ExportMaterial('Facade', (0.8, 0.75, 0.7)), ExportMaterial('Glass')),
                       array('i', (0 if face < half else 1 for face in range(len(triangles) // 3))))
    canopy = ExportMesh('Корпус 1/козырёк', *mesh(*sheet(4, 2, lambda i, j: (i * 1.5, -3.0 + j * 1.5, 4.0))),
                        (ExportMaterial('Facade', (0.8, 0.75, 0.7)),))
    plain = ExportMesh('Mesh', *mesh(*sheet(2, 2, lambda i, j: (30 + i, j, 1.0))))
    return read_scene(parse_fbx(write_fbx([block, canopy, plain])))


def ground():
    samples = tuple(HeightSample(EAST - 100 + column * 10, NORTH - 100 + row * 10, 150 + column * 0.05,
                                 'geometry', 'POINT', f'{row * 21 + column:X}', 'relief')
                    for row in range(21) for column in range(21))
    return build_terrain(HeightSurvey('geometry', 1.0, samples, (), {}, (150.0, 151.0)))


def names(roots):
    objects = next(n for n in roots if n.name == 'Objects')
    return {n.props[0]: n.props[1].split('\x00')[0] for n in objects.children}


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scene, cls.terrain = building(), ground()
        controls = [ControlPoint((0, 0, 0), (EAST, NORTH, ZERO)), ControlPoint((24, 0, 0), (EAST + 24, NORTH, ZERO)),
                    ControlPoint((0, 12, 30), (EAST, NORTH + 12, ZERO + 30))]
        cls.placed = place_building(cls.scene, cls.terrain, Placement(EAST, NORTH, ZERO, 0.0), controls)
        cls.result = export_npm(cls.scene, cls.placed, cls.terrain, max_error=0.001)
        cls.roots = parse_fbx(cls.result.data)
        cls.read = read_scene(cls.roots)

    def survey(self, read_mesh):
        origin = self.result.report['origin']
        return array('d', (c + origin[i % 3] for i, c in enumerate(read_mesh.positions)))

    def test_exported_file_reads_back_in_metres_with_every_object(self):
        labels = names(self.roots)
        self.assertEqual(self.read.unit_meters, 1.0)
        self.assertEqual([labels[m.model_id] for m in self.read.meshes],
                         ['Terrain', 'Корпус 1', 'Корпус 1/козырёк', 'Mesh'])
        self.assertEqual(self.result.report['origin'], (2_197_450.0, 481_227.0, 0.0))

    def test_building_keeps_shape_position_and_materials_after_reduction(self):
        labels = names(self.roots)
        block = self.read.meshes[1]
        faces = triples(self.survey(block), block.triangles)
        self.assertEqual(len(faces), 12)
        self.assertAlmostEqual(volume([[tuple(c - o for c, o in zip(p, (EAST, NORTH, ZERO))) for p in f]
                                       for f in faces]), 24 * 12 * 30, places=4)
        for actual, expected in zip(bounds(self.survey(block)),
                                    ((EAST, EAST + 24), (NORTH, NORTH + 12), (ZERO, ZERO + 30))):
            self.assertAlmostEqual(actual[0], expected[0], places=6)
            self.assertAlmostEqual(actual[1], expected[1], places=6)
        self.assertEqual([labels[i] for i in block.material_ids], ['Facade', 'Glass'])
        source = self.placed.meshes[0]
        for slot in (0, 1):
            before = area([f for f, s in zip(triples(source.positions, source.triangles), source.material_indices)
                           if s == slot])
            after = area([f for f, s in zip(faces, block.material_indices) if s == slot])
            self.assertAlmostEqual(after, before, places=4)
        canopy, plain = self.read.meshes[2:]
        self.assertEqual(canopy.material_ids, block.material_ids[:1])
        self.assertEqual(len(canopy.triangles) // 3, 2)
        self.assertEqual((plain.material_ids, len(plain.material_indices)), ((), 0))

    def test_terrain_keeps_the_surveyed_surface_under_the_building(self):
        terrain = self.read.meshes[0]
        index = TerrainIndex(type(self.terrain)(self.survey(terrain), terrain.triangles, (),
                                                tuple(bounds(self.survey(terrain)))))
        original = TerrainIndex(self.terrain)
        for x, y in ((EAST, NORTH), (EAST + 24, NORTH + 12), (EAST - 99, NORTH + 99), (EAST + 99.5, NORTH - 99.5)):
            self.assertAlmostEqual(index.height(x, y), original.height(x, y), places=6)
        self.assertLess(len(terrain.triangles) // 3, len(self.terrain.triangles) // 3)

    def test_report_states_the_measured_reduction(self):
        report = self.result.report
        self.assertEqual([e['name'] for e in report['objects']], ['Terrain', 'Корпус 1', 'Корпус 1/козырёк', 'Mesh'])
        self.assertEqual(report['input_triangles'], sum(len(m.triangles) // 3 for m in self.placed.meshes)
                         + len(self.terrain.triangles) // 3)
        self.assertEqual(report['output_triangles'], sum(len(m.triangles) // 3 for m in self.read.meshes))
        self.assertAlmostEqual(report['triangle_share'], report['output_triangles'] / report['input_triangles'])
        self.assertLess(report['triangle_share'], 0.05)
        self.assertEqual(report['objects'][1]['materials'], ['Facade', 'Glass'])
        self.assertEqual((report['objects'][3]['materials'], report['objects'][3]['materials_assigned']), ([], False))
        self.assertEqual(report['textures'], 'not exported')
        self.assertEqual(report['control_residuals'], self.placed.residuals)

    def test_clashing_names_stay_distinct(self):
        twin = ExportMesh('Terrain', *mesh(*sheet(1, 1, lambda i, j: (i, j, 0.0))))
        other = ExportMesh('Terrain 2', *mesh(*sheet(1, 1, lambda i, j: (i + 2, j, 0.0))))
        scene = read_scene(parse_fbx(write_fbx([twin, other])))
        controls = [ControlPoint((0, 0, 0), (EAST, NORTH, ZERO)), ControlPoint((3, 1, 0), (EAST + 3, NORTH + 1, ZERO))]
        placed = place_building(scene, self.terrain, Placement(EAST, NORTH, ZERO, 0.0), controls)
        roots = parse_fbx(export_npm(scene, placed, self.terrain).data)
        self.assertEqual(sorted(names(roots)[m.model_id] for m in read_scene(roots).meshes),
                         ['Terrain', 'Terrain 2', 'Terrain 2 2'])

    def test_scenes_in_shared_coordinates_become_one_file_without_moving(self):
        ground_mesh = ExportMesh('Ground', *mesh(*sheet(10, 10, lambda i, j: (9400 + i * 8.0, 3700 + j * 8.0, 130.0))),
                                 (ExportMaterial('Facade', (0.2, 0.5, 0.2)),))
        positions, triangles = mesh(*box((12.0, 8.0, 40.0), 4))
        moved = array('d', (c + (9430.5, 3731.25, 130.0)[i % 3] for i, c in enumerate(positions)))
        tower = ExportMesh('Tower', moved, triangles, (ExportMaterial('Facade', (0.8, 0.75, 0.7)),))
        first = read_scene(parse_fbx(write_fbx([tower])))
        second = read_scene(parse_fbx(write_fbx([ground_mesh])))
        result = export_scenes([('building', first), ('ground', second)], max_error=0.001)
        roots = parse_fbx(result.data)
        read = read_scene(roots)
        labels = names(roots)
        self.assertEqual(result.report['origin'], (9400.0, 3700.0, 0.0))
        self.assertEqual([(e['name'], e['kind'], e['output_triangles']) for e in result.report['objects']],
                         [('Tower', 'building', 12), ('Ground', 'ground', 2)])
        # Two source files each named a material Facade with its own colour: both survive.
        self.assertEqual([[labels[i] for i in m.material_ids] for m in read.meshes], [['Facade'], ['Facade 2']])
        for mesh_read, source in zip(read.meshes, (first.meshes[0], second.meshes[0])):
            for actual, expected in zip(bounds(self.survey_of(result, mesh_read)), bounds(source.positions)):
                self.assertAlmostEqual(actual[0], expected[0], places=6)
                self.assertAlmostEqual(actual[1], expected[1], places=6)
        self.assertNotIn('control_residuals', result.report)

    @staticmethod
    def survey_of(result, read_mesh):
        origin = result.report['origin']
        return array('d', (c + origin[i % 3] for i, c in enumerate(read_mesh.positions)))


if __name__ == '__main__':
    unittest.main()
