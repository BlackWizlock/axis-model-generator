import io
import struct
import unittest
import zipfile
import zlib

from fixtures.builders import zip_bytes
from model_generator.archive import read_archive
from model_generator.limits import Limits


class ArchiveTests(unittest.TestCase):
    def test_unicode_path_extra_is_not_a_null_byte_attack(self):
        info = zipfile.ZipInfo("a")
        extra = b"\x01" + struct.pack("<I", zlib.crc32(b"a")) + "Модель_пример.fbx".encode()
        info.extra = struct.pack("<HH", 0x7075, len(extra)) + extra
        result = read_archive(zip_bytes([(info, b"abc")]))
        self.assertFalse(result.findings)
        self.assertEqual(result.entries[0].name, "Модель_пример.fbx")

    def test_nested_wrapper_has_logical_names(self):
        data = zip_bytes([("Объект/", b""), ("Объект/model.zip", zip_bytes([("a.fbx", b"abc")]))])
        result = read_archive(data)
        self.assertEqual([(e.name, e.data) for e in result.entries], [("Объект/model.zip/a.fbx", b"abc")])
        self.assertFalse(result.findings)

    def test_unsafe_paths_and_symlink_rejected(self):
        for name in ["../a", "a/../../b", r"a\..\b", "/a", r"C:\a", "a:b", "./a", "a//b"]:
            with self.subTest(name=name):
                result = read_archive(zip_bytes([(name, b"abc")]))
                self.assertFalse(result.entries)
                self.assertEqual(result.findings[0].rule_id, "zip.path")
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        self.assertEqual(read_archive(zip_bytes([(info, b"target")])).findings[0].rule_id, "zip.symlink")

    def test_duplicate_and_ambiguous_wrapper(self):
        for entries in [[("A", b"a"), ("a", b"b")], [("é", b"a"), ("e\u0301", b"b")],
                        [("a.zip", zip_bytes([])), ("b.zip", zip_bytes([]))],
                        [("a.zip", zip_bytes([])), ("other", b"x")]]:
            self.assertTrue(read_archive(zip_bytes(entries)).findings)
        nested = zip_bytes([("a.zip", zip_bytes([("b.zip", zip_bytes([]))]))])
        self.assertEqual(read_archive(nested).findings[-1].rule_id, "zip.depth")

    def test_budgets_enforced_at_boundary(self):
        data = zip_bytes([("a", b"abc")])
        self.assertEqual(len(read_archive(data, Limits(member_bytes=3, expanded_bytes=3, entries=1)).entries), 1)
        for limits in [Limits(input_bytes=len(data)-1), Limits(member_bytes=2),
                       Limits(expanded_bytes=2), Limits(entries=0)]:
            self.assertTrue(read_archive(data, limits).findings)
        self.assertFalse(read_archive(data, Limits(input_bytes=len(data))).findings)
        inner = zip_bytes([("a", b"abc")])
        outer = zip_bytes([("a.zip", inner)])
        self.assertTrue(read_archive(outer, Limits(expanded_bytes=len(inner)+2)).findings)

    def test_encryption_and_crc_preserve_prior_result(self):
        data = bytearray(zip_bytes([("a", b"abc")], zipfile.ZIP_STORED))
        local = data.index(b"PK\x03\x04")
        central = data.index(b"PK\x01\x02")
        struct.pack_into("<H", data, local+6, 1)
        struct.pack_into("<H", data, central+8, 1)
        self.assertEqual(read_archive(bytes(data)).findings[0].rule_id, "zip.encrypted")
        corrupt = bytearray(zip_bytes([("a", b"abc"), ("b", b"def")], zipfile.ZIP_STORED))
        with zipfile.ZipFile(io.BytesIO(corrupt)) as archive:
            info = archive.getinfo("b")
            offset = info.header_offset + 30 + len(info.filename)
        corrupt[offset] ^= 1
        result = read_archive(bytes(corrupt))
        self.assertEqual([e.name for e in result.entries], ["a"])
        self.assertEqual(result.findings[-1].rule_id, "zip.integrity")

    def test_invalid_archive_is_addressed(self):
        self.assertEqual(read_archive(b"no zip").findings[0].rule_id, "zip.integrity")

    def test_metadata_errors_identify_offending_member_in_wrapper(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        cases = [([("../bad", b"x")], "../bad", "zip.path"),
                 ([("A", b"x"), ("a", b"y")], "a", "zip.duplicate"),
                 ([(link, b"target")], "link", "zip.symlink")]
        for entries, name, rule in cases:
            with self.subTest(rule=rule):
                result = read_archive(zip_bytes([("outer.zip", zip_bytes(entries))]))
                self.assertEqual(result.findings[0].file, "outer.zip/" + name)
                self.assertEqual(result.findings[0].rule_id, rule)
                self.assertFalse(result.entries)
