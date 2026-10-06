import struct
import unittest

from fixtures.builders import fbx_bytes, prop
from model_generator.fbx_binary import parse_fbx
from model_generator.limits import Limits, ReadError


class BinaryTests(unittest.TestCase):
    def test_property_types_and_nested_nodes(self):
        props = [prop("Y", -2), prop("C", True), prop("B", -1), prop("I", 123),
                 prop("F", 1.5), prop("D", 2.5), prop("L", 10000000000), prop("S", "Привет"), prop("R", b"abc")]
        for kind in "fdilbc":
            props.extend([prop(kind, [0, 1]), prop(kind, [0, 1], True)])
        tree = parse_fbx(fbx_bytes([("Root", props, [("Child", [], [])])]))
        self.assertEqual(tree[0].name, "Root")
        self.assertEqual(tree[0].props[:9], [-2, True, -1, 123, 1.5, 2.5, 10000000000, "Привет", b"abc"])
        self.assertEqual(list(tree[0].props[-1]), [0, 1])
        self.assertEqual(tree[0].children[0].name, "Child")
        self.assertEqual(parse_fbx(fbx_bytes([])), [])

    def test_unsupported_and_truncation(self):
        for data, rule in [(fbx_bytes([], 7500), "fbx.unsupported"),
                           (b"; FBX 7.4", "fbx.unsupported"),
                           (fbx_bytes([])[:25], "fbx.integrity")]:
            with self.subTest(rule=rule), self.assertRaises(ReadError) as caught:
                parse_fbx(data)
            self.assertEqual(caught.exception.rule, rule)

    def test_bad_offsets_and_properties(self):
        good = fbx_bytes([("A", [prop("I", 1)], [])])
        cases = []
        for offset, value in [(27, 10), (27, len(good)+1), (35, 999), (31, 2)]:
            bad = bytearray(good)
            struct.pack_into("<I", bad, offset, value)
            cases.append(bytes(bad))
        cases += [fbx_bytes([("A", [b"X"], [])]), good[:-14]]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ReadError):
                parse_fbx(data)

    def test_arrays_length_encoding_zlib_and_budgets(self):
        payloads = [b"i" + struct.pack("<III", 3, 0, 4) + bytes(4),
                    b"i" + struct.pack("<III", 1, 2, 4) + bytes(4),
                    b"i" + struct.pack("<III", 1, 1, 3) + b"bad",
                    prop("i", [0]*100, True)[:-1]]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ReadError):
                parse_fbx(fbx_bytes([("A", [payload], [])]))
        good = fbx_bytes([("A", [prop("i", [1, 2], True), prop("i", [3, 4])], [])])
        self.assertEqual(len(parse_fbx(good, Limits(fbx_array_bytes=16))), 1)
        for limits in [Limits(fbx_array_bytes=15), Limits(fbx_nodes=0), Limits(fbx_properties=1)]:
            with self.assertRaises(ReadError):
                parse_fbx(good, limits)
        deep = fbx_bytes([("A", [], [("B", [], [])])])
        with self.assertRaises(ReadError):
            parse_fbx(deep, Limits(fbx_depth=1))
