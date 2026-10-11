from array import array
import math
import unittest

from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_scene import read_scene
from functools import partial

# The tests of this file read the texture coordinates with the scene.
plain_scene, read_scene = read_scene, partial(read_scene, unwrapping=True)
from model_generator.fbx_write import ExportMaterial, ExportMesh, ExportTexture, write_fbx
from fixtures.builders import png_bytes
from model_generator.limits import ReadError

BRICK, GLASS = ExportMaterial('Brick', (0.6, 0.3, 0.2)), ExportMaterial('Glass')


def quad(name, x=0.0, z=0.0, **options):
    points = [(x, 0, z), (x + 4.5, 0, z), (x + 4.5, 0.25, z + 3), (x, 0.25, z + 3)]
    return ExportMesh(name, array('d', (c for p in points for c in p)), array('I', (0, 1, 2, 0, 2, 3)), **options)


def labels(roots):
    objects = next(n for n in roots if n.name == 'Objects')
    return {n.props[0]: (n.name, n.props[1].split('\x00')[0]) for n in objects.children}


class WriteTests(unittest.TestCase):
    def test_reader_returns_what_was_written(self):
        meshes = [quad('Wall', materials=(BRICK, GLASS), material_indices=array('i', (1, 0))),
                  quad('Roof', x=-812.125, z=141.0625, materials=(BRICK,)), quad('Ground', z=-3.5)]
        data = write_fbx(meshes)
        roots = parse_fbx(data)
        scene = read_scene(roots)
        names = labels(roots)
        self.assertEqual(scene.unit_meters, 1.0)
        self.assertEqual([names[m.model_id] for m in scene.meshes],
                         [('Model', 'Wall'), ('Model', 'Roof'), ('Model', 'Ground')])
        for written, read in zip(meshes, scene.meshes):
            self.assertEqual(read.positions, written.positions)
            self.assertEqual(read.triangles, written.triangles)
            self.assertFalse(read.mirrored)
        wall, roof, ground = scene.meshes
        self.assertEqual([names[i] for i in wall.material_ids], [('Material', 'Brick'), ('Material', 'Glass')])
        self.assertEqual(list(wall.material_indices), [1, 0])
        # The same material is one object shared by both meshes.
        self.assertEqual(roof.material_ids, wall.material_ids[:1])
        self.assertEqual(list(roof.material_indices), [0])
        self.assertEqual((ground.material_ids, list(ground.material_indices)), ((), []))
        self.assertEqual(scene.bounds, ((-812.125, 0.0, -3.5), (4.5, 0.25, 144.0625)))

    def test_texture_coordinates_and_pictures_read_back(self):
        picture, other = png_bytes(), png_bytes(128, 128)
        wall = ExportMaterial('Wall', (0.5, 0.5, 0.5), (ExportTexture('DiffuseColor', 'wall.png', picture),
                                                        ExportTexture('ShininessExponent', 'rough.png', other)))
        sign = ExportMaterial('Sign', textures=(ExportTexture('DiffuseColor', 'wall.png', picture),))
        uvs = array('d', (0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 0.75))
        meshes = [quad('Front', materials=(wall,), uvs=uvs), quad('Back', x=9.0, materials=(sign,), uvs=uvs),
                  quad('Bare', x=18.0, uvs=uvs), quad('Plain', x=27.0, materials=(wall,))]
        data = write_fbx(meshes)
        self.assertEqual(data, write_fbx(meshes))
        roots = parse_fbx(data)
        scene = read_scene(roots)
        self.assertEqual([m.uvs for m in scene.meshes], [uvs, uvs, uvs, array('d')])
        objects = next(n for n in roots if n.name == 'Objects')
        by_id, names = {n.props[0]: n for n in objects.children}, labels(roots)
        # Four distinct pairs for six corners: the shared ones are written once.
        layer = next(c for c in objects.children[0].children if c.name == 'LayerElementUV')
        self.assertEqual((len(layer.child('UV').props[0]), list(layer.child('UVIndex').props[0])), (8, [0, 1, 2, 0, 2, 3]))
        videos = {n.props[1].split('\x00')[0]: n for n in objects.children if n.name == 'Video'}
        self.assertEqual({name: node.child('Content').props[0] for name, node in videos.items()},
                         {'wall.png': picture, 'rough.png': other})
        self.assertEqual({node.child('RelativeFilename').props[0] for node in videos.values()}, {'wall.png', 'rough.png'})
        links = [n.props for n in next(n for n in roots if n.name == 'Connections').children]
        shown = sorted((names[material][1], channel, names[next(l[1] for l in links if l[0] == 'OO' and l[2] == texture)][1])
                       for kind, texture, material, *channel in links if kind == 'OP')
        self.assertEqual(shown, [('Sign', ['DiffuseColor'], 'wall.png'), ('Wall', ['DiffuseColor'], 'wall.png'),
                                 ('Wall', ['ShininessExponent'], 'rough.png')])
        self.assertEqual(sum(n.name == 'Texture' for n in objects.children), 3)

    def test_invalid_texture_data_is_refused(self):
        picture = png_bytes()
        texture = lambda **change: ExportTexture(**{'channel': 'DiffuseColor', 'filename': 'a.png', 'content': picture, **change})
        material = lambda *textures: ExportMaterial('Wall', textures=textures)
        cases = [quad('Wall', uvs=array('d', (0.0,) * 11)), quad('Wall', uvs=array('d', (math.nan,) * 12)),
                 quad('Wall', materials=(material(texture(filename='dir/a.png')),)),
                 quad('Wall', materials=(material(texture(filename='dir\\a.png')),)),
                 quad('Wall', materials=(material(texture(filename=' ')),)),
                 quad('Wall', materials=(material(texture(content=b'GIF89a')),)),
                 quad('Wall', materials=(material(texture(channel='Diffuse Color')),)),
                 quad('Wall', materials=(material(texture(), texture(filename='b.png')),)),
                 quad('Wall', materials=(material(texture()), ExportMaterial('Other', textures=(texture(content=png_bytes(64, 64)),))))]
        for index, mesh in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ReadError) as raised:
                write_fbx([mesh])
            self.assertEqual(raised.exception.rule, 'export.fbx')

    def test_material_colour_is_written_only_when_known(self):
        roots = parse_fbx(write_fbx([quad('Wall', materials=(BRICK, GLASS))]))
        objects = next(n for n in roots if n.name == 'Objects')
        colours = {n.props[1].split('\x00')[0]: [p.props for p in n.child('Properties70').children]
                   for n in objects.children if n.name == 'Material'}
        self.assertEqual(colours, {'Brick': [['DiffuseColor', 'Color', '', 'A', 0.6, 0.3, 0.2]], 'Glass': []})

    def test_output_is_deterministic_and_large_arrays_are_compressed(self):
        count = 4000
        positions = array('d', (c for i in range(count) for c in (i * 0.5, math.sin(i), 0.0)))
        triangles = array('I', (c for i in range(count - 2) for c in (i, i + 1, i + 2)))
        mesh = ExportMesh('Strip', positions, triangles)
        data = write_fbx([mesh])
        self.assertEqual(data, write_fbx([mesh]))
        self.assertLess(len(data), len(positions) * 8)
        scene = read_scene(parse_fbx(data))
        self.assertEqual((scene.meshes[0].positions, scene.meshes[0].triangles), (positions, triangles))

    def test_invalid_scenes_are_refused(self):
        good = quad('Wall')
        def changed(**fields):
            return ExportMesh(**{**good.__dict__, **fields})
        cases = [[], [good, good], [changed(name='')], [changed(name='a\x00b')],
                 [changed(positions=array('d', (0, 0)))], [changed(triangles=array('I', (0, 1)))],
                 [changed(triangles=array('I', (0, 1, 9)))], [changed(triangles=array('I', (0, 1, 1)))],
                 [changed(positions=array('d', (math.nan,) * 12))],
                 [changed(positions=array('d', (2_197_450.0,) * 12))],
                 [changed(material_indices=array('i', (0, 0)))],
                 [changed(materials=(BRICK,), material_indices=array('i', (0,)))],
                 [changed(materials=(BRICK,), material_indices=array('i', (0, 1)))],
                 [changed(materials=(BRICK, BRICK))],
                 [changed(materials=(ExportMaterial('Paint', (0.5, 2, 0)),))],
                 [changed(materials=(BRICK,)), quad('Other', materials=(ExportMaterial('Brick'),))]]
        for index, meshes in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ReadError) as raised:
                write_fbx(meshes)
            self.assertEqual(raised.exception.rule, 'export.fbx')


if __name__ == '__main__':
    unittest.main()
