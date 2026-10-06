"""Bounded structural inspection of exact portable scene geometry.

Structural availability is deliberately separate from source fidelity, rendered
appearance, external coordinate control and IFC schema/IDS validation.
"""
from dataclasses import dataclass
from fractions import Fraction
import math
import re
import struct
import unicodedata
from .diagnostics import Finding
from .package_manifest import ARRAY_ROLES, PackageError, PackageLimits, decode_package_json
from .package_reader import PackageData


@dataclass(frozen=True)


class SceneInspection:
    scene: dict[str, object]
    measurements: dict[str, object]
    capabilities: dict[str, dict[str, object]]
    findings: tuple[Finding, ...]
_IDENTITY = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _error(message, *, rule='package.schema', file='scene.json', key=None, actual=None, expected=None):
    # Error payloads must serialize with allow_nan=False.
    if isinstance(actual, float) and (not math.isfinite(actual)):
        actual = repr(actual)
    raise PackageError(rule, message, file=file, element_key=key, actual=actual, expected=expected)


def _object(value, fields, *, key=None):
    if not isinstance(value, dict) or set(value) - set(fields) - {'extensions'} or set(fields) - set(value):
        _error('Missing or unknown object fields', key=key)
    if 'extensions' in value and (not isinstance(value['extensions'], dict)):
        _error('extensions must be an object', key=key)
    return value


def _id(value, key=None):
    if (not isinstance(value, str) or not value
            or len(value.encode('utf-8')) > 512
            or any(unicodedata.category(c) in {'Cc', 'Cs'} for c in value)):
        _error('Invalid identifier', key=key)
    return value


def _text(value):
    if not isinstance(value, str) or not value:
        _error('Expected a nonempty string')
    return value


def _enum(value, choices, key=None):
    if not isinstance(value, str) or value not in choices:
        _error('Unknown enumerated value', key=key, actual=value, expected=sorted(choices))
    return value


def _integer(value, minimum=0, *, file='scene.json', key=None):
    if type(value) is not int or value < minimum:
        _error('Expected an integer', file=file, key=key, actual=value, expected=f'integer >= {minimum}')
    return value


def _number(value, *, file='scene.json', key=None):
    try:
        if type(value) not in {int, float}:
            raise ValueError
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
    except (ValueError, OverflowError):
        _error('Expected a finite float64-compatible number',
            file=file,
            key=key,
            actual=repr(value),
            expected='finite float64')
    return result


def _vector(value, *, key=None):
    if not isinstance(value, list) or len(value) != 3:
        _error('Expected three numbers', key=key)
    return tuple((_number(item, key=key) for item in value))


def _determinant(matrix):
    # Exact float64 rational sign avoids epsilon and rank underflow.
    a, b, c, d, e, f, g, h, i = (Fraction(matrix[n]) for n in (0, 1, 2, 4, 5, 6, 8, 9, 10))
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _matrix(value, key=None):
    if not isinstance(value, list) or len(value) != 16:
        _error('Expected row-major Matrix4 with sixteen numbers', key=key)
    matrix = tuple((_number(item, key=key) for item in value))
    if matrix[12:] != (0, 0, 0, 1) or not _determinant(matrix):
        _error('Matrix must be affine and nonsingular', rule='package.transform', key=key)
    return matrix


def _finite_sum(products, key=None):
    try:
        result = math.fsum(products)
    except (OverflowError, ValueError):
        result = math.inf
    if not math.isfinite(result):
        _error('Computed transformation overflow',
            rule='package.transform',
            key=key,
            actual='non-finite',
            expected='finite float64')
    return result


def _compose(left, right, key=None):
    return tuple((_finite_sum((left[r * 4 + k] * right[k * 4 + c] for k in range(4)),
        key) for r in range(4) for c in range(4)))


def _point(matrix, vector, key=None):
    return tuple((_finite_sum([matrix[r * 4 + c] * vector[c] for c in range(3)] + [matrix[r * 4 + 3]],
        key) for r in range(3)))


def _scalars(value):
    if not isinstance(value, dict):
        _error('Expected scalar parameter object')
    for item in value.values():
        if item is None or type(item) in {str, bool}:
            continue
        _number(item)
    return value


def _key(value):
    obj = _object(value,
        {'document_id', 'link_instance_path', 'unique_id'},
        key=value if isinstance(value, dict) else None)
    _id(obj['document_id'], obj)
    _id(obj['unique_id'], obj)
    if not isinstance(obj['link_instance_path'], list):
        _error('Expected link instance path list', key=obj)
    for item in obj['link_instance_path']:
        _id(item, obj)
    return (obj['document_id'], tuple(obj['link_instance_path']), obj['unique_id'])


class _Inspector:

    def __init__(self, package, limits):
        self.package = package
        self.limits = limits
        self.files = {item.path: item for item in package.manifest.files}
        self.scene = None
        self.records = 0
        self.findings = []
        self.capabilities = {name: {'state': 'missing',
            'evidence': [],
            'limitations': []} for name in package.manifest.capabilities}

    def rows(self, value):
        if not isinstance(value, list):
            _error('Expected a scene record list')
        self.records += len(value)
        if self.records > self.limits.scene_records:
            _error('Total scene record budget exceeded',
                rule='package.budget',
                actual=self.records,
                expected=self.limits.scene_records)
        return value

    def reference(self, path, role, key=None):
        if not isinstance(path, str) or path not in self.files or path not in self.package.members:
            _error('Missing package resource reference',
                rule='package.reference',
                file=path if isinstance(path, str) else 'scene.json',
                key=key)
        descriptor = self.files[path]
        if descriptor.role != role:
            _error('Resource has the wrong role',
                rule='package.reference',
                file=path,
                key=key,
                actual=descriptor.role,
                expected=role)
        return descriptor

    def array_layouts(self):
        scalars = 0
        for descriptor in self.files.values():
            if descriptor.role not in ARRAY_ROLES:
                continue
            self.reference(descriptor.path, descriptor.role)
            array = descriptor.array
            dtype, components, stride = ARRAY_ROLES[descriptor.role]
            if array is None or array.dtype != dtype or array.byte_order != 'little' or (type(array.components) is not int) or (array.components != components) or (type(array.stride_bytes) is not int) or (array.stride_bytes != stride):
                _error('Array layout disagrees with exact role', file=descriptor.path)
            count = _integer(array.count, file=descriptor.path)
            if descriptor.bytes != count * stride or len(self.package.members[descriptor.path]) != count * stride:
                _error('Array byte length disagrees with count/stride',
                    rule='package.size',
                    file=descriptor.path)
            scalars += count * components
            if count * components > self.limits.array_items or scalars > self.limits.array_items:
                _error('Array scalar budget exceeded',
                    rule='package.budget',
                    file=descriptor.path,
                    actual=scalars,
                    expected=self.limits.array_items)
            # Unreferenced arrays cannot conceal nonfinite scalars.
            if dtype == 'float64':
                for value, in struct.iter_unpack('<d', self.package.members[descriptor.path]):
                    _number(value, file=descriptor.path)

    def unique(self, rows, fields, id_field):
        result = {}
        for row in self.rows(rows):
            row = _object(row, fields)
            identifier = _id(row[id_field])
            if identifier in result:
                _error('Duplicate scene record identifier', actual=identifier)
            result[identifier] = row
        return result

    def documents(self, scene):
        documents = self.unique(scene['documents'], {'document_id', 'revision'}, 'document_id')
        if scene['root_document_id'] not in documents:
            _error('Root document does not exist', rule='package.reference')
        for document in documents.values():
            rev = _object(document['revision'], {'value', 'method', 'saved_file_sha256', 'unsaved_changes'})
            method = _enum(rev['method'], {'saved_file_sha256', 'revit_revision', 'unknown'})
            if type(rev['unsaved_changes']) is not bool:
                _error('unsaved_changes must be boolean')
            if rev['value'] is not None:
                _id(rev['value'])
            if method == 'unknown' and rev['value'] is not None:
                _error('Unknown revision requires value=null')
            digest = rev['saved_file_sha256']
            if method == 'saved_file_sha256':
                if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
                    _error('Saved-file method requires SHA-256')
            elif digest is not None:
                _error('Other revision methods require saved_file_sha256=null')
        return documents

    def affected_link_key(self, scene, offending_pair):
        """Resolve raw root paths only to locate a structurally valid source key."""
        if not isinstance(scene['instances'], list):
            return None
        targets = {}
        for row in scene['links']:
            if isinstance(row, dict) and all(isinstance(row.get(field), str) for field in
                    ('parent_document_id', 'link_instance_id', 'document_id')):
                targets.setdefault((row['parent_document_id'], row['link_instance_id']), row['document_id'])
        for instance in scene['instances']:
            if not isinstance(instance, dict):
                continue
            key = instance.get('element_key')
            try:
                _, path, _ = _key(key)
            except PackageError:
                continue
            parent = scene['root_document_id']
            seen = {parent}
            for name in path:
                pair = (parent, name)
                if pair == offending_pair:
                    return key
                if pair not in targets or targets[pair] in seen:
                    break
                parent = targets[pair]
                seen.add(parent)
        return None

    def links(self, scene, documents):
        links, outgoing = ({}, {name: [] for name in documents})
        for row in self.rows(scene['links']):
            row = _object(row, {'link_instance_id', 'parent_document_id', 'document_id', 'transform'})
            parent, child, name = (_id(row[field]) for field in ('parent_document_id',
                'document_id',
                'link_instance_id'))
            if parent not in documents or child not in documents or (parent, name) in links:
                _error('Invalid or duplicate link document reference',
                       rule='package.link', key=self.affected_link_key(scene, (parent, name)),
                       actual={'parent_document_id': parent, 'link_instance_id': name, 'document_id': child})
            matrix = _matrix(row['transform'])
            links[parent, name] = (child, matrix)
            outgoing[parent].append(child)
        # Check the entire graph, including links unused by instances.
        indegree = dict.fromkeys(documents, 0)
        for children in outgoing.values():
            for child in children:
                indegree[child] += 1
        pending = [name for name, degree in indegree.items() if degree == 0]
        visited = 0
        while pending:
            parent = pending.pop()
            visited += 1
            for child in outgoing[parent]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    pending.append(child)
        if visited != len(documents):
            _error('Link graph contains a cycle', rule='package.link')
        reached = {scene['root_document_id']}
        pending = list(reached)
        while pending:
            for child in outgoing[pending.pop()]:
                if child not in reached:
                    reached.add(child)
                    pending.append(child)
        if any((parent not in reached for parent, _ in links)):
            _error('Link graph is unreachable from root', rule='package.link')
        omitted = set()
        for row in self.rows(scene['omitted_links']):
            _object(row, {'link_instance_id', 'parent_document_id', 'reason', 'impact'})
            pair = (_id(row['parent_document_id']), _id(row['link_instance_id']))
            if pair[0] not in documents or pair in links or pair in omitted:
                _error('Invalid or duplicate omitted link', rule='package.link')
            omitted.add(pair)
            _enum(row['reason'], {'excluded', 'unloaded', 'unresolved'})
            _text(row['impact'])
            self.limit('links',
                row['impact'],
                actual={'link_instance_id': pair[1], 'parent_document_id': pair[0]})
        self.state('links',
            'partial' if omitted and links else 'missing' if not links else 'available',
            'Link graph and transforms structurally validated')
        return links

    def materials(self, scene):
        materials = self.unique(scene['materials'],
            {'material_id', 'status', 'source_parameters', 'color_space', 'alpha_mode', 'textures'},
            'material_id')
        for material in materials.values():
            _enum(material['status'], {'available', 'unsupported'})
            _scalars(material['source_parameters'])
            _enum(material['color_space'], {'srgb', 'linear', 'unknown'})
            _enum(material['alpha_mode'], {'opaque', 'mask', 'blend', 'unknown'})
            slots = set()
            for texture in self.rows(material['textures']):
                _object(texture, {'slot', 'path', 'color_space'})
                slot = _enum(texture['slot'], {'base_color', 'normal', 'roughness', 'metallic', 'opacity'})
                if slot in slots:
                    _error('Duplicate material texture slot')
                slots.add(slot)
                _enum(texture['color_space'], {'srgb', 'linear', 'unknown'})
                self.reference(texture['path'], 'texture')
        return materials

    def mesh_source_key(self, scene, mesh_id):
        """Locate a source key for this mesh use, not another shared-file user."""
        if not isinstance(scene['instances'], list):
            return None
        for instance in scene['instances']:
            if not isinstance(instance, dict) or instance.get('mesh_id') != mesh_id:
                continue
            key = instance.get('element_key')
            try:
                _key(key)
            except PackageError:
                continue
            return key
        return None

    def preflight_mesh_arrays(self, scene, meshes):
        """Bound cumulative reference work before any per-mesh binary loop."""
        mesh_array_scalars = 0
        for mesh in meshes.values():
            vertex_count = _integer(mesh['vertex_count'], 1)
            triangle_count = _integer(mesh['triangle_count'], 1)
            for role, count in (('vertices', vertex_count), ('triangles', triangle_count),
                                ('normals', vertex_count), ('uv', vertex_count)):
                path = mesh[role]
                if path is None and role in {'normals', 'uv'}:
                    continue
                descriptor = self.reference(path, role)
                if descriptor.array.count != count:
                    _error('Mesh count disagrees with array descriptor', file=path,
                           actual=count, expected=descriptor.array.count)
                mesh_array_scalars += descriptor.array.count * descriptor.array.components
            if mesh_array_scalars > self.limits.array_items:
                _error('Cumulative mesh array reference work budget exceeded',
                       rule='package.budget', file=mesh['vertices'],
                       key=self.mesh_source_key(scene, mesh['mesh_id']),
                       actual={'mesh_id': mesh['mesh_id'], 'mesh_array_scalars': mesh_array_scalars},
                       expected=self.limits.array_items)

    def meshes(self, scene, materials):
        meshes = self.unique(scene['meshes'],
            {'mesh_id', 'vertices', 'triangles', 'vertex_count', 'triangle_count', 'normals', 'uv', 'material_ranges'},
            'mesh_id')
        self.preflight_mesh_arrays(scene, meshes)
        assigned = supported = total = 0
        for mesh in meshes.values():
            vertex_count = mesh['vertex_count']
            triangle_count = mesh['triangle_count']
            total += triangle_count
            for triangle in struct.iter_unpack('<III', self.package.members[mesh['triangles']]):
                if any(index >= vertex_count for index in triangle):
                    _error('Triangle index outside vertex array',
                           rule='package.geometry', file=mesh['triangles'],
                           actual=list(triangle), expected=f'index < {vertex_count}')
            if mesh['normals'] is not None:
                for normal in struct.iter_unpack('<ddd', self.package.members[mesh['normals']]):
                    if normal == (0, 0, 0):
                        _error('Zero-length vertex normal', rule='package.geometry', file=mesh['normals'])
            previous_end = 0
            for row in self.rows(mesh['material_ranges']):
                _object(row, {'start_triangle', 'triangle_count', 'material_id'})
                start = _integer(row['start_triangle'])
                count = _integer(row['triangle_count'], 1)
                name = _id(row['material_id'])
                if start < previous_end or start + count > triangle_count:
                    _error('Material range overlaps, is unsorted or outside mesh',
                        rule='package.material',
                        file=mesh['triangles'])
                if name not in materials:
                    _error('Missing material reference',
                        rule='package.reference',
                        file=mesh['triangles'],
                        actual=name)
                previous_end = start + count
                assigned += count
                if materials[name]['status'] == 'available':
                    supported += count
                else:
                    self.limit('materials',
                        'Unsupported source material',
                        file=mesh['triangles'],
                        actual={'material_id': name})
        self.state('materials',
            'missing' if not assigned else 'unsupported' if not supported else 'available' if supported == total else 'partial',
            'Material assignments and resource references structurally validated')
        if supported < total:
            self.limit('materials', 'Some triangles have no supported material assignment')
        for role in ('normals', 'uv'):
            present = sum((mesh[role] is not None for mesh in meshes.values()))
            self.state(role,
                'missing' if not present else 'available' if present == len(meshes) else 'partial',
                'Vertex-indexed arrays structurally validated')
            if present < len(meshes):
                self.limit(role, 'Some meshes explicitly omit this vertex array')
        if materials:
            self.limit('materials',
                'Material appearance, texture pixel decoding and render matching are not checked',
                status='not_checked',
                rule='package.material_appearance')
        return meshes

    def state(self, area, state, evidence):
        self.capabilities[area]['state'] = state
        self.capabilities[area]['evidence'] = [evidence] if state in {'available', 'partial'} else []

    def limit(self,
        area,
        message,
        *,
        file='scene.json',
        key=None,
        actual=None,
        status='warn',
        scope='technical',
        rule=None):
        self.capabilities[area]['limitations'].append({'file': file, 'element_key': key, 'message': message})
        if key is not None:
            actual = {'element_key': key, 'detail': actual}
        self.findings.append(Finding(rule or 'package.' + area,
            status,
            file,
            actual,
            'structural availability only' if status == 'not_checked' else 'complete source data',
            message,
            scope))

    def instances(self, scene, meshes, documents, links, project):
        instances = self.unique(scene['instances'],
            {'instance_id', 'mesh_id', 'element_key', 'transform', 'element_id'},
            'instance_id')
        keys, processing, project_bounds = (set(), None, None)
        rendered = mirrored = 0
        transformed_scalars = 0
        # Preflight every placement before the first geometry transform.
        for instance in instances.values():
            key = instance['element_key']
            signature = _key(key)
            if signature in keys:
                _error('Duplicate compound element key', rule='package.reference', key=key)
            keys.add(signature)
            _id(instance['mesh_id'], key)
            if signature[0] not in documents or instance['mesh_id'] not in meshes:
                _error('Instance refers to absent document or mesh', rule='package.reference', key=key)
            if instance['element_id'] is not None:
                _integer(instance['element_id'], key=key)
            transformed_scalars += meshes[instance['mesh_id']]['vertex_count'] * 3
            if transformed_scalars > self.limits.array_items:
                _error('Rendered vertex scalar work budget exceeded',
                       rule='package.budget', key=key,
                       actual=transformed_scalars, expected=self.limits.array_items)
        for instance in instances.values():
            key = instance['element_key']
            signature = _key(key)
            parent = scene['root_document_id']
            chain = _IDENTITY
            seen = {parent}
            for name in signature[1]:
                if (parent, name) not in links:
                    _error('Element link path cannot be resolved', rule='package.link', key=key)
                child, transform = links[parent, name]
                if child in seen:
                    _error('Repeated document in element link path', rule='package.link', key=key)
                seen.add(child)
                parent = child
                chain = _compose(chain, transform, key)
            if parent != signature[0]:
                _error('Element path ends in a different document', rule='package.link', key=key)
            transform = _compose(chain, _matrix(instance['transform'], key), key)
            mirrored += _determinant(transform) < 0
            project_transform = _compose(project, transform, key)
            mesh = meshes[instance['mesh_id']]
            rendered += mesh['triangle_count']
            for vertex in struct.iter_unpack('<ddd', self.package.members[mesh['vertices']]):
                processing = _bounds(processing, _point(transform, vertex, key))
                project_bounds = _bounds(project_bounds, _point(project_transform, vertex, key))
            for role in ('normals', 'uv'):
                if mesh[role] is None:
                    self.limit(role, 'Instance mesh omits vertex data', file=mesh['vertices'], key=key)
        self.state('geometry',
            'available' if meshes and instances else 'missing',
            'Exact mesh arrays, instance paths and bounds structurally validated')
        return (instances, keys, processing, project_bounds, rendered, int(mirrored))

    def coordinates(self, scene):
        coords = _object(scene['coordinates'],
            {'units', 'axes', 'processing_origin', 'processing_to_project', 'shared', 'regional', 'vertical', 'control_points', 'height_check'})
        _enum(coords['units'], {'metre'})
        _enum(coords['axes'], {'right-handed-z-up'})
        _vector(coords['processing_origin'])
        project = _matrix(coords['processing_to_project'])
        known = 0
        for name in ('shared', 'regional', 'vertical'):
            system = _object(coords[name], {'state', 'name', 'zone', 'method', 'transform', 'parameters'})
            state = _enum(system['state'], {'known', 'unknown'})
            method = _enum(system['method'], {'affine', 'declared', 'unknown'})
            _scalars(system['parameters'])
            if state == 'unknown':
                if any((system[field] is not None for field in ('name',
                    'zone',
                    'transform'))) or method != 'unknown' or system['parameters']:
                    _error('Unknown coordinate system must have explicit null/empty fields')
            else:
                known += 1
                _text(system['name'])
                if system['zone'] is not None and (not isinstance(system['zone'], str)):
                    _error('Coordinate zone must be string or null')
                if method == 'unknown' or (method == 'declared' and system['transform'] is not None):
                    _error('Invalid known coordinate method')
                if method == 'affine':
                    _matrix(system['transform'])
        points, ids, residuals = ([], set(), [])
        for row in self.rows(coords['control_points']):
            _object(row, {'id', 'local', 'project', 'source'})
            name = _id(row['id'])
            if name in ids:
                _error('Duplicate control point id')
            ids.add(name)
            local = _vector(row['local'])
            expected = _vector(row['project'])
            _text(row['source'])
            actual = _point(project, local)
            distance = math.dist(actual, expected)
            residuals.append({'id': name, 'distance': _number(distance)})
            points.append(local)
        height = None
        if coords['height_check'] is not None:
            row = _object(coords['height_check'], {'local', 'expected', 'source'})
            local = _number(row['local'])
            expected = _number(row['expected'])
            _text(row['source'])
            height = _number(_point(project, (0, 0, local))[2] - expected)
        noncollinear = _noncollinear(points)
        self.state('coordinates',
            'partial' if known or points or height is not None else 'missing',
            'Coordinate fields and computed residuals structurally inspected')
        if not noncollinear:
            self.limit('coordinates', 'At least three noncollinear local control points are required')
        if height is None:
            self.limit('coordinates', 'Height control is explicitly absent')
        if known < 3:
            self.limit('coordinates', 'Some coordinate systems are explicitly unknown')
        self.limit('coordinates',
            'Regional method and external control point fidelity are not checked',
            status='not_checked',
            scope='external',
            rule='package.coordinate_control')
        return (project, residuals, height)

    def ifc(self, scene, keys):
        value = scene['ifc']
        if value is not None:
            row = _object(value, {'path', 'snapshot_id', 'settings', 'element_map'})
            self.reference(row['path'], 'ifc')
            if self.package.manifest.metadata['ifc_exporter'] is None:
                _error('IFC resource requires exporter metadata')
            if row['snapshot_id'] != scene['snapshot_id']:
                _error('IFC snapshot differs from scene', rule='package.snapshot', file=row['path'])
            _scalars(row['settings'])
            guids = set()
            mapped_keys = set()
            for mapping in self.rows(row['element_map']):
                _object(mapping, {'ifc_guid', 'element_key'})
                guid = _id(mapping['ifc_guid'])
                key = mapping['element_key']
                if guid in guids:
                    _error('Duplicate IFC GUID', file=row['path'], key=key)
                guids.add(guid)
                signature = _key(key)
                if signature not in keys:
                    _error('IFC map refers to absent element key',
                        rule='package.reference',
                        file=row['path'],
                        key=key)
                mapped_keys.add(signature)
            self.state('ifc',
                'partial',
                'IFC resource, snapshot and mapping references structurally validated')
            if mapped_keys != keys:
                for instance in scene['instances']:
                    key = instance['element_key']
                    if _key(key) not in mapped_keys:
                        self.limit('ifc', 'IFC element mapping is incomplete',
                                   file=row['path'], key=key)
        else:
            self.limit('ifc', 'IFC reference is explicitly absent')
        self.limit('ifc',
            'IFC schema, IDS and source fidelity are not checked',
            status='not_checked',
            rule='package.ifc_semantics')

    def address_resource_error(self, error):
        """Attach an affected source key without accepting malformed records."""
        if error.element_key is not None or self.scene is None:
            return
        # Mesh compute-budget errors already identify the exact reference use.
        if error.rule == 'package.budget' and isinstance(error.actual, dict) and 'mesh_array_scalars' in error.actual:
            return
        scene = self.scene
        if not all((isinstance(scene.get(name), list) for name in ('materials', 'meshes', 'instances'))):
            return
        affected_materials = set()
        for material in scene['materials']:
            if not isinstance(material, dict) or not isinstance(material.get('material_id'), str):
                continue
            textures = material.get('textures')
            if isinstance(textures,
                list) and any((isinstance(ref, dict) and ref.get('path') == error.file for ref in textures)):
                affected_materials.add(material['material_id'])
        affected_meshes = set()
        for mesh in scene['meshes']:
            if not isinstance(mesh, dict) or not isinstance(mesh.get('mesh_id'), str):
                continue
            affected = any((mesh.get(role) == error.file for role in ARRAY_ROLES))
            ranges = mesh.get('material_ranges')
            if isinstance(ranges, list):
                affected = affected or any((isinstance(row,
                    dict) and isinstance(row.get('material_id'),
                    str) and (row['material_id'] in affected_materials) for row in ranges))
            if affected:
                affected_meshes.add(mesh['mesh_id'])
        for instance in scene['instances']:
            if not isinstance(instance,
                dict) or not isinstance(instance.get('mesh_id'),
                str) or instance['mesh_id'] not in affected_meshes:
                continue
            key = instance.get('element_key')
            try:
                _key(key)
            except PackageError:
                continue
            error.element_key = key
            return

    def inspect(self):
        self.reference(self.package.manifest.scene_path, 'scene')
        scene = decode_package_json(self.package.members[self.package.manifest.scene_path],
            file=self.package.manifest.scene_path,
            byte_budget=self.limits.scene_bytes,
            limits=self.limits)
        self.scene = scene
        _object(scene,
            {'schema_version', 'snapshot_id', 'root_document_id', 'documents', 'links', 'omitted_links', 'meshes', 'instances', 'materials', 'coordinates', 'ifc'})
        if type(scene['schema_version']) is not int or scene['schema_version'] != 1:
            _error('Unsupported scene schema', rule='package.unsupported')
        _id(scene['snapshot_id'])
        _id(scene['root_document_id'])
        if scene['snapshot_id'] != self.package.manifest.metadata['snapshot_id']:
            _error('Scene snapshot differs from manifest', rule='package.snapshot')
        self.array_layouts()
        documents = self.documents(scene)
        links = self.links(scene, documents)
        materials = self.materials(scene)
        meshes = self.meshes(scene, materials)
        project, residuals, height = self.coordinates(scene)
        instances, keys, bounds, project_bounds, rendered, mirrored = self.instances(scene,
            meshes,
            documents,
            links,
            project)
        self.ifc(scene, keys)
        self.state('parameters',
            'partial' if documents else 'missing',
            'Document revisions and instance identifiers structurally inspected')
        self.limit('parameters',
            'Parameter completeness and provenance fidelity are not checked',
            status='not_checked',
            rule='package.parameter_fidelity')
        for area, capability in self.package.manifest.capabilities.items():
            for declared in capability['limitations']:
                key = declared['element_key']
                if key is not None and _key(key) not in keys:
                    _error('Declared limitation refers to an absent source key',
                           rule='package.reference', file=declared['file'] or 'manifest.json', key=key)
                self.capabilities[area]['limitations'].append(dict(declared))
                self.findings.append(Finding(
                    'package.declared_limitation', 'warn', declared['file'] or 'manifest.json',
                    {'area': area, 'element_key': key, 'sender_declared': True},
                    'source fidelity remains unverified', declared['message']))
        for name, actual in self.capabilities.items():
            incoming = self.package.manifest.capabilities[name]['state']
            inflated = incoming == 'available' and actual['state'] != 'available' or (incoming == 'partial' and actual['state'] in {'missing',
                'unsupported'})
            if inflated:
                self.findings.append(Finding('package.capability_claim',
                    'warn',
                    'manifest.json',
                    {'area': name, 'state': incoming},
                    {'area': name, 'state': actual['state']},
                    'Sender capability claim exceeds structurally verified availability'))
        measurements = {'meshes': len(meshes),
            'instances': len(instances),
            'mesh_vertices': sum((mesh['vertex_count'] for mesh in meshes.values())),
            'mesh_triangles': sum((mesh['triangle_count'] for mesh in meshes.values())),
            'rendered_triangles': rendered,
            'processing_bounds': bounds,
            'project_bounds': project_bounds,
            'mirrored_instances': mirrored,
            'control_point_residuals': residuals,
            'height_residual': height}
        return SceneInspection(scene, measurements, self.capabilities, tuple(self.findings))


def _bounds(bounds, point):
    if bounds is None:
        return {'min': list(point), 'max': list(point)}
    for n, value in enumerate(point):
        bounds['min'][n] = min(bounds['min'][n], value)
        bounds['max'][n] = max(bounds['max'][n], value)
    return bounds


def _noncollinear(points):
    if len(points) < 3:
        return False
    origin = tuple((Fraction(value) for value in points[0]))
    direction = None
    for point in points[1:]:
        vector = tuple((Fraction(value) - base for value, base in zip(point, origin)))
        if not any(vector):
            continue
        if direction is None:
            direction = vector
            continue
        a, b, c = direction
        d, e, f = vector
        if any((b * f - c * e, c * d - a * f, a * e - b * d)):
            return True
    return False


def inspect_package_scene(package: PackageData, limits: PackageLimits | None=None) -> SceneInspection:
    """Inspect data in memory; never open, decode pixels or execute resources."""
    inspector = _Inspector(package, limits or PackageLimits())
    try:
        return inspector.inspect()
    except PackageError as error:
        inspector.address_resource_error(error)
        raise
    except (struct.error, UnicodeError, RecursionError):
        raise PackageError('package.geometry', 'Malformed scene or binary array', file='scene.json') from None
