from array import array
import unittest

from model_generator.dxf_heights import HeightSample, HeightSurvey
from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_scene import read_scene
from functools import partial

# The tests of this file read the texture coordinates with the scene.
plain_scene, read_scene = read_scene, partial(read_scene, unwrapping=True)
from model_generator.fbx_binary import Node
from model_generator.fbx_write import ExportMaterial, ExportMesh, ExportTexture, write_fbx
from fixtures.builders import png_bytes, scene_bytes
from test_simplify import corner_uvs
from unittest import mock
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
        self.assertEqual(report['textures'], {'written': 0, 'bytes': 0, 'left_out': 0})
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

    def textured(self):
        positions, triangles = mesh(*sheet(10, 6, lambda i, j: (500 + i * 0.5, 7.0, j * 0.5)))
        picture, rough = png_bytes(), png_bytes(128, 128)
        wall = ExportMaterial('Wall', textures=(ExportTexture('DiffuseColor', 'wall.png', picture),
                                                ExportTexture('ShininessExponent', 'rough.png', rough)))
        place = lambda x, y, z: ((x - 500) / 5, z / 3)
        return parse_fbx(write_fbx([ExportMesh('Front', positions, triangles, (wall,),
                                               uvs=corner_uvs(positions, triangles, place))])), place, picture, rough

    def contents(self, roots):
        objects = next(n for n in roots if n.name == 'Objects')
        return {n.child('RelativeFilename').props[0]: n.child('Content').props[0] for n in objects.children if n.name == 'Video'}

    def test_pictures_and_texture_coordinates_survive_the_reduction(self):
        roots, place, picture, rough = self.textured()
        result = export_scenes([('model', read_scene(roots))], max_error=0.001)
        written = parse_fbx(result.data)
        front = read_scene(written).meshes[0]
        self.assertEqual(len(front.triangles), 6)
        origin = result.report['origin']
        survey = array('d', (c + origin[i % 3] for i, c in enumerate(front.positions)))
        for actual, expected in zip(front.uvs, corner_uvs(survey, front.triangles, place)):
            self.assertAlmostEqual(actual, expected, places=12)
        self.assertEqual(self.contents(written), {'wall.png': picture, 'rough.png': rough})
        self.assertEqual(result.report['textures'], {'written': 2, 'bytes': len(picture) + len(rough), 'left_out': 0})
        self.assertEqual([(e['texture_coordinates'], e['texture_slide'] < 1e-9) for e in result.report['objects']], [(True, True)])

    def test_pictures_beyond_the_budget_or_laid_with_a_transform_are_left_out(self):
        roots, place, picture, rough = self.textured()
        build = lambda max_bytes=None: export_scenes([('model', read_scene(roots))], max_error=0.001, max_bytes=max_bytes)
        whole = build()
        self.assertEqual(build(len(whole.data)).data, whole.data)
        # No room for any picture: the geometry is written whole, without them.
        bare = build(len(whole.data) - len(picture) - len(rough))
        self.assertEqual(self.contents(parse_fbx(bare.data)), {})
        self.assertEqual(bare.report['textures'], {'written': 0, 'bytes': 0, 'left_out': 2})
        self.assertEqual(read_scene(parse_fbx(bare.data)).meshes[0].uvs, read_scene(parse_fbx(whole.data)).meshes[0].uvs)
        self.assertEqual([m.textures for mesh in bare.meshes for m in mesh.materials], [()])
        # Room for one: the colour picture stays although the other one is smaller.
        with mock.patch('model_generator.npm_export.PICTURE_NODES', 1000):
            result = build(len(bare.data) + len(picture) + 1000)
        self.assertEqual(self.contents(parse_fbx(result.data)), {'wall.png': picture})
        self.assertEqual(result.report['textures'], {'written': 1, 'bytes': len(picture), 'left_out': 1})
        self.assertLessEqual(len(result.data), len(bare.data) + len(picture) + 1000)
        objects = next(n for n in roots if n.name == 'Objects')
        texture = next(n for n in objects.children if n.name == 'Texture')
        texture.child('Properties70').children.append(Node('P', ['Scaling', 'Vector', '', 'A', 2.0, 2.0, 1.0], []))
        result = export_scenes([('model', read_scene(roots))], max_error=0.001)
        self.assertEqual(self.contents(parse_fbx(result.data)), {'rough.png': rough})
        self.assertEqual(result.report['textures']['left_out'], 1)
        # The triangles keep their coordinates even when a picture is gone.
        self.assertTrue(result.report['objects'][0]['texture_coordinates'])

    def test_source_path_of_a_picture_is_reduced_to_its_file_name(self):
        roots, place, picture, rough = self.textured()
        objects = next(n for n in roots if n.name == 'Objects')
        for video, path in zip((n for n in objects.children if n.name == 'Video'),
                               ('C:\\legacy\\site.fbm\\tex.png', 'other/tex.png')):
            video.child('RelativeFilename').props[0] = path
        result = export_scenes([('model', read_scene(roots))], max_error=0.001)
        # Two files of one name in different folders stay two files.
        self.assertEqual(self.contents(parse_fbx(result.data)), {'tex.png': picture, 'tex 2.png': rough})
        # A material without a stated slot on the triangles is not written, nor is its picture.
        bare = export_scenes([('model', read_scene(parse_fbx(scene_bytes(axes=True))))])
        self.assertEqual(bare.report['textures'], {'written': 0, 'bytes': 0, 'left_out': 0})
        self.assertFalse(bare.report['objects'][0]['texture_coordinates'])

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
