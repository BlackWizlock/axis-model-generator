import unittest
from fixtures.builders import scene_bytes
from model_generator.fbx_binary import Node, parse_fbx
from model_generator.fbx_inspection import inspect_fbx


class InspectionTests(unittest.TestCase):
    def test_geometry_units_transforms_and_embedded_resource(self):
        result, findings = inspect_fbx(parse_fbx(scene_bytes()), "a.fbx")
        self.assertEqual(result["triangles"], 1)
        self.assertEqual(result["polygons"], 1)
        self.assertEqual(result["unit_meters"], 1)
        self.assertEqual(result["counts"]["Model"], 1)
        self.assertEqual(len(result["images"]), 1)
        self.assertIn("fbx.transform", [f.rule_id for f in findings])
        self.assertNotIn("fbx.resource", [f.rule_id for f in findings if f.status == "fail"])

    def test_invalid_faces_and_nonfinite_vertices(self):
        for indices, vertices in [((0, 1, 2), (0, 0, 0)*3), ((0, 1, -5), (0, 0, 0)*3),
                                  ((0, 1, 2, -4), (0, 0, 0)*4), ((0, 1, -3), (float("nan"), 0, 0)*3),
                                  ((0, 1, -3), (0, 0))]:
            result, findings = inspect_fbx(parse_fbx(scene_bytes(indices, vertices)), "a.fbx")
            self.assertTrue(any(f.status == "fail" for f in findings))
        result, findings = inspect_fbx(parse_fbx(scene_bytes((0, 1, 2, -4), (0, 0, 0)*4)), "a.fbx")
        self.assertEqual(result["triangles"], 0)
        self.assertEqual(result["polygons"], 1)

    def test_duplicate_id_missing_link_and_hierarchy(self):
        tree = parse_fbx(scene_bytes())
        objects = tree[1]
        objects.children.append(Node("Model", [2, "duplicate", "Mesh"], []))
        tree[2].children.extend([Node("C", ["OO", 999, 2], []), Node("C", ["OO", 2, 2], [])])
        _, findings = inspect_fbx(tree, "a.fbx")
        rules = {f.rule_id for f in findings}
        self.assertTrue({"fbx.object_id", "fbx.connection", "fbx.hierarchy"} <= rules)

    def test_missing_units_are_not_assumed(self):
        tree = parse_fbx(scene_bytes())[1:]
        result, findings = inspect_fbx(tree, "a.fbx")
        self.assertIsNone(result["unit_meters"])
        self.assertIn("fbx.units", [f.rule_id for f in findings])
