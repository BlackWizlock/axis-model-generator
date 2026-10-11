from array import array
import unittest
from unittest import mock
from dataclasses import replace

from fixtures.builders import scene_bytes
from model_generator.fbx_binary import Node, parse_fbx
from model_generator.limits import Limits, ReadError
from model_generator.fbx_scene import read_scene
from functools import partial

# The tests of this file read the texture coordinates with the scene.
plain_scene, read_scene = read_scene, partial(read_scene, unwrapping=True)


def placed(roots):
    return [round(v, 9) + 0.0 for v in read_scene(roots).meshes[0].positions]


def model_properties(roots):
    return roots[1].children[1].child('Properties70').children


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

    def test_template_transform_is_a_default_and_is_applied(self):
        roots = fixture()
        roots.append(Node('Definitions', [], [Node('ObjectType', ['Model'], [
            Node('PropertyTemplate', ['FbxNode'], [Node('Properties70', [], [
                property_node('Lcl Scaling', [2, 2, 2])])])])]))
        self.assertEqual(tuple(read_scene(roots).meshes[0].positions), (10, 0, 0, 12, 0, 0, 10, 2, 0))

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


class TransformTests(unittest.TestCase):
    def test_rotation_about_z_turns_the_mesh_before_translation(self):
        roots = fixture()
        model_properties(roots).append(property_node('Lcl Rotation', [0, 0, 90]))
        self.assertEqual(placed(roots), [10, 0, 0, 10, 1, 0, 9, 0, 0])

    def test_scaling_precedes_rotation(self):
        roots = fixture()
        model_properties(roots).extend([property_node('Lcl Rotation', [0, 0, 90]),
                                        property_node('Lcl Scaling', [2, 3, 1])])
        self.assertEqual(placed(roots), [10, 0, 0, 10, 2, 0, 7, 0, 0])

    def test_euler_axes_apply_in_xyz_order_by_default(self):
        roots = fixture()
        model_properties(roots).append(property_node('Lcl Rotation', [90, 0, 90]))
        # X first lifts the Y edge to Z; Z then turns the X edge to Y.
        self.assertEqual(placed(roots), [10, 0, 0, 10, 1, 0, 10, 0, 1])

    def test_rotation_order_applies_only_while_rotation_is_active(self):
        roots = fixture()
        model_properties(roots).extend([property_node('Lcl Rotation', [90, 0, 90]),
                                        property_node('RotationOrder', [5])])
        self.assertEqual(placed(roots), [10, 0, 0, 10, 1, 0, 10, 0, 1])
        model_properties(roots).append(property_node('RotationActive', [1]))
        # ZYX: Z first turns X to Y and Y to -X; X then lifts the turned X edge to Z.
        self.assertEqual(placed(roots), [10, 0, 0, 10, 0, 1, 9, 0, 0])

    def test_pre_and_post_rotation_wrap_the_local_rotation(self):
        roots = fixture()
        model_properties(roots).extend([property_node('RotationActive', [1]),
                                        property_node('PreRotation', [0, 0, 90]),
                                        property_node('PostRotation', [0, 0, 90])])
        # The post rotation is inverted, so equal pre and post rotations cancel.
        self.assertEqual(placed(roots), [10, 0, 0, 11, 0, 0, 10, 1, 0])
        roots = fixture()
        model_properties(roots).append(property_node('PreRotation', [0, 0, 90]))
        self.assertEqual(placed(roots), [10, 0, 0, 11, 0, 0, 10, 1, 0])

    def test_pivots_and_offsets_move_the_centre_of_rotation_and_scaling(self):
        roots = fixture()
        model_properties(roots).extend([property_node('Lcl Rotation', [0, 0, 90]),
                                        property_node('RotationPivot', [1, 0, 0])])
        self.assertEqual(placed(roots), [11, -1, 0, 11, 0, 0, 10, -1, 0])
        roots = fixture()
        model_properties(roots).extend([property_node('Lcl Scaling', [2, 2, 2]),
                                        property_node('ScalingPivot', [1, 0, 0]),
                                        property_node('RotationOffset', [0, 0, 5]),
                                        property_node('ScalingOffset', [0, 3, 0])])
        self.assertEqual(placed(roots), [9, 3, 5, 11, 3, 5, 9, 5, 5])

    def test_geometric_transform_moves_geometry_but_not_children(self):
        roots = fixture()
        model_properties(roots).extend([property_node('GeometricTranslation', [0, 0, 4]),
                                        property_node('GeometricScaling', [2, 1, 1])])
        roots[1].children.append(Node('Model', [6, 'child', 'Mesh'], []))
        roots[2].children.extend([Node('C', ['OO', 1, 6], []), Node('C', ['OO', 6, 2], [])])
        scene = read_scene(roots)
        self.assertEqual(tuple(scene.meshes[0].positions), (10, 0, 4, 12, 0, 4, 10, 1, 4))
        self.assertEqual(tuple(scene.meshes[1].positions), (10, 0, 0, 11, 0, 0, 10, 1, 0))

    def test_parent_chain_is_composed_through_empty_nodes(self):
        roots = fixture()
        roots[1].children.extend([
            Node('Model', [6, 'parent', 'Null'], [Node('Properties70', [], [
                property_node('Lcl Translation', [0, 0, 100]), property_node('Lcl Rotation', [0, 0, 90]),
                property_node('InheritType', [1])])]),
            Node('Model', [7, 'root', 'Null'], [Node('Properties70', [], [
                property_node('Lcl Scaling', [2, 2, 2])])]),
            Node('NodeAttribute', [8, 'parent', 'Null'], [])])
        model_properties(roots).append(property_node('InheritType', [1]))
        roots[2].children.extend([Node('C', ['OO', 2, 6], []), Node('C', ['OO', 6, 7], []),
                                  Node('C', ['OO', 7, 0], []), Node('C', ['OO', 8, 6], [])])
        self.assertEqual(placed(roots), [0, 20, 200, 0, 22, 200, -2, 20, 200])

    def test_scale_inheritance_other_than_the_matrix_product_is_refused_under_a_scaled_ancestor(self):
        for inherit, scale, accepted in ((0, [1, 1, 1], True), (0, [2, 2, 2], False),
                                         (2, [2, 2, 2], False), (1, [2, 2, 2], True), (3, [1, 1, 1], False)):
            roots = fixture()
            roots[1].children.append(Node('Model', [6, 'parent', 'Null'], [Node('Properties70', [], [
                property_node('Lcl Scaling', scale)])]))
            model_properties(roots).append(property_node('InheritType', [inherit]))
            roots[2].children.append(Node('C', ['OO', 2, 6], []))
            with self.subTest(inherit=inherit, scale=scale):
                if accepted:
                    read_scene(roots)
                else:
                    with self.assertRaises(ReadError):
                        read_scene(roots)

    def test_cyclic_or_ambiguous_hierarchy_and_skeleton_nodes_are_refused(self):
        roots = fixture()
        roots[1].children.append(Node('Model', [6, 'parent', 'Null'], []))
        roots[2].children.extend([Node('C', ['OO', 2, 6], []), Node('C', ['OO', 6, 2], [])])
        with self.assertRaises(ReadError):
            read_scene(roots)
        roots = fixture()
        roots[1].children.extend([Node('Model', [6, 'a', 'Null'], []), Node('Model', [7, 'b', 'Null'], [])])
        roots[2].children.extend([Node('C', ['OO', 2, 6], []), Node('C', ['OO', 2, 7], [])])
        with self.assertRaises(ReadError):
            read_scene(roots)
        roots = fixture()
        roots[1].children.append(Node('Model', [6, 'bone', 'LimbNode'], []))
        with self.assertRaises(ReadError):
            read_scene(roots)
        roots = fixture()
        roots[1].children.append(Node('Model', [6, 'empty', 'Null'], []))
        roots[2].children.append(Node('C', ['OO', 1, 6], []))
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_y_up_scene_is_converted_to_z_up(self):
        roots = fixture()
        settings = {p.props[0]: p for p in roots[0].child('Properties70').children}
        for key, value in (('UpAxis', 1), ('FrontAxis', 2), ('FrontAxisSign', 1)):
            settings[key].props[4] = value
        model_properties(roots)[0].props[4:] = [10, 7, 3]
        # Source Y is height; source Z points to the viewer and becomes negative Y.
        self.assertEqual(placed(roots), [10, -3, 7, 11, -3, 7, 10, -3, 8])
        self.assertFalse(read_scene(roots).meshes[0].mirrored)

    def test_left_handed_or_malformed_axes_are_refused(self):
        for key, value in (('FrontAxisSign', 1), ('UpAxis', 0), ('CoordAxisSign', 2), ('UpAxis', 1.0)):
            roots = fixture()
            next(p for p in roots[0].child('Properties70').children if p.props[0] == key).props[4] = value
            with self.subTest(key=key, value=value), self.assertRaises(ReadError):
                read_scene(roots)

    def test_reflection_reverses_winding_and_singular_transform_is_refused(self):
        roots = fixture()
        model_properties(roots).append(property_node('Lcl Scaling', [-1, 1, 1]))
        mesh = read_scene(roots).meshes[0]
        self.assertTrue(mesh.mirrored)
        self.assertEqual(tuple(mesh.triangles), (0, 2, 1))
        self.assertEqual(tuple(mesh.positions), (10, 0, 0, 9, 0, 0, 10, 1, 0))
        roots = fixture()
        model_properties(roots).append(property_node('Lcl Scaling', [0, 1, 1]))
        with self.assertRaises(ReadError):
            read_scene(roots)

    def test_malformed_transform_values_are_refused(self):
        for key, value in (('Lcl Rotation', [0, 0]), ('Lcl Scaling', [1, float('nan'), 1]),
                           ('RotationOrder', [6]), ('RotationOrder', [1.5]), ('RotationActive', [2])):
            roots = fixture()
            model_properties(roots).extend([property_node('RotationActive', [1])] if key == 'RotationOrder' else [])
            model_properties(roots).append(property_node(key, value))
            with self.subTest(key=key, value=value), self.assertRaises(ReadError):
                read_scene(roots)


class PolygonTests(unittest.TestCase):
    def quad(self, indices=(0, 1, 2, -4, 0, 3, -5), materials=None):
        roots = fixture()
        geo = roots[1].children[0]
        geo.child('Vertices').props[0] = [0, 0, 0, 2, 0, 0, 2, 2, 0, 0, 2, 0, 1, 3, 0]
        geo.child('PolygonVertexIndex').props[0] = list(indices)
        if materials:
            roots[1].children.append(Node('Material', [6, 'other', ''], []))
            roots[2].children.append(Node('C', ['OO', 6, 2], []))
            geo.children.append(Node('LayerElementMaterial', [0], [
                Node('MappingInformationType', ['ByPolygon'], []),
                Node('ReferenceInformationType', ['IndexToDirect'], []),
                Node('Materials', [list(materials)], [])]))
        return roots

    def test_convex_polygon_becomes_a_fan_and_materials_follow_each_triangle(self):
        mesh = read_scene(self.quad(materials=(1, 0))).meshes[0]
        self.assertEqual(tuple(mesh.triangles), (0, 1, 2, 0, 2, 3, 0, 3, 4))
        self.assertEqual(tuple(mesh.material_indices), (1, 1, 0))

    def unwrapped(self, mapping='ByPolygonVertex', reference='IndexToDirect', pairs=7, index=(6, 5, 4, 3, 2, 1, 0)):
        roots = self.quad()
        layer = [Node('MappingInformationType', [mapping], []), Node('ReferenceInformationType', [reference], []),
                 Node('UV', [array('d', (c for k in range(pairs) for c in (k / 10, k / 100)))], [])]
        if index is not None:
            layer.append(Node('UVIndex', [array('i', index)], []))
        roots[1].children[0].children.append(Node('LayerElementUV', [0], layer))
        return roots

    def pairs(self, mesh):
        return [round(u * 10) for u in mesh.uvs[::2]]

    def test_texture_coordinates_follow_every_corner_of_the_fan(self):
        mesh = read_scene(self.unwrapped()).meshes[0]
        self.assertEqual(tuple(mesh.triangles), (0, 1, 2, 0, 2, 3, 0, 3, 4))
        # The quad holds places 0..3 of the index list, the triangle places 4..6.
        self.assertEqual(self.pairs(mesh), [6, 5, 4, 6, 4, 3, 2, 1, 0])
        self.assertEqual(list(mesh.uvs[1::2]), [u / 10 for u in mesh.uvs[::2]])
        direct = read_scene(self.unwrapped(reference='Direct', index=None)).meshes[0]
        self.assertEqual(self.pairs(direct), [0, 1, 2, 0, 2, 3, 4, 5, 6])
        vertex = read_scene(self.unwrapped('ByVertice', 'Direct', 5, None)).meshes[0]
        self.assertEqual(self.pairs(vertex), list(vertex.triangles))
        named = read_scene(self.unwrapped('ByVertice', index=(4, 3, 2, 1, 0))).meshes[0]
        self.assertEqual(self.pairs(named), [4 - i for i in named.triangles])

    def test_texture_coordinates_turn_with_a_reflected_mesh(self):
        roots = self.unwrapped()
        model_properties(roots).append(property_node('Lcl Scaling', [-1, 1, 1]))
        mesh = read_scene(roots).meshes[0]
        self.assertEqual(tuple(mesh.triangles), (0, 2, 1, 0, 3, 2, 0, 4, 3))
        self.assertEqual(self.pairs(mesh), [6, 4, 5, 6, 3, 4, 2, 0, 1])

    def test_texture_coordinates_that_cannot_be_placed_are_left_out(self):
        self.assertEqual(len(read_scene(self.quad()).meshes[0].uvs), 0)
        cases = [dict(mapping='ByPolygon'), dict(reference='Index'), dict(index=(6, 5, 4, 3, 2, 1, 7)),
                 dict(index=(6, 5, 4, 3, 2, 1, -1)), dict(index=(6, 5, 4)), dict(reference='Direct', pairs=6, index=None),
                 dict(mapping='ByVertice', reference='Direct', pairs=4, index=None)]
        for case in cases:
            with self.subTest(case=case):
                mesh = read_scene(self.unwrapped(**case)).meshes[0]
                self.assertEqual((len(mesh.uvs), len(mesh.triangles)), (0, 9))
        roots = self.unwrapped()
        roots[1].children[0].children[-1].child('UV').props[0][3] = float('inf')
        self.assertEqual(len(read_scene(roots).meshes[0].uvs), 0)

    def test_texture_coordinates_cost_the_corners_not_the_length_of_the_layer(self):
        # A pair no corner uses is never looked at, however long the layer is.
        roots = self.unwrapped(pairs=8)
        roots[1].children[0].children[-1].child('UV').props[0][15] = float('nan')
        self.assertEqual(self.pairs(read_scene(roots).meshes[0]), [6, 5, 4, 6, 4, 3, 2, 1, 0])

    def test_reading_a_scene_does_not_look_at_the_texture_layers(self):
        from model_generator import fbx_scene
        with mock.patch.object(fbx_scene, '_uv_layer', wraps=fbx_scene._uv_layer) as found:
            scene = plain_scene(self.unwrapped())
        self.assertEqual((found.call_count, len(scene.meshes[0].uvs)), (0, 0))
        self.assertEqual(self.pairs(fbx_scene.unwrap(scene).meshes[0]), [6, 5, 4, 6, 4, 3, 2, 1, 0])

    def test_texture_layer_is_found_once_for_all_placements_of_a_geometry(self):
        from model_generator import fbx_scene
        roots = self.unwrapped()
        objects = next(n for n in roots if n.name == 'Objects')
        links = next(n for n in roots if n.name == 'Connections')
        geometry = next(n for n in objects.children if n.name == 'Geometry')
        model = next(n for n in objects.children if n.name == 'Model')
        for extra in range(3):
            objects.children.append(Node('Model', [9000 + extra, *model.props[1:]], model.children))
            links.children += [Node('C', ['OO', geometry.props[0], 9000 + extra], []), Node('C', ['OO', 9000 + extra, 0], [])]
        with mock.patch.object(fbx_scene, '_uv_layer', wraps=fbx_scene._uv_layer) as found:
            meshes = read_scene(roots).meshes
        self.assertEqual((len(meshes), found.call_count, {len(m.uvs) for m in meshes}), (4, 1, {18}))

    def test_texture_coordinates_past_the_array_budget_are_left_out_not_refused(self):
        from dataclasses import replace
        from model_generator.limits import Limits
        geometry = read_scene(self.unwrapped()).meshes[0]
        room = (15 + 7) * 8
        kept = read_scene(self.unwrapped(), replace(Limits(), fbx_array_bytes=room + len(geometry.uvs) * 8)).meshes[0]
        self.assertEqual(len(kept.uvs), 18)
        bare = read_scene(self.unwrapped(), replace(Limits(), fbx_array_bytes=room + len(geometry.uvs) * 8 - 1)).meshes[0]
        self.assertEqual((len(bare.uvs), tuple(bare.triangles)), (0, tuple(geometry.triangles)))

    def test_concave_degenerate_and_short_polygons_are_refused(self):
        for indices in ((0, 1, 4, 2, -4), (0, 1, 0, -2), (0, -2)):
            with self.subTest(indices=indices), self.assertRaises(ReadError):
                read_scene(self.quad(indices))

    def test_fan_keeps_the_polygon_area(self):
        mesh = read_scene(self.quad((0, 1, 2, 4, -4,))).meshes[0]
        area = 0
        for i in range(0, len(mesh.triangles), 3):
            a, b, c = (mesh.positions[k * 3:k * 3 + 2] for k in mesh.triangles[i:i + 3])
            area += ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2
        self.assertAlmostEqual(area, 5)
