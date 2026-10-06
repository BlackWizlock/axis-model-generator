"""Read one autonomous ZIP in memory with independent bounded decompression."""

from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import stat
import struct
import zipfile
import zlib

from .package_manifest import (PackageError, PackageLimits, PackageManifest,
                               _unique_paths, check_member_suffix, parse_manifest)


@dataclass(frozen=True)
class PackageData:
    manifest: PackageManifest
    members: dict[str, bytes]
    input_sha256: str


_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_CHUNK = 65536


def _error(rule, message, file="", *, actual=None, expected=None):
    raise PackageError(rule, message, file=file, actual=actual, expected=expected)


def _directory_budget(data, limits):
    """Bound directory allocation before ZipFile constructs its ZipInfo list."""
    if not data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        _error("package.integrity", "Expected an unwrapped ZIP container")
    position = data.rfind(b"PK\x05\x06", max(0, len(data) - 65557))
    if position < 0 or position + 22 > len(data):
        _error("package.integrity", "Missing ZIP end record")
    disk, start_disk, disk_count, total_count, size, offset, comment = struct.unpack_from("<HHHHIIH", data, position + 4)
    if position + 22 + comment != len(data):
        _error("package.integrity", "Trailing or truncated ZIP end record")
    if disk or start_disk or disk_count != total_count:
        _error("package.unsupported", "Multi-disk ZIP is unsupported")
    # Python honors a locator immediately before EOCD even without sentinels.
    # Reject every global ZIP64 layout before it can redirect directory parsing.
    locator = position - 20
    if (disk_count == 65535 or total_count == 65535 or offset == 0xffffffff or size == 0xffffffff
            or (locator >= 0 and data[locator:locator + 4] == b"PK\x06\x07")):
        _error("package.unsupported", "Global ZIP64 end records are unsupported")
    directory_end = position
    if total_count > limits.entries:
        _error("package.budget", "ZIP entry count exceeds budget", actual=total_count, expected=limits.entries)
    if offset + size != directory_end or offset < 0:
        _error("package.integrity", "Invalid ZIP central directory bounds")
    # Count actual records too, so a forged end count cannot evade the budget.
    cursor = offset
    actual_count = 0
    while cursor < directory_end:
        if cursor + 46 > directory_end or data[cursor:cursor + 4] != b"PK\x01\x02":
            _error("package.integrity", "Invalid central directory entry")
        name, extra, comment = struct.unpack_from("<HHH", data, cursor + 28)
        if data[cursor + 6] > zipfile.MAX_EXTRACT_VERSION:
            flags = struct.unpack_from("<H", data, cursor + 8)[0]
            raw_name = data[cursor + 46:cursor + 46 + name]
            file = raw_name.decode("utf-8" if flags & 0x800 else "cp437", errors="replace")
            _error("package.unsupported", "Unsupported ZIP extraction version", file)
        cursor += 46 + name + extra + comment
        actual_count += 1
        if actual_count > limits.entries:
            _error("package.budget", "ZIP entry count exceeds budget")
    if cursor != directory_end or actual_count != total_count:
        _error("package.integrity", "ZIP directory count or size mismatch")


def _check_inventory_metadata(infos, limits):
    _unique_paths((info.orig_filename for info in infos), limits)
    total = 0
    for info in infos:
        name = info.orig_filename
        check_member_suffix(name)
        if info.filename != name or info.is_dir():
            _error("package.path", "Directory or truncated member name is forbidden", name)
        if not info.flag_bits & 0x800 and not name.isascii():
            _error("package.path", "Non-ASCII member names must be encoded as UTF-8", name)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in {0, stat.S_IFREG} or info.external_attr & 0x10:
            _error("package.type", "Only regular file members are permitted", name)
        if info.flag_bits & (1 | 0x40):
            _error("package.encrypted", "Encrypted members are forbidden", name)
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            _error("package.unsupported", "Unsupported compression codec", name)
        maximum = limits.member_bytes
        if name == "manifest.json":
            maximum = min(maximum, limits.manifest_bytes)
        elif name == "scene.json":
            maximum = min(maximum, limits.scene_bytes)
        if info.file_size > maximum:
            _error("package.budget", "Declared member bytes exceed budget", name, actual=info.file_size, expected=maximum)
        if info.file_size > max(info.compress_size, 1) * limits.compression_ratio:
            _error("package.budget", "Declared compression ratio exceeds budget", name)
        total += info.file_size
        if total > limits.expanded_bytes:
            _error("package.budget", "Declared expanded bytes exceed budget", name, actual=total, expected=limits.expanded_bytes)


def _read_member(data, archive, info, remaining, limits):
    """Use ZipFile for header/overlap checks, but never trust its output cap."""
    name = info.filename
    try:
        with archive.open(info):
            pass
        offset = info.header_offset
        if offset < 0 or offset + 30 > len(data) or data[offset:offset + 4] != b"PK\x03\x04":
            _error("package.integrity", "Invalid local ZIP header", name)
        flags, codec = struct.unpack_from("<HH", data, offset + 6)
        if flags != info.flag_bits or codec != info.compress_type:
            _error("package.integrity", "Local and central ZIP metadata differ", name)
        name_size, extra_size = struct.unpack_from("<HH", data, offset + 26)
        start = offset + 30 + name_size + extra_size
        end = start + info.compress_size
        if end > len(data):
            _error("package.integrity", "Compressed payload extends beyond container", name)
        # Decode at most declared size + one byte, additionally bounded by budgets.
        maximum = min(info.file_size, remaining, limits.member_bytes)
        if name == "manifest.json":
            maximum = min(maximum, limits.manifest_bytes)
        elif name == "scene.json":
            maximum = min(maximum, limits.scene_bytes)
        output = bytearray()
        digest = hashlib.sha256()
        crc = 0
        decoder = zlib.decompressobj(-15) if codec == zipfile.ZIP_DEFLATED else None
        cursor = start
        while cursor < end:
            input_chunk = _CHUNK if decoder else min(_CHUNK, maximum - len(output) + 1)
            chunk = data[cursor:min(cursor + input_chunk, end)]
            cursor += len(chunk)
            decoded = decoder.decompress(chunk, maximum - len(output) + 1) if decoder else chunk
            if len(output) + len(decoded) > maximum:
                _error("package.integrity", "Actual expanded size exceeds declaration or budget", name, actual=len(output) + len(decoded), expected=maximum)
            output.extend(decoded)
            digest.update(decoded)
            crc = zlib.crc32(decoded, crc)
            if decoder and (decoder.unconsumed_tail or decoder.unused_data):
                _error("package.integrity", "Excessive or trailing compressed payload", name)
            if len(output) > max(info.compress_size, 1) * limits.compression_ratio:
                _error("package.budget", "Actual compression ratio exceeds budget", name)
        if decoder and not decoder.eof:
            _error("package.integrity", "Incomplete compressed payload", name)
        if len(output) != info.file_size or crc != info.CRC:
            _error("package.integrity", "Actual member size or CRC mismatch", name, actual=len(output), expected=info.file_size)
        content = bytes(output)
        if content.startswith(_ZIP_SIGNATURES):
            _error("package.content", "Nested ZIP signature is forbidden", name)
        return content, digest.hexdigest()
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error, struct.error, UnicodeError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("package.integrity", "Invalid member stream or ZIP header", file=name) from None


def read_package_bytes(data: bytes, limits: PackageLimits | None = None) -> PackageData:
    limits = limits or PackageLimits()
    if len(data) > limits.input_bytes:
        _error("package.budget", "Input bytes exceed budget", actual=len(data), expected=limits.input_bytes)
    try:
        _directory_budget(data, limits)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            _check_inventory_metadata(infos, limits)
            inventory = {info.filename: info for info in infos}
            if "manifest.json" not in inventory:
                _error("package.inventory", "Missing root manifest", "manifest.json")
            manifest_data, _ = _read_member(data, archive, inventory["manifest.json"], limits.expanded_bytes, limits)
            manifest = parse_manifest(manifest_data, limits)
            descriptors = {item.path: item for item in manifest.files}
            for name in inventory:
                if name != "manifest.json" and name not in descriptors:
                    _error("package.inventory", "Unlisted ZIP member", name)
            for name, descriptor in descriptors.items():
                if name not in inventory:
                    _error("package.inventory", "Listed member is missing", name)
                if inventory[name].file_size != descriptor.bytes:
                    _error("package.size", "Manifest and ZIP declared sizes differ", name, actual=inventory[name].file_size, expected=descriptor.bytes)
            used = len(manifest_data)
            members = {}
            for descriptor in manifest.files:
                content, sha256 = _read_member(data, archive, inventory[descriptor.path], limits.expanded_bytes - used, limits)
                used += len(content)
                if len(content) != descriptor.bytes:
                    _error("package.size", "Actual member bytes differ from manifest", descriptor.path, actual=len(content), expected=descriptor.bytes)
                if sha256 != descriptor.sha256:
                    _error("package.hash", "Member SHA-256 differs from manifest", descriptor.path, actual=sha256, expected=descriptor.sha256)
                members[descriptor.path] = content
            return PackageData(manifest, members, hashlib.sha256(data).hexdigest())
    except (zipfile.BadZipFile, struct.error, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError("package.integrity", "Invalid ZIP container") from None


def read_package_path(path: Path, limits: PackageLimits | None = None) -> PackageData:
    limits = limits or PackageLimits()
    with path.open("rb") as stream:
        # read(size) bounds growth even if stat lies or a file grows during reading.
        data = stream.read(limits.input_bytes + 1)
    return read_package_bytes(data, limits)
