import io
import unittest
from unittest import mock
import zipfile

from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_scene import read_scene
from functools import partial

# The tests of this file read the texture coordinates with the scene.
read_scene = partial(read_scene, unwrapping=True)
from model_generator.fbx_write import ExportMaterial, ExportMesh, ExportTexture, write_fbx
from model_generator.npm_export import export_scenes
from model_generator.reduced_model import FACT_BYTES, FACT_OBJECTS, MODEL_MAX_BYTES, build_reduced_model
from model_generator.simplify import OverBudget
from fixtures.builders import png_bytes
from test_simplify import box, corner_uvs, mesh, sheet


def tower():
    return write_fbx([ExportMesh('Tower', *mesh(*box((24.0, 12.0, 30.0), 6)), (ExportMaterial('Facade'),))])


def lawn():
    return write_fbx([ExportMesh('Lawn', *mesh(*sheet(12, 12, lambda i, j: (-20 + i * 5.0, -20 + j * 5.0, 0.0))),
                                 (ExportMaterial('Grass'),))])


def painted():
    positions, triangles = mesh(*sheet(10, 6, lambda i, j: (500 + i * 0.5, 7.0, j * 0.5)))
    wall = ExportMaterial('Wall', textures=(ExportTexture('DiffuseColor', 'wall.png', png_bytes()),))
    return write_fbx([ExportMesh('Front', positions, triangles, (wall,),
                                 uvs=corner_uvs(positions, triangles, lambda x, y, z: ((x - 500) / 5, z / 3)))])


def package(members):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return target.getvalue()


class ReducedModelTests(unittest.TestCase):
    def test_archive_members_become_one_readable_file_with_roles(self):
        data, facts = build_reduced_model(package({'SM_Site_001_Main.fbx': tower(), 'SM_Site_Ground.fbx': lawn(),
                                                   'notes.txt': b'ignored'}), 'zip-fbx')
        scene = read_scene(parse_fbx(data))
        self.assertEqual((facts['status'], facts['reason'], facts['max_error']), ('built', None, 0.02))
        self.assertEqual([(item['name'], item['kind']) for item in facts['objects']],
                         [('Tower', 'building'), ('Lawn', 'ground')])
        self.assertEqual(len(scene.meshes), 2)
        self.assertEqual(facts['output_triangles'], sum(len(m.triangles) // 3 for m in scene.meshes))
        self.assertLess(facts['output_triangles'], facts['input_triangles'])
        self.assertLessEqual(len(data), MODEL_MAX_BYTES)

    def test_single_file_is_reduced_without_a_role(self):
        data, facts = build_reduced_model(tower(), 'fbx')
        self.assertEqual([item['kind'] for item in facts['objects']], ['model'])
        self.assertEqual(facts['textures'], {'written': 0, 'bytes': 0, 'left_out': 0})
        self.assertEqual([item['texture_coordinates'] for item in facts['objects']], [False])
        self.assertEqual(len(read_scene(parse_fbx(data)).meshes), 1)

    def test_unreadable_or_empty_input_gives_no_file(self):
        for data, kind in ((b'not-a-zip', 'zip-fbx'), (tower()[:80], 'fbx'), (package({'notes.txt': b'x'}), 'zip-fbx'),
                           (package({'a.fbx': tower(), 'b.fbx': b'broken'}), 'zip-fbx')):
            with self.subTest(kind=kind, size=len(data)):
                self.assertEqual(build_reduced_model(data, kind),
                                 (None, {'status': 'not_built', 'reason': 'model_unsupported'}))

    def test_result_over_the_size_limit_is_refused(self):
        self.assertEqual(build_reduced_model(tower(), 'fbx', max_bytes=64),
                         (None, {'status': 'not_built', 'reason': 'model_budget'}))

    def test_reduction_that_outlasts_its_time_is_refused(self):
        for data, kind in ((tower(), 'fbx'), (package({'a.fbx': tower(), 'b_ground.fbx': lawn()}), 'zip-fbx')):
            with self.subTest(kind=kind):
                self.assertEqual(build_reduced_model(data, kind, cpu_seconds=-1.0),
                                 (None, {'status': 'not_built', 'reason': 'model_budget'}))

    def test_model_too_large_with_the_unwrapping_is_built_without_it(self):
        data, facts = build_reduced_model(painted(), 'fbx')
        self.assertEqual((facts['textures']['written'], facts['objects'][0]['texture_coordinates']), (1, True))
        plain = export_scenes([('model', read_scene(parse_fbx(painted())))], unwrapping=False)
        self.assertLess(len(plain.data), len(data) - len(png_bytes()))
        data, facts = build_reduced_model(painted(), 'fbx', max_bytes=len(plain.data))
        self.assertEqual(data, plain.data)
        self.assertEqual((facts['status'], facts['textures']), ('built', {'written': 0, 'bytes': 0, 'left_out': 1}))
        self.assertEqual([item['texture_coordinates'] for item in facts['objects']], [False])
        self.assertFalse(read_scene(parse_fbx(data)).meshes[0].uvs)

    def test_model_out_of_time_or_memory_with_the_unwrapping_is_built_without_it(self):
        for failure in (OverBudget, MemoryError, OverflowError, ValueError, RecursionError):
            calls = []
            def build(scenes, **options):
                calls.append(options.get('unwrapping', True))
                if calls[-1]:
                    raise failure
                return export_scenes(scenes, **options)
            with self.subTest(failure=failure.__name__), mock.patch('model_generator.reduced_model.export_scenes', build):
                data, facts = build_reduced_model(painted(), 'fbx')
                self.assertEqual(calls, [True, False])
                self.assertEqual((facts['status'], facts['textures']['left_out']), ('built', 1))
                self.assertFalse(read_scene(parse_fbx(data)).meshes[0].uvs)

    def test_unwrapping_is_not_tried_inside_the_time_of_the_build_without_it(self):
        calls = []
        def build(scenes, **options):
            calls.append(options.get('unwrapping', True))
            return export_scenes(scenes, **options)
        for left, expected in ((34.9, [False]), (35.1, [True])):
            calls.clear()
            with self.subTest(left=left), mock.patch('model_generator.reduced_model.export_scenes', build), \
                    mock.patch('model_generator.reduced_model.cpu_left', lambda: left):
                self.assertEqual(build_reduced_model(painted(), 'fbx')[1]['status'], 'built')
                self.assertEqual(calls, expected)

    def test_texture_met_in_many_connections_is_looked_at_once(self):
        from model_generator import npm_export
        from model_generator.fbx_binary import Node
        scene = read_scene(parse_fbx(painted()))
        links = next(n for n in scene.source_nodes if n.name == 'Connections')
        link = next(n for n in links.children if n.props[0] == 'OP')
        links.children += [Node('C', [*link.props[:3], f'Channel{n}'], []) for n in range(5)] + [Node('C', list(link.props), [])]
        with mock.patch.object(npm_export, '_untransformed', wraps=npm_export._untransformed) as looked:
            facts = export_scenes([('model', scene)]).report['textures']
        self.assertEqual((looked.call_count, facts['written'], facts['left_out']), (1, 1, 1))

    def test_work_that_never_looks_at_its_time_is_stopped(self):
        import time
        from model_generator.reduced_model import stopped_after
        def spin(seconds):
            until = time.process_time() + seconds
            while time.process_time() < until:
                pass
        started = time.process_time()
        with self.assertRaises(OverBudget), stopped_after(0.2):
            spin(30)
        self.assertLess(time.process_time() - started, 5)
        with stopped_after(0.2):
            pass
        spin(0.5)  # nothing is left armed behind either use
        with self.assertRaises(OverBudget), stopped_after(0.0):
            self.fail('no time, no work')

    def test_build_with_the_unwrapping_that_ignores_its_time_gives_way_to_the_build_without_it(self):
        calls = []
        def build(scenes, **options):
            calls.append(options.get('unwrapping', True))
            while calls[-1]:
                pass
            return export_scenes(scenes, **options)
        with mock.patch('model_generator.reduced_model.export_scenes', build):
            data, facts = build_reduced_model(painted(), 'fbx', cpu_seconds=0.3)
        self.assertEqual((calls, facts['status'], facts['textures']['left_out']), ([True, False], 'built', 1))

    def test_build_without_the_unwrapping_stops_short_of_the_limit_of_the_process(self):
        from model_generator import reduced_model
        def build(scenes, **options):
            while True:
                pass
        with mock.patch('model_generator.reduced_model.export_scenes', build), \
                mock.patch('model_generator.reduced_model.cpu_left', lambda: reduced_model.REPORT_CPU_MARGIN + 0.3):
            self.assertEqual(build_reduced_model(painted(), 'fbx'), (None, {'status': 'not_built', 'reason': 'model_budget'}))

    def test_mesh_above_the_unwrapping_size_is_built_once_without_it(self):
        calls = []
        def build(scenes, **options):
            calls.append((options.get('unwrapping', True), [len(m.uvs) for _, scene in scenes for m in scene.meshes]))
            return export_scenes(scenes, **options)
        with mock.patch('model_generator.reduced_model.export_scenes', build), \
                mock.patch('model_generator.reduced_model.UNWRAPPING_MESH_TRIANGLES', 119):
            data, facts = build_reduced_model(painted(), 'fbx')
        self.assertEqual(calls, [(False, [0])])
        self.assertEqual((facts['status'], facts['textures']), ('built', {'written': 0, 'bytes': 0, 'left_out': 1}))
        with mock.patch('model_generator.reduced_model.UNWRAPPING_MESH_TRIANGLES', 120):
            self.assertEqual(build_reduced_model(painted(), 'fbx')[1]['textures']['written'], 1)

    def test_second_build_out_of_time_or_memory_gives_no_file(self):
        for failure in (OverBudget, MemoryError):
            def build(scenes, **options):
                raise failure
            with self.subTest(failure=failure.__name__), mock.patch('model_generator.reduced_model.export_scenes', build):
                self.assertEqual(build_reduced_model(painted(), 'fbx'),
                                 (None, {'status': 'not_built', 'reason': 'model_budget'}))

    def test_facts_stay_small_for_many_meshes_and_long_names(self):
        import json
        tile = mesh(*sheet(1, 1, lambda i, j: (i * 1.0, j * 1.0, 0.0)))
        many = write_fbx([ExportMesh(f'Tile {n}', *tile, (ExportMaterial('Stone'),)) for n in range(FACT_OBJECTS + 50)])
        data, facts = build_reduced_model(many, 'fbx')
        self.assertEqual(len(read_scene(parse_fbx(data)).meshes), FACT_OBJECTS + 50)
        self.assertEqual((len(facts['objects']), facts['objects_total']), (FACT_OBJECTS, FACT_OBJECTS + 50))
        self.assertEqual(facts['output_triangles'], 2 * (FACT_OBJECTS + 50))
        long = write_fbx([ExportMesh('Плита ' + 'я' * 900 + str(n), *tile, (ExportMaterial('Stone'),)) for n in range(60)])
        data, facts = build_reduced_model(long, 'fbx')
        self.assertEqual((facts['status'], facts['objects'], facts['objects_total']), ('built', [], 60))
        self.assertLessEqual(len(json.dumps(facts, ensure_ascii=False).encode('utf-8')), FACT_BYTES)
        self.assertEqual(build_reduced_model(tower(), 'fbx')[1]['objects_total'], 1)

if __name__ == '__main__':
    unittest.main()
