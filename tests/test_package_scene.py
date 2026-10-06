"""Exact synthetic geometry, graph and semantic-boundary regression tests."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import struct
import unittest
from unittest.mock import patch
from fixtures.package_builders import IDENTITY, make_package, make_scene, pack_array
from model_generator.package_manifest import ArrayDescriptor, FileDescriptor, PackageError, PackageLimits
from model_generator.package_reader import read_package_bytes
from model_generator.package_scene import inspect_package_scene

def translation(x=0, y=0, z=0):
    result = list(IDENTITY)
    result[3], result[7], result[11] = (x, y, z)
    return result

def document(name):
    return {'document_id': name,
        'revision': {'value': None, 'method': 'unknown', 'saved_file_sha256': None, 'unsaved_changes': False}}

def instance(name, doc='root', path=(), transform=None):
    return {'instance_id': name,
        'mesh_id': 'triangle',
        'element_key': {'document_id': doc, 'link_instance_path': list(path), 'unique_id': 'same-source-element'},
        'transform': list(IDENTITY) if transform is None else transform,
        'element_id': 10}

def package(scene=None, arrays=None):
    base = read_package_bytes(make_package())
    members = dict(base.members)
    members['scene.json'] = json.dumps(scene or make_scene()).encode()
    descriptors = {item.path: item for item in base.manifest.files}
    for path, role, dtype, components, values in arrays or []:
        data = pack_array(dtype, values)
        members[path] = data
        descriptors[path] = FileDescriptor(path,
            role,
            len(data),
            hashlib.sha256(data).hexdigest(),
            ArrayDescriptor(dtype, 'little', len(values), components, components * (8 if dtype == 'float64' else 4)))
    return replace(base, manifest=replace(base.manifest, files=tuple(descriptors.values())), members=members)

class SceneTests(unittest.TestCase):

    def assert_error(self, data, rule=None, file=None, key=None, limits=None):
        with self.assertRaises(PackageError) as caught:
            inspect_package_scene(data, limits)
        error = caught.exception
        self.assertTrue(error.rule.startswith('package.'))
        if rule:
            self.assertEqual(error.rule, rule)
        if file:
            self.assertEqual(error.file, file)
        if key:
            self.assertEqual(error.element_key, key)
        return error

    def test_exact_large_float64_bounds_and_counts(self):
        offset = 2 ** 40
        result = inspect_package_scene(package(arrays=[('geometry/vertices.bin',
            'vertices',
            'float64',
            3,
            [(offset, 0, 0), (offset + 0.125, 0, 0), (offset, 0.25, 0)])]))
        self.assertEqual(result.measurements['processing_bounds'],
            {'min': [offset, 0, 0], 'max': [offset + 0.125, 0.25, 0]})
        self.assertEqual({k: result.measurements[k] for k in ('meshes', 'instances', 'mesh_vertices', 'mesh_triangles', 'rendered_triangles', 'mirrored_instances')},
            {'meshes': 1, 'instances': 1, 'mesh_vertices': 3, 'mesh_triangles': 1, 'rendered_triangles': 1, 'mirrored_instances': 0})
        self.assertEqual(result.capabilities['geometry']['state'], 'available')

    def test_nested_links_compose_once_and_keep_instance_keys(self):
        scene = make_scene()
        scene['documents'] += [document('linked'), document('nested')]
        scale = list(IDENTITY)
        scale[0] = 2
        mirror = list(IDENTITY)
        mirror[0] = -1
        scene['links'] = [{'parent_document_id': 'root', 'document_id': 'linked', 'link_instance_id': 'a', 'transform': translation(10)},
            {'parent_document_id': 'root', 'document_id': 'linked', 'link_instance_id': 'b', 'transform': mirror},
            {'parent_document_id': 'linked', 'document_id': 'nested', 'link_instance_id': 'c', 'transform': scale}]
        scene['instances'] = [instance('a', 'nested', ['a', 'c'], translation(3)),
            instance('b', 'linked', ['b']),
            instance('root', transform=translation(5))]
        scene['coordinates']['processing_origin'] = [999, 999, 999]
        scene['coordinates']['processing_to_project'] = translation(100)
        result = inspect_package_scene(package(scene))
        self.assertEqual(result.measurements['processing_bounds'], {'min': [-1, 0, 0], 'max': [18, 1, 0]})
        self.assertEqual(result.measurements['project_bounds'], {'min': [99, 0, 0], 'max': [118, 1, 0]})
        self.assertEqual(result.measurements['mesh_triangles'], 1)
        self.assertEqual(result.measurements['rendered_triangles'], 3)
        self.assertEqual(result.measurements['mirrored_instances'], 1)

    def test_nonfinite_binary_vertices_and_normals_uv(self):
        for role, comps in (('vertices', 3), ('normals', 3), ('uv', 2)):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(role=role, value=value):
                    scene = make_scene()
                    path = f'geometry/{role}.bin'
                    if role != 'vertices':
                        scene['meshes'][0][role] = path
                    values = [[1] * comps for _ in range(3)]
                    values[0][0] = value
                    self.assert_error(package(scene, [(path, role, 'float64', comps, values)]), file=path)

    def test_uint32_out_of_bounds_including_negative_wire_value(self):
        for index in (3, 4294967295):
            self.assert_error(package(arrays=[('geometry/triangles.bin', 'triangles', 'uint32', 3, [(0, 1, index)])]),
                file='geometry/triangles.bin')

    def test_array_exact_layout_size_and_count(self):
        base = package()
        for change in ({'dtype': 'float32'},
            {'components': 2},
            {'stride_bytes': 12},
            {'count': 2},
            {'byte_order': 'big'}):
            with self.subTest(change=change):
                files = list(base.manifest.files)
                files[1] = replace(files[1], array=replace(files[1].array, **change))
                self.assert_error(replace(base, manifest=replace(base.manifest, files=tuple(files))),
                    file=files[1].path)
        for data in (base.members['geometry/vertices.bin'][:-1],
            base.members['geometry/vertices.bin'] + b'\x00'):
            members = dict(base.members)
            members['geometry/vertices.bin'] = data
            self.assert_error(replace(base, members=members), file='geometry/vertices.bin')
        scene = make_scene()
        scene['meshes'][0]['vertex_count'] = 4
        self.assert_error(package(scene), file='geometry/vertices.bin')

    def test_individual_and_summed_array_scalar_budget(self):
        self.assert_error(package(), 'package.budget', limits=PackageLimits(array_items=8))
        self.assert_error(package(), 'package.budget', limits=PackageLimits(array_items=11))

    def test_scene_record_budget_includes_nested_ranges_and_control_points(self):
        scene = make_scene()
        self.assert_error(package(scene), 'package.budget', limits=PackageLimits(scene_records=4))
        inspect_package_scene(package(scene), PackageLimits(scene_records=5))
        scene['coordinates']['control_points'] = [{'id': 'p',
            'local': [0, 0, 0],
            'project': [0, 0, 0],
            'source': 'fixture'}]
        self.assert_error(package(scene), 'package.budget', limits=PackageLimits(scene_records=5))

    def test_duplicate_element_key_and_unknown_mesh_document_are_addressed(self):
        for fault in ('duplicate', 'mesh', 'document', 'path'):
            scene = make_scene()
            key = scene['instances'][0]['element_key']
            if fault == 'duplicate':
                other = deepcopy(scene['instances'][0])
                other['instance_id'] = 'other'
                scene['instances'].append(other)
            elif fault == 'mesh':
                scene['instances'][0]['mesh_id'] = 'absent'
            elif fault == 'document':
                key['document_id'] = 'absent'
            else:
                key['link_instance_path'] = ['absent']
            self.assert_error(package(scene), key=key)

    def test_duplicate_record_ids_and_unknown_fields(self):
        for collection, id_field in (('documents', 'document_id'),
            ('meshes', 'mesh_id'),
            ('instances', 'instance_id'),
            ('materials', 'material_id')):
            scene = make_scene()
            scene[collection].append(deepcopy(scene[collection][0]))
            self.assert_error(package(scene))
        scene = make_scene()
        scene['meshes'][0]['typo'] = True
        self.assert_error(package(scene), 'package.schema')
        scene = make_scene()
        scene['extensions'] = {}
        scene['meshes'][0]['extensions'] = {}
        inspect_package_scene(package(scene))

    def test_bad_matrices_and_composition_overflow(self):
        for matrix in ([1] * 15, [True] + IDENTITY[1:], [0] + IDENTITY[1:], IDENTITY[:-1] + [2]):
            scene = make_scene()
            scene['instances'][0]['transform'] = matrix
            self.assert_error(package(scene), key=scene['instances'][0]['element_key'])
        scene = make_scene()
        scene['instances'][0]['transform'] = [math.inf] + IDENTITY[1:]
        self.assert_error(package(scene), 'package.json')
        scene = make_scene()
        scene['instances'][0]['transform'] = translation(1e+308)
        scene['coordinates']['processing_to_project'] = translation(1e+308)
        self.assert_error(package(scene))

    def test_unused_cycle_and_unreachable_links_rejected(self):
        for links in ([('root', 'a', 'l1'), ('a', 'root', 'l2')],
            [('a', 'b', 'orphan')],
            [('a', 'b', 'l1'), ('b', 'a', 'l2')]):
            scene = make_scene()
            scene['documents'] += [document('a'), document('b')]
            scene['links'] = [{'parent_document_id': parent, 'document_id': child, 'link_instance_id': name, 'transform': list(IDENTITY)} for parent,
                child,
                name in links]
            self.assert_error(package(scene), 'package.link')

    def test_included_omitted_link_collision_and_missing_document(self):
        scene = make_scene()
        scene['documents'].append(document('a'))
        scene['links'] = [{'parent_document_id': 'root',
            'document_id': 'a',
            'link_instance_id': 'a',
            'transform': list(IDENTITY)}]
        scene['omitted_links'] = [{'parent_document_id': 'root',
            'link_instance_id': 'a',
            'reason': 'unloaded',
            'impact': 'fixture'}]
        self.assert_error(package(scene))
        scene['omitted_links'] = []
        scene['links'][0]['document_id'] = 'missing'
        self.assert_error(package(scene))

    def test_document_revision_contract(self):
        for changes in ({'method': 'saved_file_sha256'},
            {'saved_file_sha256': '0' * 64},
            {'value': 'invented'},
            {'unsaved_changes': 0}):
            scene = make_scene()
            scene['documents'][0]['revision'].update(changes)
            self.assert_error(package(scene))
        scene = make_scene()
        scene['documents'][0]['revision'].update(method='saved_file_sha256',
            value='saved',
            saved_file_sha256='a' * 64,
            unsaved_changes=True)
        inspect_package_scene(package(scene))

    def test_material_ranges_outside_overlap_order_and_missing_reference(self):
        for ranges in ([{'start_triangle': 1, 'triangle_count': 1, 'material_id': 'plain'}],
            [{'start_triangle': 0, 'triangle_count': 1, 'material_id': 'plain'}] * 2,
            [{'start_triangle': 0, 'triangle_count': 1, 'material_id': 'missing'}]):
            scene = make_scene()
            scene['meshes'][0]['material_ranges'] = ranges
            self.assert_error(package(scene))

    def test_missing_texture_and_geometry_refs_are_integrity_failures(self):
        scene = make_scene()
        scene['materials'][0]['textures'] = [{'slot': 'base_color',
            'path': 'textures/missing.png',
            'color_space': 'srgb'}]
        self.assert_error(package(scene), 'package.reference', 'textures/missing.png')
        scene = make_scene()
        scene['meshes'][0]['vertices'] = 'geometry/absent.bin'
        self.assert_error(package(scene), 'package.reference', 'geometry/absent.bin')
        data = package()
        members = dict(data.members)
        del members['geometry/vertices.bin']
        self.assert_error(replace(data, members=members), 'package.reference', 'geometry/vertices.bin')

    def test_normals_uv_count_type_and_zero_normals(self):
        for role, comps in (('normals', 3), ('uv', 2)):
            scene = make_scene()
            path = f'geometry/{role}.bin'
            scene['meshes'][0][role] = path
            self.assert_error(package(scene, [(path, role, 'float64', comps, [[1] * comps] * 2)]), file=path)
            self.assert_error(package(scene, [(path, role, 'uint32', comps, [[1] * comps] * 3)]), file=path)
        scene = make_scene()
        scene['meshes'][0]['normals'] = 'geometry/normals.bin'
        self.assert_error(package(scene, [('geometry/normals.bin', 'normals', 'float64', 3, [[0, 0, 0]] * 3)]),
            file='geometry/normals.bin')

    def test_actual_capabilities_and_appearance_not_checked(self):
        result = inspect_package_scene(package())
        self.assertEqual(result.capabilities['materials']['state'], 'available')
        for name in ('uv', 'normals', 'ifc', 'coordinates'):
            self.assertEqual(result.capabilities[name]['state'], 'missing')
        self.assertEqual(result.capabilities['parameters']['state'], 'partial')
        self.assertTrue(any((item.rule_id == 'package.material_appearance' and item.status == 'not_checked' for item in result.findings)))
        data = package()
        caps = deepcopy(data.manifest.capabilities)
        for value in caps.values():
            value.update(state='available', evidence=['sender says full'])
        result = inspect_package_scene(replace(data, manifest=replace(data.manifest, capabilities=caps)))
        claims = [item for item in result.findings if item.rule_id == 'package.capability_claim']
        self.assertEqual(len(claims), 6)
        self.assertTrue(all((item.status == 'warn' for item in claims)))

    def test_absent_and_unsupported_material_assignments(self):
        scene = make_scene()
        scene['meshes'][0]['material_ranges'] = []
        self.assertEqual(inspect_package_scene(package(scene)).capabilities['materials']['state'], 'missing')
        scene = make_scene()
        scene['materials'][0]['status'] = 'unsupported'
        result = inspect_package_scene(package(scene))
        self.assertEqual(result.capabilities['materials']['state'], 'unsupported')
        self.assertTrue(result.capabilities['materials']['limitations'])

    def test_empty_scene_has_null_bounds_and_missing_geometry(self):
        scene = make_scene()
        scene['meshes'] = []
        scene['instances'] = []
        scene['materials'] = []
        result = inspect_package_scene(package(scene))
        self.assertIsNone(result.measurements['processing_bounds'])
        self.assertIsNone(result.measurements['project_bounds'])
        self.assertEqual(result.capabilities['geometry']['state'], 'missing')

    def test_snapshot_schema_and_required_fields(self):
        for changes in ({'snapshot_id': 'other'}, {'schema_version': True}, {'root_document_id': 'absent'}):
            scene = make_scene()
            scene.update(changes)
            self.assert_error(package(scene))
        scene = make_scene()
        del scene['coordinates']
        self.assert_error(package(scene))

    def test_coordinate_unknown_and_declared_contract(self):
        for changes in ({'name': 'guessed'},
            {'parameters': {'guess': 1}},
            {'method': 'declared'},
            {'transform': list(IDENTITY)}):
            scene = make_scene()
            scene['coordinates']['regional'].update(changes)
            self.assert_error(package(scene))
        scene = make_scene()
        scene['coordinates']['regional'].update(state='known', name='declared-grid', method='declared')
        result = inspect_package_scene(package(scene))
        self.assertEqual(result.capabilities['coordinates']['state'], 'partial')
        self.assertTrue(any((item.status == 'not_checked' and item.scope == 'external' for item in result.findings)))

    def test_control_residuals_height_and_no_coordinate_upgrade(self):
        scene = make_scene()
        scene['coordinates']['processing_to_project'] = translation(10, 20, 30)
        scene['coordinates']['control_points'] = [{'id': str(n), 'local': local, 'project': project, 'source': 'fixture'} for n,
            local,
            project in [(0, [0, 0, 0], [10, 20, 30]), (1, [1, 0, 0], [11, 20, 30]), (2, [0, 1, 0], [10, 22, 30])]]
        scene['coordinates']['height_check'] = {'local': 2, 'expected': 33, 'source': 'fixture'}
        result = inspect_package_scene(package(scene))
        self.assertEqual(result.measurements['control_point_residuals'],
            [{'id': '0', 'distance': 0}, {'id': '1', 'distance': 0}, {'id': '2', 'distance': 1}])
        self.assertEqual(result.measurements['height_residual'], -1)
        self.assertEqual(result.capabilities['coordinates']['state'], 'partial')
        scene['coordinates']['control_points'].append(deepcopy(scene['coordinates']['control_points'][0]))
        self.assert_error(package(scene))

    def test_collinear_controls_are_recorded_as_incomplete(self):
        scene = make_scene()
        scene['coordinates']['control_points'] = [{'id': str(n),
            'local': [n, n, n],
            'project': [n, n, n],
            'source': 'fixture'} for n in range(3)]
        result = inspect_package_scene(package(scene))
        self.assertTrue(any(('noncollinear' in item['message'] for item in result.capabilities['coordinates']['limitations'])))

    def test_ifc_refs_snapshot_duplicate_guid_missing_key(self):
        for fault in ('path', 'snapshot', 'duplicate', 'key'):
            scene = make_scene()
            key = deepcopy(scene['instances'][0]['element_key'])
            scene['ifc'] = {'path': 'information/model.ifc',
                'snapshot_id': 'synthetic-snapshot',
                'settings': {},
                'element_map': [{'ifc_guid': 'guid', 'element_key': key}]}
            if fault == 'snapshot':
                scene['ifc']['snapshot_id'] = 'other'
            elif fault == 'duplicate':
                scene['ifc']['element_map'] *= 2
            elif fault == 'key':
                key['unique_id'] = 'missing'
            data = package(scene)
            if fault != 'path':
                desc = FileDescriptor('information/model.ifc', 'ifc', 3, '0' * 64, None)
                data = replace(data,
                    manifest=replace(data.manifest, files=data.manifest.files + (desc,), metadata={**data.manifest.metadata, 'ifc_exporter': {'name': 'synthetic', 'version': '1'}}),
                    members={**data.members, desc.path: b'IFC'})
            self.assert_error(data)

    def test_valid_ifc_mapping_remains_partial_not_checked(self):
        scene = make_scene()
        scene['ifc'] = {'path': 'information/model.ifc',
            'snapshot_id': 'synthetic-snapshot',
            'settings': {},
            'element_map': [{'ifc_guid': 'guid', 'element_key': deepcopy(scene['instances'][0]['element_key'])}]}
        data = package(scene)
        desc = FileDescriptor('information/model.ifc', 'ifc', 3, '0' * 64, None)
        data = replace(data,
            manifest=replace(data.manifest, files=data.manifest.files + (desc,), metadata={**data.manifest.metadata, 'ifc_exporter': {'name': 'synthetic', 'version': '1'}}),
            members={**data.members, desc.path: b'IFC'})
        result = inspect_package_scene(data)
        self.assertEqual(result.capabilities['ifc']['state'], 'partial')
        self.assertTrue(any((item.rule_id == 'package.ifc_semantics' and item.status == 'not_checked' for item in result.findings)))

    def test_malformed_mesh_id_is_addressed_input_error(self):
        for value in ([], {}, True, ''):
            scene = make_scene()
            scene['instances'][0]['mesh_id'] = value
            self.assert_error(package(scene), key=scene['instances'][0]['element_key'])

    def test_mesh_resource_error_keeps_source_key(self):
        scene = make_scene()
        scene['meshes'][0]['vertices'] = 'geometry/missing.bin'
        self.assert_error(package(scene),
            'package.reference',
            'geometry/missing.bin',
            scene['instances'][0]['element_key'])

    def test_available_vertex_arrays_have_real_count_and_values(self):
        scene = make_scene()
        scene['meshes'][0].update(normals='geometry/normals.bin', uv='geometry/uv.bin')
        data = package(scene,
            [('geometry/normals.bin', 'normals', 'float64', 3, [[0, 0, 2]] * 3), ('geometry/uv.bin', 'uv', 'float64', 2, [[0, 0], [1, 0], [0, 1]])])
        result = inspect_package_scene(data)
        self.assertEqual(result.capabilities['normals']['state'], 'available')
        self.assertEqual(result.capabilities['uv']['state'], 'available')

    def test_uncovered_triangle_ranges_are_partial(self):
        scene = make_scene()
        scene['meshes'][0]['triangle_count'] = 2
        result = inspect_package_scene(package(scene,
            [('geometry/triangles.bin', 'triangles', 'uint32', 3, [(0, 1, 2), (2, 1, 0)])]))
        self.assertEqual(result.capabilities['materials']['state'], 'partial')
        self.assertTrue(any((item.status == 'warn' for item in result.findings if item.rule_id == 'package.materials')))

    def test_ref_wrong_role_duplicate_slots_and_valid_texture(self):
        scene = make_scene()
        texture = {'slot': 'base_color', 'path': 'textures/base.png', 'color_space': 'srgb'}
        scene['materials'][0]['textures'] = [texture]
        data = package(scene)
        descriptor = FileDescriptor(texture['path'], 'texture', 6, '0' * 64, None)
        data = replace(data,
            manifest=replace(data.manifest, files=data.manifest.files + (descriptor,)),
            members={**data.members, descriptor.path: b'opaque'})
        result = inspect_package_scene(data)
        self.assertEqual(result.capabilities['materials']['state'], 'available')
        self.assertTrue(any((item.rule_id == 'package.material_appearance' and item.status == 'not_checked' for item in result.findings)))
        files = tuple((replace(item,
            role='evidence') if item.path == descriptor.path else item for item in data.manifest.files))
        self.assert_error(replace(data, manifest=replace(data.manifest, files=files)),
            'package.reference',
            descriptor.path)
        scene['materials'][0]['textures'].append(deepcopy(texture))
        data = replace(data, members={**data.members, 'scene.json': json.dumps(scene).encode()})
        self.assert_error(data)

    def test_coordinate_residual_overflow_is_json_serializable_error(self):
        scene = make_scene()
        scene['coordinates']['control_points'] = [{'id': 'p',
            'local': [1e+308, 0, 0],
            'project': [-1e+308, 0, 0],
            'source': 'synthetic'}]
        error = self.assert_error(package(scene))
        json.dumps({'actual': error.actual, 'expected': error.expected}, allow_nan=False)

    def test_scene_bool_counts_and_nonempty_identifier_contract(self):
        for field, value in (('vertex_count', True), ('triangle_count', 0), ('mesh_id', '')):
            scene = make_scene()
            scene['meshes'][0][field] = value
            self.assert_error(package(scene))
        scene = make_scene()
        scene['instances'][0]['element_key']['unique_id'] = 'x' * 513
        self.assert_error(package(scene))

    def test_tiny_nonsingular_transform_has_no_epsilon_rejection(self):
        scene = make_scene()
        scene['instances'][0]['transform'][0] = 1e-300
        result = inspect_package_scene(package(scene))
        self.assertEqual(result.measurements['processing_bounds']['max'][0], 1e-300)

    def test_rendered_work_budget_rejects_before_any_vertex_transform(self):
        scene = make_scene()
        scene['instances'] = [instance('first'), instance('second')]
        for row in scene['instances']:
            row['element_key']['unique_id'] = row['instance_id']
        with patch('model_generator.package_scene._point', side_effect=AssertionError('Vertex transform started before budget rejection')):
            error = self.assert_error(package(scene), 'package.budget', key=scene['instances'][1]['element_key'], limits=PackageLimits(array_items=17))
        self.assertEqual(error.actual, 18)
        self.assertEqual(error.expected, 17)

    def test_rendered_work_budget_accepts_exact_boundary(self):
        scene = make_scene()
        scene['instances'] = [instance('first'), instance('second')]
        for row in scene['instances']:
            row['element_key']['unique_id'] = row['instance_id']
        result = inspect_package_scene(package(scene), PackageLimits(array_items=18))
        self.assertEqual(result.measurements['instances'], 2)
        self.assertEqual(result.measurements['rendered_triangles'], 2)

    def test_ifc_coverage_uses_element_keys_not_guid_count(self):
        scene = make_scene()
        scene['instances'] = [instance('first'), instance('second')]
        for row in scene['instances']:
            row['element_key']['unique_id'] = row['instance_id']
        first_key, second_key = [row['element_key'] for row in scene['instances']]
        scene['ifc'] = {'path': 'information/model.ifc', 'snapshot_id': 'synthetic-snapshot',
                        'settings': {}, 'element_map': [
                            {'ifc_guid': 'guid-1', 'element_key': deepcopy(first_key)},
                            {'ifc_guid': 'guid-2', 'element_key': deepcopy(first_key)}]}
        data = package(scene)
        descriptor = FileDescriptor('information/model.ifc', 'ifc', 3, '0' * 64, None)
        data = replace(data, manifest=replace(data.manifest, files=data.manifest.files + (descriptor,),
                       metadata={**data.manifest.metadata, 'ifc_exporter': {'name': 'synthetic', 'version': '1'}}),
                       members={**data.members, descriptor.path: b'IFC'})
        result = inspect_package_scene(data)
        self.assertTrue(any(item['element_key'] == second_key and 'mapping is incomplete' in item['message']
                            for item in result.capabilities['ifc']['limitations']))
        scene['ifc']['element_map'].append({'ifc_guid': 'guid-3', 'element_key': deepcopy(second_key)})
        data = replace(data, members={**data.members, 'scene.json': json.dumps(scene).encode()})
        result = inspect_package_scene(data)
        self.assertFalse(any('mapping is incomplete' in item['message'] for item in result.capabilities['ifc']['limitations']))
        self.assertEqual(result.capabilities['ifc']['state'], 'partial')

    def test_missing_link_target_keeps_affected_instance_key(self):
        scene = make_scene()
        scene['documents'].append(document('first-link'))
        scene['links'] = [
            {'parent_document_id': 'root', 'document_id': 'first-link', 'link_instance_id': 'first', 'transform': list(IDENTITY)},
            {'parent_document_id': 'first-link', 'document_id': 'absent-target', 'link_instance_id': 'broken', 'transform': list(IDENTITY)}]
        scene['instances'] = [instance('linked', 'absent-target', ['first', 'broken'])]
        error = self.assert_error(package(scene), 'package.link', key=scene['instances'][0]['element_key'])
        self.assertEqual(error.actual, {'parent_document_id': 'first-link', 'link_instance_id': 'broken', 'document_id': 'absent-target'})

    def test_unused_broken_link_keeps_pair_without_fabricated_element_key(self):
        scene = make_scene()
        scene['links'] = [{'parent_document_id': 'root', 'document_id': 'absent-target', 'link_instance_id': 'broken', 'transform': list(IDENTITY)}]
        error = self.assert_error(package(scene), 'package.link')
        self.assertIsNone(error.element_key)
        self.assertEqual(error.actual, {'parent_document_id': 'root', 'link_instance_id': 'broken', 'document_id': 'absent-target'})

    def test_link_address_matches_parent_pair_not_same_link_id_elsewhere(self):
        scene = make_scene()
        scene['documents'] += [document('a'), document('b')]
        scene['links'] = [
            {'parent_document_id': 'root', 'document_id': 'a', 'link_instance_id': 'to-a', 'transform': list(IDENTITY)},
            {'parent_document_id': 'a', 'document_id': 'missing', 'link_instance_id': 'same-id', 'transform': list(IDENTITY)},
            {'parent_document_id': 'root', 'document_id': 'b', 'link_instance_id': 'same-id', 'transform': list(IDENTITY)}]
        scene['instances'] = [instance('unaffected', 'b', ['same-id']), instance('affected', 'missing', ['to-a', 'same-id'])]
        self.assert_error(package(scene), 'package.link', key=scene['instances'][1]['element_key'])

    def test_mesh_reference_amplification_rejects_before_repeat_scans(self):
        scene = make_scene()
        mesh = scene['meshes'][0]
        mesh['triangle_count'] = 1000
        mesh['material_ranges'][0]['triangle_count'] = 1000
        scene['meshes'] = [{**deepcopy(mesh), 'mesh_id': f'mesh-{n}'} for n in range(50)]
        scene['instances'][0]['mesh_id'] = 'mesh-0'
        data = package(scene, [('geometry/triangles.bin', 'triangles', 'uint32', 3, [(0, 1, 2)] * 1000)])
        real_iter_unpack = struct.iter_unpack
        scanned_formats = []
        def unique_float_scan_only(fmt, buffer):
            scanned_formats.append(fmt)
            if fmt != '<d':
                raise AssertionError('Per-mesh repeated scan started before preflight rejection')
            return real_iter_unpack(fmt, buffer)
        with patch('model_generator.package_scene.struct.iter_unpack', side_effect=unique_float_scan_only):
            error = self.assert_error(data, 'package.budget', 'geometry/vertices.bin', limits=PackageLimits(array_items=10000))
        self.assertEqual(error.actual, {'mesh_id': 'mesh-3', 'mesh_array_scalars': 12036})
        self.assertEqual(error.expected, 10000)
        self.assertIsNone(error.element_key)
        self.assertEqual(scanned_formats, ['<d'])
        scene['instances'][0]['mesh_id'] = 'mesh-3'
        data = replace(data, members={**data.members, 'scene.json': json.dumps(scene).encode()})
        self.assert_error(data, 'package.budget', key=scene['instances'][0]['element_key'], limits=PackageLimits(array_items=10000))

    def test_mesh_compute_budget_counts_all_optional_array_reference_uses(self):
        scene = make_scene()
        mesh = scene['meshes'][0]
        mesh.update(triangle_count=1000, normals='geometry/normals.bin', uv='geometry/uv.bin')
        mesh['material_ranges'][0]['triangle_count'] = 1000
        scene['meshes'] = [{**deepcopy(mesh), 'mesh_id': f'mesh-{n}'} for n in range(4)]
        scene['instances'][0]['mesh_id'] = 'mesh-0'
        data = package(scene, [
            ('geometry/triangles.bin', 'triangles', 'uint32', 3, [(0, 1, 2)] * 1000),
            ('geometry/normals.bin', 'normals', 'float64', 3, [[0, 0, 1]] * 3),
            ('geometry/uv.bin', 'uv', 'float64', 2, [[0, 0], [1, 0], [0, 1]])])
        result = inspect_package_scene(data, PackageLimits(array_items=12096))
        self.assertEqual(result.measurements['mesh_triangles'], 4000)
        self.assertEqual(result.measurements['rendered_triangles'], 1000)
        self.assertEqual(result.capabilities['normals']['state'], 'available')
        real_iter_unpack = struct.iter_unpack
        def unique_float_scan_only(fmt, buffer):
            if fmt != '<d':
                raise AssertionError('Per-mesh array loop ran before all-role budget rejection')
            return real_iter_unpack(fmt, buffer)
        with patch('model_generator.package_scene.struct.iter_unpack', side_effect=unique_float_scan_only):
            error = self.assert_error(data, 'package.budget', limits=PackageLimits(array_items=12095))
        self.assertEqual(error.actual, {'mesh_id': 'mesh-3', 'mesh_array_scalars': 12096})

    def test_mesh_compute_budget_includes_repeated_vertex_references(self):
        scene = make_scene()
        mesh = scene['meshes'][0]
        mesh['vertex_count'] = 300
        scene['meshes'] = [{**deepcopy(mesh), 'mesh_id': f'mesh-{n}'} for n in range(4)]
        scene['instances'][0]['mesh_id'] = 'mesh-0'
        data = package(scene, [('geometry/vertices.bin', 'vertices', 'float64', 3, [[0, 0, 0]] * 300)])
        error = self.assert_error(data, 'package.budget', limits=PackageLimits(array_items=3500))
        self.assertEqual(error.actual, {'mesh_id': 'mesh-3', 'mesh_array_scalars': 3612})

    def test_sender_limitations_remain_declared_and_addressed(self):
        data = package()
        caps = deepcopy(data.manifest.capabilities)
        key = make_scene()['instances'][0]['element_key']
        limitation = {'file': 'geometry/vertices.bin', 'element_key': key, 'message': 'Sender reports source fidelity limitation'}
        caps['geometry']['limitations'] = [limitation]
        data = replace(data, manifest=replace(data.manifest, capabilities=caps))
        result = inspect_package_scene(data)
        self.assertEqual(result.capabilities['geometry']['state'], 'available')
        self.assertIn(limitation, result.capabilities['geometry']['limitations'])
        self.assertTrue(any(item.rule_id == 'package.declared_limitation' and item.status == 'warn' for item in result.findings))
        key['unique_id'] = 'absent-source-key'
        self.assert_error(data, 'package.reference', 'geometry/vertices.bin', key)

    def test_scalar_parameter_objects_do_not_accept_nested_values(self):
        for location in ('material', 'coordinate'):
            scene = make_scene()
            obj = scene['materials'][0]['source_parameters'] if location == 'material' else scene['coordinates']['regional']['parameters']
            obj['nested'] = {'fake': 'proof'}
            self.assert_error(package(scene))
if __name__ == '__main__':
    unittest.main()
