import unittest
from dataclasses import replace

from fixtures.builders import scene_bytes
from model_generator.fbx_binary import Node, parse_fbx
from model_generator.limits import Limits, ReadError
from model_generator.fbx_scene import read_scene


def fixture():
    roots = parse_fbx(scene_bytes())
    settings = roots[0].child('Properties70')
    for key, value in [('UpAxis', 2), ('UpAxisSign', 1), ('FrontAxis', 1),
                       ('FrontAxisSign', -1), ('CoordAxis', 0), ('CoordAxisSign', 1)]:
        settings.children.append(Node('P', [key, '', '', '', value], []))
    return roots


def property_node(key, values):
    return Node('P', [key, '', '', '', *values], [])


class SceneTests(unittest.TestCase):
    def test_translation_is_preserved_and_units_apply_to_vertices_and_origin(self):
        roots = fixture()
        roots[0].child('Properties70').children[0].props[-1] = 1
        scene = read_scene(roots)
        mesh = scene.meshes[0]
        self.assertEqual(tuple(mesh.positions), (.1, 0, 0, .11, 0, 0, .1, .01, 0))
        self.assertEqual(tuple(mesh.triangles), (0, 1, 2))
        self.assertEqual(mesh.material_ids, (5,))
        self.assertEqual(scene.bounds, ((.1, 0, 0), (.11, .01, 0)))

    def test_shared_geometry_keeps_each_model_placement(self):
        roots = fixture()
        roots[1].children.append(Node('Model', [6, 'second', 'Mesh'], [
            Node('Properties70', [], [property_node('Lcl Translation', [0, 20, 0])])]))
        roots[2].children.append(Node('C', ['OO', 1, 6], []))
        scene = read_scene(roots)
        self.assertEqual(len(scene.meshes), 2)
        self.assertEqual(tuple(scene.meshes[1].positions), (0, 20, 0, 1, 20, 0, 0, 21, 0))
        self.assertEqual(scene.bounds, ((0, 0, 0), (11, 21, 0)))

    def test_unknown_units_or_axes_are_rejected(self):
        for key in ('UnitScaleFactor', 'UpAxis'):
            roots = fixture()
            props = roots[0].child('Properties70')
            props.children = [p for p in props.children if p.props[0] != key]
            with self.subTest(key=key), self.assertRaises(ReadError):
                read_scene(roots)

    def test_nonidentity_rotation_scale_and_pivot_are_rejected(self):
        for key, value in [('Lcl Rotation', [0, 0, 90]), ('Lcl Scaling', [2, 1, 1]),
                           ('GeometricTranslation', [1, 0, 0]), ('RotationPivot', [1, 0, 0])]:
            roots = fixture()
            roots[1].children[1].child('Properties70').children.append(property_node(key, value))
            with self.subTest(key=key), self.assertRaises(ReadError):
                read_scene(roots)

    def test_hierarchy_is_rejected_instead_of_flattened(self):
        roots = fixture()
        roots[1].children.append(Node('Model', [6, 'parent', 'Null'], []))
        roots[2].children.append(Node('C', ['OO', 2, 6], []))
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_unplaced_geometry_is_rejected(self):
        roots = fixture()
        roots[2].children = [n for n in roots[2].children if n.props[1] != 1]
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_instancing_obeys_expanded_geometry_budget(self):
        with self.assertRaises(ReadError):
            read_scene(fixture(), replace(Limits(), fbx_array_bytes=80))

    def test_duplicate_properties_and_invalid_geometry_are_rejected(self):
        roots = fixture()
        roots[0].child('Properties70').children.append(property_node('UnitScaleFactor', [100]))
        with self.assertRaises(ReadError):
            read_scene(roots)
        roots = fixture()
        roots[1].children[0].child('PolygonVertexIndex').props[0][2] = -10
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_nonidentity_template_transform_is_not_ignored(self):
        roots = fixture()
        roots.append(Node('Definitions', [], [Node('ObjectType', ['Model'], [
            Node('PropertyTemplate', ['FbxNode'], [Node('Properties70', [], [
                property_node('Lcl Scaling', [2, 2, 2])])])])]))
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_material_polygon_mapping_is_preserved_and_checked(self):
        roots = fixture()
        geo = roots[1].children[0]
        geo.children.append(Node('LayerElementMaterial', [0], [
            Node('MappingInformationType', ['AllSame'], []),
            Node('ReferenceInformationType', ['IndexToDirect'], []),
            Node('Materials', [[0]], [])]))
        self.assertEqual(tuple(read_scene(roots).meshes[0].material_indices), (0,))
        geo.child('LayerElementMaterial').child('Materials').props[0] = [1]
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_repeated_texture_link_does_not_duplicate_scene_placements(self):
        roots = fixture()
        roots[2].children.append(Node('C', ['OO', 3, 4], []))
        self.assertEqual(len(read_scene(roots).meshes), 1)

    def test_empty_unrelated_metadata_is_retained(self):
        roots = fixture()
        roots[0].child('Properties70').children.append(Node('P', ['TimeMarker', '', '', ''], []))
        self.assertEqual(len(read_scene(roots).meshes), 1)

    def test_ambiguous_authoritative_nodes_are_rejected(self):
        for kind in ('Properties70', 'Vertices', 'PolygonVertexIndex', 'Definitions', 'Materials'):
            roots = fixture()
            if kind == 'Properties70':
                roots[1].children[1].children.append(Node(kind, [], [property_node('Lcl Scaling', [2, 2, 2])]))
            elif kind == 'Definitions':
                roots.extend([Node(kind, [], []), Node(kind, [], [])])
            elif kind == 'Materials':
                roots[1].children[0].children.append(Node('LayerElementMaterial', [0], [
                    Node(kind, [[0]], []), Node(kind, [[1]], [])]))
            else:
                geo = roots[1].children[0]
                geo.children.append(Node(kind, geo.child(kind).props, []))
            with self.subTest(kind=kind), self.assertRaises(ReadError):
                read_scene(roots)

    def test_positive_source_unit_that_underflows_is_rejected(self):
        roots = fixture()
        roots[0].child('Properties70').children[0].props[-1] = 5e-324
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_instance_budget_counts_all_placements_and_accepts_boundary(self):
        roots = fixture()
        self.assertEqual(len(read_scene(roots, replace(Limits(), fbx_array_bytes=96)).meshes), 1)
        roots[1].children.append(Node('Model', [6, 'second', 'Mesh'], []))
        roots[2].children.append(Node('C', ['OO', 1, 6], []))
        with self.assertRaises(ReadError):
            read_scene(roots, replace(Limits(), fbx_array_bytes=191))
        self.assertEqual(len(read_scene(roots, replace(Limits(), fbx_array_bytes=192)).meshes), 2)

    def test_polygon_material_indices_follow_connection_order(self):
        roots = fixture()
        geo = roots[1].children[0]
        geo.child('PolygonVertexIndex').props[0] = [0, 1, -3, 2, 1, -1]
        roots[1].children.append(Node('Material', [6, 'other', ''], []))
        roots[2].children.insert(0, Node('C', ['OO', 6, 2], []))
        geo.children.append(Node('LayerElementMaterial', [0], [
            Node('MappingInformationType', ['ByPolygon'], []),
            Node('ReferenceInformationType', ['IndexToDirect'], []),
            Node('Materials', [[1, 0]], [])]))
        mesh = read_scene(roots).meshes[0]
        self.assertEqual(mesh.material_ids, (6, 5))
        self.assertEqual(tuple(mesh.material_indices), (1, 0))

    def test_duplicate_placement_or_material_slot_is_rejected(self):
        for source in (1, 5):
            roots = fixture()
            roots[2].children.append(Node('C', ['OO', source, 2], []))
            with self.subTest(source=source), self.assertRaises(ReadError):
                read_scene(roots)

    def test_coordinate_overflow_is_rejected(self):
        roots = fixture()
        roots[1].children[1].child('Properties70').children[0].props[4:] = [1e308, 0, 0]
        roots[0].child('Properties70').children[0].props[-1] = 1e308
        with self.assertRaises(ReadError):
            read_scene(roots)
