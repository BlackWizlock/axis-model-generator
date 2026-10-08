"""Synthetic, pure geometry checks; these do not prove a Blender runtime."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from fixtures.package_builders import IDENTITY, make_package, make_scene, pack_array
from model_generator.package_reader import read_package_bytes
from model_generator.package_scene import inspect_package_scene
from model_generator.web import preview


def translation(x=0, y=0, z=0):
    matrix = list(IDENTITY)
    matrix[3], matrix[7], matrix[11] = x, y, z
    return matrix


def scene_package(scene=None, vertices=None):
    overrides = {} if vertices is None else {'geometry/vertices.bin': pack_array('float64', vertices)}
    package = read_package_bytes(make_package(scene_updates=scene or {}, member_overrides=overrides))
    return package, inspect_package_scene(package)


def altered(package, inspection, *, scene=None, member=None):
    """Create a real DTO with an internally hashed, post-inspection mutation."""
    members = dict(package.members)
    if scene is not None:
        members['scene.json'] = json.dumps(scene).encode()
    if member:
        members.update(member)
    files = tuple(replace(item, bytes=len(members[item.path]),
                          sha256=hashlib.sha256(members[item.path]).hexdigest())
                  for item in package.manifest.files)
    return replace(package, members=members, manifest=replace(package.manifest, files=files)), replace(inspection, scene=scene or inspection.scene)


class PreviewTests(unittest.TestCase):
    def test_neutral_package_preserves_reader_report_and_preview(self):
        from model_generator.web.validation_child import validate_input, ChildSettings
        wire=make_package()
        reports=[]; previews=[]
        for filename in ('input.zip','input.bin'):
            with tempfile.TemporaryDirectory() as directory:
                scratch=Path(directory); path=scratch/filename; path.write_bytes(wire)
                result=validate_input(path,'portable-package',ChildSettings(scratch=scratch))
                reports.append(result.report)
                self.assertIsNotNone(result.preview_input_path)
                previews.append(result.preview_input_path.read_bytes())
                self.assertEqual(result.report['input_sha256'],hashlib.sha256(wire).hexdigest())
        self.assertEqual(reports[0],reports[1]); self.assertEqual(previews[0],previews[1])

    def test_limits_are_exact_immutable_positive_and_cannot_raise_hard_caps(self):
        limits = preview.PreviewLimits()
        self.assertEqual((limits.instances, limits.vertices, limits.triangles, limits.wire_bytes),
                         (1000, 300000, 200000, 16777216))
        with self.assertRaises(FrozenInstanceError):
            limits.vertices = 4
        for values in ({'instances': True}, {'vertices': 0}, {'triangles': 200001}):
            with self.assertRaises(ValueError):
                preview.PreviewLimits(**values)

    def test_triangle_has_exact_contract_and_original_bytes_stay_unchanged(self):
        package, inspection = scene_package()
        snapshot = deepcopy(package.members)
        document = preview.build_preview(package, inspection, preview.PreviewLimits())
        self.assertEqual(document['vertexCount'], 3)
        self.assertEqual(document['triangleCount'], 1)
        self.assertEqual(document['positions'], [-.5, -.5, 0, .5, -.5, 0, -.5, .5, 0])
        self.assertEqual(document['indices'], [0, 1, 2])
        self.assertEqual(document['origin'], [.5, .5, 0])
        self.assertEqual(document['provenance'], 'uploaded_package')
        self.assertEqual(package.members, snapshot)
        self.assertEqual(preview.validate_preview(document, preview.PreviewLimits()), document)

    def test_two_instances_rotation_and_mirror_remap_once(self):
        scene = make_scene()
        rotation = [0, -1, 0, 4, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        mirror = list(IDENTITY)
        mirror[0] = -1
        first = scene['instances'][0]
        first['transform'] = rotation
        second = deepcopy(first)
        second.update(instance_id='mirror', transform=mirror)
        second['element_key']['unique_id'] = 'second'
        scene['instances'].append(second)
        document = preview.build_preview(*scene_package(scene), preview.PreviewLimits())
        self.assertEqual(document['indices'], [0, 1, 2, 3, 5, 4])
        self.assertEqual(document['vertexCount'], 6)
        restored = [document['positions'][n] + document['origin'][n % 3] for n in range(18)]
        self.assertEqual(restored, [4, 0, 0, 4, 1, 0, 3, 0, 0, 0, 0, 0, -1, 0, 0, 0, 1, 0])

    def test_link_chain_and_double_mirror_keep_processing_transform_only(self):
        scene = make_scene()
        revision = deepcopy(scene['documents'][0]['revision'])
        scene['documents'] += [{'document_id': doc, 'revision': revision} for doc in ('a', 'b')]
        mirror = list(IDENTITY)
        mirror[0], mirror[3] = -2, 10
        scene['links'] = [
            {'parent_document_id': 'root', 'document_id': 'a', 'link_instance_id': 'to-a', 'transform': mirror},
            {'parent_document_id': 'a', 'document_id': 'b', 'link_instance_id': 'to-b', 'transform': translation(3)}]
        instance = scene['instances'][0]
        instance['element_key'].update(document_id='b', link_instance_path=['to-a', 'to-b'])
        instance['transform'][0] = -1
        scene['coordinates'].update(processing_origin=[999, 999, 999], processing_to_project=translation(100))
        document = preview.build_preview(*scene_package(scene), preview.PreviewLimits())
        self.assertEqual(document['indices'], [0, 1, 2])
        self.assertEqual(document['origin'], [5, .5, 0])
        self.assertEqual(document['positions'], [-1, -.5, 0, 1, -.5, 0, -1, .5, 0])

    def test_9511_metres_rebase_preserves_hash_and_records_display_error_separately(self):
        scene = make_scene()
        scene['instances'][0]['transform'] = translation(9511)
        package, inspection = scene_package(scene, [(0, 0, 0), (.123456789, 0, 0), (0, .25, 0)])
        old_hash = package.input_sha256
        old_bytes = package.members['geometry/vertices.bin']
        document = preview.build_preview(package, inspection, preview.PreviewLimits())
        self.assertAlmostEqual(document['origin'][0], 9511 + .123456789 / 2)
        measurements = preview.preview_measurements(document)
        self.assertGreater(measurements['float32_max_error_metres'], 0)
        self.assertLessEqual(measurements['float32_max_error_metres'], 1e-5)
        self.assertNotIn('accuracy', document)
        self.assertEqual((package.input_sha256, package.members['geometry/vertices.bin']), (old_hash, old_bytes))

    def test_limits_reject_before_array_scan_or_transform(self):
        package, inspection = scene_package()
        for limits in (preview.PreviewLimits(vertices=2), preview.PreviewLimits(triangles=1, vertices=2)):
            with patch('model_generator.web.preview.struct.iter_unpack', side_effect=AssertionError('allocation started')):
                with self.assertRaises(preview.PreviewError) as error:
                    preview.build_preview(package, inspection, limits)
                self.assertEqual(error.exception.code, 'preview_budget')
        scene = make_scene()
        other = deepcopy(scene['instances'][0])
        other['instance_id'] = 'other'
        other['element_key']['unique_id'] = 'other'
        scene['instances'].append(other)
        package, inspection = scene_package(scene)
        with patch('model_generator.web.preview.struct.iter_unpack', side_effect=AssertionError('allocation started')):
            for limits in (preview.PreviewLimits(instances=1), preview.PreviewLimits(triangles=1)):
                with self.assertRaises(preview.PreviewError) as error:
                    preview.build_preview(package, inspection, limits)
                self.assertEqual(error.exception.code, 'preview_budget')

    def test_binary_sizes_hash_layout_indices_and_nan_are_rechecked(self):
        package, inspection = scene_package()
        for data in (b'bad', pack_array('float64', [(float('nan'), 0, 0), (1, 0, 0), (0, 1, 0)])):
            changed = altered(package, inspection, member={'geometry/vertices.bin': data})
            with self.assertRaises(preview.PreviewError):
                preview.build_preview(*changed, preview.PreviewLimits())
        changed = altered(package, inspection, member={'geometry/triangles.bin': struct.pack('<III', 0, 1, 3)})
        with self.assertRaises(preview.PreviewError):
            preview.build_preview(*changed, preview.PreviewLimits())
        members = {**package.members, 'geometry/vertices.bin': bytes(72)}
        with self.assertRaises(preview.PreviewError):
            preview.build_preview(replace(package, members=members), inspection, preview.PreviewLimits())
        files = tuple(replace(item, array=replace(item.array, dtype='float32')) if item.role == 'vertices' else item
                      for item in package.manifest.files)
        with self.assertRaises(preview.PreviewError):
            preview.build_preview(replace(package, manifest=replace(package.manifest, files=files)), inspection, preview.PreviewLimits())

    def test_axes_non_affine_singular_overflow_and_unbound_scene_rejected(self):
        package, inspection = scene_package()
        for fault in ('axes', 'affine', 'singular', 'overflow', 'extension'):
            scene = deepcopy(inspection.scene)
            if fault == 'axes':
                scene['coordinates']['axes'] = 'y-up'
            elif fault == 'extension':
                scene['instances'][0]['extensions'] = {'unverified': 1}
            else:
                matrix = scene['instances'][0]['transform']
                if fault == 'affine':
                    matrix[12] = 1
                elif fault == 'singular':
                    matrix[0] = 0
                else:
                    matrix[0], matrix[3] = 1e308, 1e308
            with self.assertRaises(preview.PreviewError):
                preview.build_preview(*altered(package, inspection, scene=scene), preview.PreviewLimits())
        scene = deepcopy(inspection.scene)
        scene['instances'][0]['transform'] = translation(4)
        with self.assertRaises(preview.PreviewError):
            preview.build_preview(package, replace(inspection, scene=scene), preview.PreviewLimits())

    def test_unknown_scene_preview_fields_are_rejected(self):
        package, inspection = scene_package()
        for location in ('scene', 'coordinates', 'mesh', 'instance', 'key'):
            scene = deepcopy(inspection.scene)
            target = {'scene': scene, 'coordinates': scene['coordinates'],
                      'mesh': scene['meshes'][0], 'instance': scene['instances'][0],
                      'key': scene['instances'][0]['element_key']}[location]
            target['futureFeature'] = 'unverified'
            with self.subTest(location=location), self.assertRaises(preview.PreviewError):
                preview.build_preview(*altered(package, inspection, scene=scene), preview.PreviewLimits())

    def test_empty_scene_is_honest_unavailable(self):
        scene = make_scene()
        scene.update(meshes=[], instances=[], materials=[])
        with self.assertRaises(preview.PreviewError) as error:
            preview.build_preview(*scene_package(scene), preview.PreviewLimits())
        self.assertEqual(error.exception.code, 'preview_unsupported')

    def test_exact_budgets_accept_and_wire_fingerprint_is_deterministic(self):
        package, inspection = scene_package()
        document = preview.build_preview(package, inspection,
                                         preview.PreviewLimits(instances=1, vertices=3, triangles=1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.json'
            count, digest = preview.write_preview(document, path, preview.PreviewLimits())
            tight = preview.PreviewLimits(wire_bytes=count)
            self.assertEqual(preview.write_preview(document, path, tight), (count, digest))
            self.assertEqual(preview.write_preview(dict(reversed(list(document.items()))), path, tight), (count, digest))
            with self.assertRaises(preview.PreviewError):
                preview.write_preview(document, path, preview.PreviewLimits(wire_bytes=count - 1))

    def test_float32_precision_and_range_fail_explicitly(self):
        for x in (10000.1234567, 1e40):
            package, inspection = scene_package(vertices=[(0, 0, 0), (x, 0, 0), (0, 1, 0)])
            with self.assertRaises(preview.PreviewError) as error:
                preview.build_preview(package, inspection, preview.PreviewLimits())
            self.assertEqual(error.exception.code, 'preview_roundtrip_error')

    def test_schema_counts_bounds_indices_and_rebase_are_strict(self):
        base = preview.build_synthetic_demo()
        for key, value in (('schemaVersion', True), ('provenance', 'private-path'), ('coordinates', 'world'),
                           ('vertexCount', True), ('triangleCount', 0), ('unknown', 1),
                           ('positions', [float('nan')] * 9), ('indices', [0, 1, True]),
                           ('origin', [float('inf'), 0, 0]), ('limitations', ['x' * 2000])):
            document = deepcopy(base)
            document[key] = value
            with self.subTest(key=key), self.assertRaises(preview.PreviewError):
                preview.validate_preview(document, preview.PreviewLimits())
        document = deepcopy(base)
        document['bounds']['extra'] = 0
        with self.assertRaises(preview.PreviewError):
            preview.validate_preview(document, preview.PreviewLimits())
        document = deepcopy(base)
        document['bounds']['max'][0] += 1
        with self.assertRaises(preview.PreviewError):
            preview.validate_preview(document, preview.PreviewLimits())
        document = deepcopy(base)
        document['positions'] = [item + 1 if n % 3 == 0 else item for n, item in enumerate(document['positions'])]
        document['bounds']['min'][0] += 1
        document['bounds']['max'][0] += 1
        with self.assertRaises(preview.PreviewError):
            preview.validate_preview(document, preview.PreviewLimits())

    def test_demo_is_owned_triangle_without_source_metadata(self):
        demo = preview.build_synthetic_demo()
        self.assertEqual((demo['provenance'], demo['vertexCount'], demo['triangleCount']), ('synthetic', 3, 1))
        self.assertEqual(preview.validate_preview(demo, preview.PreviewLimits()), demo)
        self.assertTrue(demo['limitations'])

    def test_wire_budget_duplicate_keys_nonfinite_and_unknown_features(self):
        document = preview.build_synthetic_demo()
        with self.assertRaises(preview.PreviewError) as error:
            preview.validate_preview(document, preview.PreviewLimits(wire_bytes=64))
        self.assertEqual(error.exception.code, 'preview_budget')
        for wire in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"a":NaN}', b'[]', b'{' + b' ' * 16777216):
            with self.assertRaises(preview.PreviewError):
                preview.decode_preview_json(wire, preview.PreviewLimits())

    def test_write_is_atomic_private_bounded_and_fingerprinted(self):
        document = preview.build_synthetic_demo()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.json'
            count, digest = preview.write_preview(document, path, preview.PreviewLimits())
            content = path.read_bytes()
            self.assertEqual((count, digest), (len(content), hashlib.sha256(content).hexdigest()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(preview.decode_preview_json(content, preview.PreviewLimits()), document)
            with self.assertRaises(preview.PreviewError):
                preview.write_preview(document, path, preview.PreviewLimits(wire_bytes=64))
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_trusted_script_checks_wire_before_importing_bpy(self):
        script = Path(__file__).resolve().parents[2] / 'workers' / 'blender_preview.py'
        spec = importlib.util.spec_from_file_location('trusted_blender_preview', script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'preview.json'
            preview.write_preview(preview.build_synthetic_demo(), path, preview.PreviewLimits())
            document, digest = module.read_checked_preview(path)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(document['vertexCount'], 3)
            path.write_bytes(b'{"schemaVersion":1,"schemaVersion":1}')
            with self.assertRaises(preview.PreviewError):
                module.read_checked_preview(path)
        for arguments in ([], ['--input', 'x', '--output-dir', 'y', '--source', 'z'],
                          ['--input', 'x', '--input', 'y']):
            with self.assertRaises(ValueError):
                module.parse_arguments(arguments)


if __name__ == '__main__':
    unittest.main()
