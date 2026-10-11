import io
import unittest
import zipfile

from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_scene import read_scene
from model_generator.fbx_write import ExportMaterial, ExportMesh, write_fbx
from model_generator.reduced_model import FACT_BYTES, FACT_OBJECTS, MODEL_MAX_BYTES, build_reduced_model
from test_simplify import box, mesh, sheet


def tower():
    return write_fbx([ExportMesh('Tower', *mesh(*box((24.0, 12.0, 30.0), 6)), (ExportMaterial('Facade'),))])


def lawn():
    return write_fbx([ExportMesh('Lawn', *mesh(*sheet(12, 12, lambda i, j: (-20 + i * 5.0, -20 + j * 5.0, 0.0))),
                                 (ExportMaterial('Grass'),))])


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
