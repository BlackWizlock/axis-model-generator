"""Container and metadata defenses for the portable wire contract."""

from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
import zlib

from fixtures.package_builders import make_package
from model_generator.package_manifest import PackageError, PackageLimits, _unique_paths, decode_package_json, parse_manifest
from model_generator.package_reader import read_package_bytes, read_package_path


def contents(blob):
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def rewrite(blob, *, updates=None, missing=(), info_updates=None, compression=zipfile.ZIP_DEFLATED, duplicate=None):
    members = contents(blob)
    members.update(updates or {})
    out = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(out, "w", compression=compression) as archive:
            for name, data in members.items():
                if name not in missing:
                    info = zipfile.ZipInfo(name)
                    info.compress_type = compression
                    if info_updates and name in info_updates:
                        for key, value in info_updates[name].items():
                            setattr(info, key, value)
                    archive.writestr(info, data)
            if duplicate:
                archive.writestr(duplicate, members[duplicate])
    return out.getvalue()


def manifest_of(blob):
    return json.loads(contents(blob)["manifest.json"])


def mutate_manifest(blob, mutate):
    manifest = manifest_of(blob)
    mutate(manifest)
    return rewrite(blob, updates={"manifest.json": json.dumps(manifest).encode()})


def patch_headers(blob, name, *, size=None, crc=None, flags=None, compressed_size=None):
    data = bytearray(blob)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        info = archive.getinfo(name)
    central = data.index(b"PK\x01\x02")
    while True:
        name_len, extra_len, comment_len = struct.unpack_from("<HHH", data, central + 28)
        candidate = bytes(data[central + 46:central + 46 + name_len]).decode("utf-8")
        if candidate == name:
            break
        central += 46 + name_len + extra_len + comment_len
    for value, local_off, central_off, fmt in [(size, 22, 24, "I"), (crc, 14, 16, "I"), (flags, 6, 8, "H"), (compressed_size, 18, 20, "I")]:
        if value is not None:
            struct.pack_into("<" + fmt, data, info.header_offset + local_off, value)
            struct.pack_into("<" + fmt, data, central + central_off, value)
    return bytes(data)


class PackageReaderTests(unittest.TestCase):
    def setUp(self):
        self.blob = make_package()

    def reject(self, blob, rule, *, file=None, limits=None):
        with self.assertRaises(PackageError) as caught:
            read_package_bytes(blob, limits)
        self.assertEqual(caught.exception.rule, rule)
        if file is not None:
            self.assertEqual(caught.exception.file, file)
        return caught.exception

    def test_round_trip_inventory_and_input_hash(self):
        package = read_package_bytes(self.blob)
        self.assertEqual(package.input_sha256, hashlib.sha256(self.blob).hexdigest())
        self.assertEqual(set(package.members), {"scene.json", "geometry/vertices.bin", "geometry/triangles.bin"})
        self.assertEqual(package.manifest.package_version, (1, 0))
        self.assertEqual(package.manifest.scene_path, "scene.json")
        for descriptor in package.manifest.files:
            self.assertEqual(descriptor.bytes, len(package.members[descriptor.path]))
            self.assertEqual(descriptor.sha256, hashlib.sha256(package.members[descriptor.path]).hexdigest())

    def test_path_reader_and_io_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "package.zip"
            path.write_bytes(self.blob)
            self.assertEqual(read_package_path(path).input_sha256, hashlib.sha256(self.blob).hexdigest())
            self.reject(self.blob, "package.budget", limits=PackageLimits(input_bytes=len(self.blob) - 1))
            with self.assertRaises(PackageError):
                read_package_path(path, PackageLimits(input_bytes=len(self.blob) - 1))
            with self.assertRaises(OSError):
                read_package_path(Path(folder) / "missing.zip")

    def test_missing_extra_and_duplicate_inventory(self):
        self.reject(rewrite(self.blob, missing=("scene.json",)), "package.inventory", file="scene.json")
        self.reject(rewrite(self.blob, updates={"evidence/extra.txt": b"extra"}), "package.inventory", file="evidence/extra.txt")
        self.reject(rewrite(self.blob, missing=("manifest.json",)), "package.inventory", file="manifest.json")
        self.reject(rewrite(self.blob, duplicate="scene.json"), "package.path", file="scene.json")
        self.reject(mutate_manifest(self.blob, lambda m: m["files"].append(m["files"][0])), "package.path", file="scene.json")
        self.reject(mutate_manifest(self.blob, lambda m: m["files"][0].update(path="manifest.json")), "package.inventory", file="manifest.json")

    def test_hash_crc_and_descriptor_size(self):
        self.reject(rewrite(self.blob, updates={"scene.json": b"bad"}), "package.size", file="scene.json")
        data = contents(self.blob)["scene.json"]
        self.reject(rewrite(self.blob, updates={"scene.json": b"x" * len(data)}), "package.hash", file="scene.json")
        self.reject(patch_headers(self.blob, "scene.json", crc=123), "package.integrity", file="scene.json")
        self.reject(mutate_manifest(self.blob, lambda m: m["files"][0].update(bytes=1)), "package.size", file="scene.json")

    def test_lying_size_cannot_hide_real_deflate_output(self):
        payload = b"a" * 50000
        # Also lie in the manifest; a ZipExtFile-only reader would accept this prefix.
        manifest = manifest_of(make_package(member_overrides={"evidence/data.txt": payload}))
        descriptor = manifest["files"][-1]
        descriptor.update(bytes=1, sha256=hashlib.sha256(b"a").hexdigest())
        source = make_package(manifest_updates=manifest, member_overrides={"evidence/data.txt": payload})
        source = patch_headers(source, "evidence/data.txt", size=1, crc=zlib.crc32(b"a"))
        self.reject(source, "package.integrity", file="evidence/data.txt")

    def test_incomplete_and_trailing_deflate(self):
        with zipfile.ZipFile(io.BytesIO(self.blob)) as archive:
            size = archive.getinfo("scene.json").compress_size
        self.reject(patch_headers(self.blob, "scene.json", compressed_size=size - 1), "package.integrity", file="scene.json")
        # Compressed size including one byte of the following local header.
        self.reject(patch_headers(self.blob, "scene.json", compressed_size=size + 1), "package.integrity", file="scene.json")

    def test_trailing_deflate_bytes_inside_member_extent(self):
        name = "geometry/triangles.bin"
        with zipfile.ZipFile(io.BytesIO(self.blob)) as archive:
            info = archive.getinfo(name)
        name_size, extra_size = struct.unpack_from("<HH", self.blob, info.header_offset + 26)
        end = info.header_offset + 30 + name_size + extra_size + info.compress_size
        blob = bytearray(self.blob[:end] + b"x" + self.blob[end:])
        end_record = blob.rindex(b"PK\x05\x06")
        central_offset = struct.unpack_from("<I", blob, end_record + 16)[0]
        struct.pack_into("<I", blob, end_record + 16, central_offset + 1)
        blob = patch_headers(bytes(blob), name, compressed_size=info.compress_size + 1)
        self.reject(blob, "package.integrity", file=name)

    def test_stored_size_lie_matches_manifest_and_prefix_crc(self):
        payload = b"abcd"
        manifest = manifest_of(make_package(member_overrides={"evidence/data.txt": payload}))
        manifest["files"][-1].update(bytes=1, sha256=hashlib.sha256(payload[:1]).hexdigest())
        blob = rewrite(make_package(manifest_updates=manifest, member_overrides={"evidence/data.txt": payload}), compression=zipfile.ZIP_STORED)
        blob = patch_headers(blob, "evidence/data.txt", size=1, crc=zlib.crc32(payload[:1]))
        self.reject(blob, "package.integrity", file="evidence/data.txt")

    def test_stored_lying_size(self):
        blob = rewrite(self.blob, compression=zipfile.ZIP_STORED)
        self.reject(patch_headers(blob, "scene.json", size=1, crc=0), "package.size", file="scene.json")

    def test_unsupported_metadata_before_geometry_open(self):
        original = zipfile.ZipFile.open
        for updates in ({"package_version": {"major": 2, "minor": 0}}, {"required_capabilities": ["scene-v1", "geometry-f64-u32-v1", "future"]}):
            blob = make_package(manifest_updates=updates)
            opened = []
            def spy(archive, name, *args, **kwargs):
                opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
                return original(archive, name, *args, **kwargs)
            with patch.object(zipfile.ZipFile, "open", spy):
                self.reject(blob, "package.unsupported", file="manifest.json")
            self.assertEqual(opened, ["manifest.json"])
        self.assertEqual(read_package_bytes(make_package(manifest_updates={"package_version": {"major": 1, "minor": 999}})).manifest.package_version, (1, 999))

    def test_wrappers_executables_and_nested_signatures(self):
        for path in ("evidence/nested.zip", "evidence/run.py", "evidence/run.EXE", "evidence/scene.blend"):
            with self.subTest(path=path):
                self.reject(make_package(member_overrides={path: b"ordinary"}), "package.content", file=path)
        self.reject(make_package(member_overrides={"evidence/nested.dat": self.blob}), "package.content", file="evidence/nested.dat")
        self.reject(rewrite(self.blob, missing=tuple(contents(self.blob)), updates={"wrapper.zip": self.blob}), "package.content", file="wrapper.zip")

    def test_unsafe_names_and_collisions(self):
        for path in ("../a", "/a", "a\\b", "a//b", "a/./b", "C:a", "evidence/CON.txt", "evidence/com9", "evidence/LPT1.x", "evidence/a.", "evidence/a ", "evidence/a?", "evidence/a\x01", "evidence/", "evidence/cafe\u0301.txt"):
            with self.subTest(path=path):
                self.reject(rewrite(self.blob, updates={path: b"x"}), "package.path", file=path)
        self.reject(rewrite(self.blob, updates={"SCENE.JSON": b"x"}), "package.path", file="SCENE.JSON")
        blob = make_package(member_overrides={"evidence/caf\u00e9.txt": b"x"})
        self.reject(rewrite(blob, updates={"evidence/cafe\u0301.txt": b"y"}), "package.path", file="evidence/cafe\u0301.txt")

    def test_unix_special_types_encryption_and_codec(self):
        for mode in (stat.S_IFLNK, stat.S_IFDIR, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
            with self.subTest(mode=mode):
                self.reject(rewrite(self.blob, info_updates={"scene.json": {"create_system": 3, "external_attr": (mode | 0o600) << 16}}), "package.type", file="scene.json")
        self.reject(patch_headers(self.blob, "scene.json", flags=1), "package.encrypted", file="scene.json")
        self.reject(rewrite(self.blob, compression=zipfile.ZIP_BZIP2), "package.unsupported", file="manifest.json")
        self.assertIsNotNone(read_package_bytes(rewrite(self.blob, compression=zipfile.ZIP_STORED)))

    def test_container_budgets(self):
        sizes = [len(value) for value in contents(self.blob).values()]
        for limits in (PackageLimits(entries=3), PackageLimits(member_bytes=1), PackageLimits(expanded_bytes=sum(sizes)-1), PackageLimits(manifest_bytes=1), PackageLimits(scene_bytes=1), PackageLimits(compression_ratio=1)):
            with self.subTest(limits=limits):
                self.reject(self.blob, "package.budget", limits=limits)
        self.assertIsNotNone(read_package_bytes(self.blob, PackageLimits(expanded_bytes=sum(sizes), member_bytes=max(sizes))))

    def test_array_contract_and_scalar_budgets(self):
        for update in ({"count": True}, {"count": -1}, {"dtype": "float32"}, {"byte_order": "big"}, {"components": 2}, {"stride_bytes": 12}):
            with self.subTest(update=update):
                self.reject(mutate_manifest(self.blob, lambda m: m["files"][1]["array"].update(update)), "package.schema", file="geometry/vertices.bin")
        self.reject(self.blob, "package.budget", limits=PackageLimits(array_items=8))
        self.reject(self.blob, "package.budget", limits=PackageLimits(array_items=11))
        self.assertIsNotNone(read_package_bytes(self.blob, PackageLimits(array_items=12)))

    def test_empty_zip_reports_missing_manifest(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w"):
            pass
        self.reject(output.getvalue(), "package.inventory", file="manifest.json")

    def test_path_reader_requests_a_bounded_read(self):
        class RecordingStream(io.BytesIO):
            requested = None
            def read(self, size=-1):
                self.requested = size
                return super().read(size)
        stream = RecordingStream(self.blob)
        with patch.object(Path, "open", return_value=stream):
            package = read_package_path(Path("synthetic.zip"), PackageLimits(input_bytes=len(self.blob)))
        self.assertEqual(stream.requested, len(self.blob) + 1)
        self.assertTrue(stream.closed)
        self.assertEqual(package.input_sha256, hashlib.sha256(self.blob).hexdigest())

    def test_file_and_derived_directory_cannot_collide(self):
        blob = make_package(member_overrides={"evidence/thing": b"a", "evidence/THING/file.txt": b"b"})
        self.reject(blob, "package.path", file="evidence/THING/file.txt")

    def test_false_end_record_count_rejected_before_directory_allocation(self):
        blob = bytearray(self.blob)
        end = blob.rindex(b"PK\x05\x06")
        struct.pack_into("<HH", blob, end + 8, 0, 0)
        with patch.object(zipfile.ZipFile, "__init__", side_effect=AssertionError("must reject before directory allocation")):
            self.reject(bytes(blob), "package.integrity")

    def test_hidden_global_zip64_rejected_before_zipinfo_allocation(self):
        end_offset = self.blob.rfind(b"PK\x05\x06")
        size, offset = struct.unpack_from("<II", self.blob, end_offset + 12)
        central = self.blob[offset:offset + size]
        name, extra, comment = struct.unpack_from("<HHH", central, 28)
        stub = bytearray(central[:46 + name + extra + comment])
        struct.pack_into("<H", stub, 32, comment + 20)
        record_offset = offset + size
        end64 = struct.pack("<4sQHHIIQQQQ", b"PK\x06\x06", 44 + len(stub), 45, 45, 0, 0, 4, 4, size, offset)
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, record_offset, 1)
        end = struct.pack("<4sHHHHIIH", b"PK\x05\x06", 0, 0, 1, 1, len(stub) + 20, record_offset + 56, 0)
        crafted = self.blob[:end_offset] + end64 + stub + locator + end
        allocated = []
        original = zipfile.ZipInfo.__init__
        def spy(info, *args, **kwargs):
            allocated.append(args[0])
            return original(info, *args, **kwargs)
        with patch.object(zipfile.ZipInfo, "__init__", spy):
            with self.assertRaises(PackageError) as caught:
                read_package_bytes(crafted, PackageLimits(entries=1))
        self.assertEqual(allocated, [])
        self.assertEqual(caught.exception.rule, "package.unsupported")

    def test_global_zip64_sentinels_are_unsupported(self):
        blob = bytearray(self.blob)
        end = blob.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", blob, end + 8, 65535, 65535)
        self.reject(bytes(blob), "package.unsupported")

    def test_local_forced_zip64_header_with_ordinary_directory_is_accepted(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, data in contents(self.blob).items():
                with archive.open(name, "w", force_zip64=True) as stream:
                    stream.write(data)
        self.assertEqual(read_package_bytes(output.getvalue()).members, {name: data for name, data in contents(self.blob).items() if name != "manifest.json"})

    def test_deep_path_components_budget_before_manifest_read(self):
        path = "evidence/" + "a/" * 300 + "leaf.txt"
        blob = make_package(member_overrides={path: b"x"})
        original = zipfile.ZipFile.open
        opened = []
        def spy(archive, name, *args, **kwargs):
            opened.append(name)
            return original(archive, name, *args, **kwargs)
        with patch.object(zipfile.ZipFile, "open", spy):
            self.reject(blob, "package.budget", file=path, limits=PackageLimits(json_nodes=256))
        self.assertEqual(opened, [])

    def test_directory_spellings_cannot_alias_across_files(self):
        path = "evidence/foo/b.txt"
        blob = make_package(member_overrides={"evidence/Foo/a.txt": b"a", path: b"b"})
        self.reject(blob, "package.path", file=path)

    def test_future_zip_extraction_version_is_addressed(self):
        blob = bytearray(self.blob)
        central = blob.index(b"PK\x01\x02")
        struct.pack_into("<H", blob, central + 6, 99)
        struct.pack_into("<H", blob, 4, 99)
        self.reject(bytes(blob), "package.unsupported", file="manifest.json")

    def test_invalid_zip_is_addressed(self):
        self.reject(b"not a zip", "package.integrity")
        self.reject(self.blob[:-10], "package.integrity")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = contents(make_package())["manifest.json"]

    def reject_json(self, data, rule="package.json", limits=None):
        with self.assertRaises(PackageError) as caught:
            decode_package_json(data, file="scene.json", byte_budget=100000, limits=limits or PackageLimits())
        self.assertEqual(caught.exception.rule, rule)
        self.assertEqual(caught.exception.file, "scene.json")

    def test_json_duplicates_nonfinite_encoding_and_root(self):
        for data in (b'{"a":{"x":1,"x":2}}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', b'{"a":1e999}', b'\xef\xbb\xbf{}', b'{"a":"\xff"}', b'{"a":"\\ud800"}', b'[]', b'{'):
            with self.subTest(data=data):
                self.reject_json(data)

    def test_json_depth_nodes_and_string_budgets(self):
        self.reject_json(b'{"a":[[[0]]]}', "package.budget", PackageLimits(json_depth=3))
        self.reject_json(b'{"a":[0,1,2]}', "package.budget", PackageLimits(json_nodes=4))
        self.reject_json(b'{"a":"long"}', "package.budget", PackageLimits(json_string_bytes=3))
        self.assertEqual(decode_package_json(b'{"a":"[\\\"{]"}', file="scene.json", byte_budget=100, limits=PackageLimits(json_depth=1)), {"a": '["{]'})

    def test_depth_rejected_before_json_decoder(self):
        with patch("model_generator.package_manifest.json.loads", side_effect=AssertionError("must budget first")):
            self.reject_json(b'{"a":[[[0]]]}', "package.budget", PackageLimits(json_depth=3))

    def test_malformed_enum_shapes_are_addressed(self):
        for value in ([], {}, False, 1, None):
            manifest = json.loads(self.manifest)
            manifest["capabilities"]["geometry"]["state"] = value
            with self.subTest(value=value), self.assertRaises(PackageError) as caught:
                parse_manifest(json.dumps(manifest).encode(), PackageLimits())
            self.assertEqual(caught.exception.rule, "package.schema")

    def test_path_helper_aggregate_component_budget(self):
        paths = ["evidence/one/a.txt", "evidence/two/b.txt"]
        with self.assertRaises(PackageError) as caught:
            _unique_paths(paths, PackageLimits(json_nodes=5))
        self.assertEqual(caught.exception.rule, "package.budget")
        self.assertEqual(caught.exception.file, paths[1])
        _unique_paths(paths, PackageLimits(json_nodes=6))

    def test_limits_positive_integer(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PackageLimits(json_depth=value)
        self.assertEqual(replace(PackageLimits(), entries=2).entries, 2)

    def test_missing_unknown_and_nested_metadata_fields(self):
        manifest = json.loads(self.manifest)
        mutations = [lambda m: m.pop("metadata"), lambda m: m.update(extra=1), lambda m: m["metadata"]["revit"].update(extra=1), lambda m: m["metadata"]["selection"].pop("scope"), lambda m: m["metadata"]["revit"].update(year=True), lambda m: m["metadata"].update(created_utc="2026-10-06T00:00:00"), lambda m: m["metadata"].update(created_utc="2026-99-06T00:00:00Z"), lambda m: m["metadata"].update(package_id=""), lambda m: m["metadata"].update(snapshot_id="x"*513), lambda m: m["capabilities"]["geometry"].update(evidence=[]), lambda m: m["required_capabilities"].append("scene-v1"), lambda m: m["required_capabilities"].remove("scene-v1")]
        for mutate in mutations:
            m = json.loads(self.manifest)
            mutate(m)
            with self.subTest(m=m), self.assertRaises(PackageError) as caught:
                parse_manifest(json.dumps(m).encode(), PackageLimits())
            self.assertEqual(caught.exception.rule, "package.schema")
        manifest["extensions"] = {"future": {"data": 1}}
        self.assertEqual(parse_manifest(json.dumps(manifest).encode(), PackageLimits()).package_version, (1, 0))

    def test_roles_paths_hash_and_capability_addresses(self):
        for mutate, rule in [(lambda m: m["files"][0].update(role="future"), "package.unsupported"), (lambda m: m["files"][1].update(path="evidence/vertices.bin"), "package.schema"), (lambda m: m["files"][0].update(sha256="A"*64), "package.schema"), (lambda m: m["files"][0].update(array={}), "package.schema"), (lambda m: m["capabilities"]["geometry"].update(limitations=[{"file": "missing.bin", "element_key": None, "message": "reason"}]), "package.schema"), (lambda m: m["capabilities"]["geometry"].update(limitations=[{"file": "", "element_key": {"document_id": "root", "link_instance_path": [], "unique_id": "id", "extra": 1}, "message": "reason"}]), "package.schema")]:
            manifest = json.loads(self.manifest)
            mutate(manifest)
            with self.subTest(rule=rule), self.assertRaises(PackageError) as caught:
                parse_manifest(json.dumps(manifest).encode(), PackageLimits())
            self.assertEqual(caught.exception.rule, rule)


if __name__ == "__main__":
    unittest.main()
