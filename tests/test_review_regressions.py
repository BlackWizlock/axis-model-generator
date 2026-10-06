"""Input failures reproduced during the independent final review."""

import io
import unittest
import zipfile
import struct
import zlib

from fixtures.builders import fbx_bytes, prop, scene_bytes, zip_bytes
from model_generator.archive import read_archive
from model_generator.fbx_binary import parse_fbx
from model_generator.fbx_inspection import inspect_fbx
from model_generator.validator import validate_bytes
from model_generator.limits import Limits


class ReviewRegressions(unittest.TestCase):
    def test_crc_errors_still_consume_expansion_budget(self):
        data = bytearray(zip_bytes([("a", b"abcd"), ("b", b"abcd"), ("c", b"abcd")], zipfile.ZIP_STORED))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                data[info.header_offset + 30 + len(info.filename)] ^= 1
        result = read_archive(bytes(data), Limits(member_bytes=4, expanded_bytes=5))
        self.assertEqual([f.rule_id for f in result.findings], ["zip.integrity", "zip.budget", "zip.budget"])

    def test_false_size_cannot_hide_remaining_compressed_data(self):
        for compression in [zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]:
            with self.subTest(compression=compression):
                data = bytearray(zip_bytes([("a", b"a" * 100000)], compression))
                central = data.index(b"PK\x01\x02")
                crc = zlib.crc32(b"a")
                struct.pack_into("<I", data, 14, crc)
                struct.pack_into("<I", data, 22, 1)
                struct.pack_into("<I", data, central+16, crc)
                struct.pack_into("<I", data, central+24, 1)
                result = read_archive(bytes(data), Limits(member_bytes=1, expanded_bytes=1))
                self.assertFalse(result.entries)
                self.assertTrue(result.findings)

    def test_empty_objects_do_not_pass_geometry(self):
        empty = fbx_bytes([("Objects", [], [])])
        report = validate_bytes(zip_bytes([("a.fbx", empty), ("a_Ground.fbx", empty)]))
        self.assertTrue(report.has_failures())
        self.assertTrue(all(f.status == "not_checked" for f in report.findings if f.rule_id == "profile.triangles"))

    def test_unknown_geometry_type_is_addressed(self):
        data = fbx_bytes([("Objects", [], [("Geometry", [prop("L", 1), prop("S", "Unknown"), prop("S", "FutureMesh")], [])])])
        _, findings = inspect_fbx(parse_fbx(data), "a.fbx")
        self.assertTrue(any(f.rule_id == "fbx.geometry_type" and f.status == "fail" for f in findings))

    def test_invalid_connection_type_cannot_supply_texture(self):
        roots = parse_fbx(scene_bytes())
        roots[2].children[1].props[0] = "INVALID"
        _, findings = inspect_fbx(roots, "a.fbx")
        self.assertTrue(any(f.rule_id == "fbx.connection" and f.status == "fail" for f in findings))
        self.assertTrue(any(f.rule_id == "fbx.resource" and f.status == "fail" for f in findings))

    def test_corrupt_bzip2_and_lzma_preserve_partial_report(self):
        for compression in [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA]:
            with self.subTest(compression=compression):
                data = bytearray(zip_bytes([("a", b"good"), ("b", b"corrupt me" * 10)], compression))
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    info = archive.getinfo("b")
                    offset = info.header_offset + 30 + len(info.filename.encode())
                data[offset:offset+info.compress_size] = bytes(info.compress_size)
                result = read_archive(bytes(data))
                self.assertEqual([entry.name for entry in result.entries], ["a"])
                self.assertEqual(result.findings[-1].file, "b")
                self.assertEqual(result.findings[-1].rule_id, "zip.integrity")
