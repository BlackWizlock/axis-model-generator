"""Read ZIP content without extraction or filesystem resource resolution."""

from dataclasses import dataclass, field
import hashlib
import bz2
import io
import lzma
import stat
import struct
import unicodedata
import zipfile
import zlib

from .diagnostics import Finding
from .limits import Limits, ReadError


@dataclass
class Entry:
    name: str
    data: bytes
    sha256: str


@dataclass
class ArchiveResult:
    entries: list[Entry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    wrapped: bool = False


def _member_content(blob, archive, info):
    # ZipExtFile truncates output to the central directory size. Only use
    # open() for its local-header and overlap checks; decode independently.
    with archive.open(info):
        pass
    offset = info.header_offset
    if offset + 30 > len(blob):
        raise ReadError("zip.integrity", "Truncated local header")
    name_length, extra_length = struct.unpack_from("<HH", blob, offset + 26)
    start = offset + 30 + name_length + extra_length
    end = start + info.compress_size
    if end > len(blob):
        raise ReadError("zip.integrity", "Compressed data extends beyond archive")
    payload = blob[start:end]
    maximum = info.file_size + 1
    if info.compress_type == zipfile.ZIP_STORED:
        content = payload
    else:
        if info.compress_type == zipfile.ZIP_DEFLATED:
            decoder = zlib.decompressobj(-15)
        elif info.compress_type == zipfile.ZIP_BZIP2:
            decoder = bz2.BZ2Decompressor()
        elif info.compress_type == zipfile.ZIP_LZMA:
            if len(payload) < 9:
                raise ReadError("zip.integrity", "Truncated ZIP LZMA properties")
            properties_size = struct.unpack_from("<H", payload, 2)[0]
            if properties_size != 5 or payload[4] >= 225:
                raise ReadError("zip.integrity", "Invalid ZIP LZMA properties")
            encoded = payload[4]
            lc, remaining = encoded % 9, encoded // 9
            lp, pb = remaining % 5, remaining // 5
            dictionary = struct.unpack_from("<I", payload, 5)[0]
            if dictionary > 16 * 1024**2:
                raise ReadError("zip.budget", "LZMA dictionary exceeds 16 MiB technical limit")
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
                "id": lzma.FILTER_LZMA1, "dict_size": max(dictionary, 4096),
                "lc": lc, "lp": lp, "pb": pb}])
            payload = payload[9:]
            if not info.flag_bits & 2:
                raise ReadError("zip.unsupported", "LZMA without an end marker is unsupported")
        else:
            raise ReadError("zip.unsupported", "Unsupported ZIP compression method")
        content = decoder.decompress(payload, maximum)
        if not decoder.eof or decoder.unused_data:
            raise ReadError("zip.integrity", "Incomplete, excessive or trailing compressed stream")
    if len(content) != info.file_size or zlib.crc32(content) != info.CRC:
        raise ReadError("zip.integrity", "Actual size or CRC mismatch")
    return content


def read_archive(data, limits=None):
    limits = limits or Limits()
    result = ArchiveResult()
    used_bytes = 0
    used_entries = 0

    def fail(rule, name, message):
        result.findings.append(Finding(rule, "fail", name, None, None, message))

    def walk(blob, prefix, depth):
        nonlocal used_bytes, used_entries
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as archive:
                infos = archive.infolist()
                if used_entries + len(infos) > limits.entries:
                    raise ReadError("zip.budget", "Archive entry budget exceeded")
                used_entries += len(infos)
                seen = set()
                for info in infos:
                    try:
                        raw = info.filename.replace("\\", "/")
                        parts = raw.rstrip("/").split("/")
                        if (not raw or raw.startswith("/") or any(p in {"", ".", ".."} for p in parts)
                                or ":" in raw or "\x00" in raw or "\x00" in info.orig_filename):
                            raise ReadError("zip.path", f"Unsafe archive path: {info.filename!r}")
                        key = unicodedata.normalize("NFC", raw.rstrip("/")).casefold()
                        if key in seen:
                            raise ReadError("zip.duplicate", "Duplicate normalized archive path")
                        seen.add(key)
                        if stat.S_ISLNK(info.external_attr >> 16):
                            raise ReadError("zip.symlink", "Symbolic link is not permitted")
                        if info.flag_bits & 1:
                            raise ReadError("zip.encrypted", "Encrypted archive is unsupported")
                    except ReadError as exc:
                        fail(exc.rule, prefix + info.filename.replace("\\", "/"), str(exc))
                        return
                files = [i for i in infos if not i.is_dir()]
                nested = [i for i in files if i.filename.lower().endswith(".zip")]
                if nested and (len(files) != 1 or len(nested) != 1):
                    raise ReadError("zip.wrapper", "Ambiguous transport wrapper")
                if nested and depth >= limits.nested_depth:
                    raise ReadError("zip.depth", "Nested ZIP depth exceeded")
                for info in files:
                    name = prefix + info.filename.replace("\\", "/")
                    try:
                        allowance = min(limits.member_bytes, limits.expanded_bytes - used_bytes)
                        if info.file_size > allowance:
                            raise ReadError("zip.budget", "Expanded data budget exceeded")
                        # Reserve before decoding. Bad CRC or codec failure must
                        # not make the expansion budget available again.
                        used_bytes += info.file_size
                        content = _member_content(blob, archive, info)
                        if nested:
                            result.wrapped = True
                            walk(content, name + "/", depth + 1)
                        else:
                            result.entries.append(Entry(name, content, hashlib.sha256(content).hexdigest()))
                    except ReadError as exc:
                        fail(exc.rule, name, str(exc))
                    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError,
                            zlib.error, lzma.LZMAError, OSError, ValueError) as exc:
                        fail("zip.integrity", name, str(exc))
        except ReadError as exc:
            fail(exc.rule, prefix, str(exc))
        except (zipfile.BadZipFile, EOFError, ValueError) as exc:
            fail("zip.integrity", prefix, str(exc))

    if len(data) > limits.input_bytes:
        fail("zip.budget", "", "Input size exceeds budget")
    else:
        walk(data, "", 0)
    result.entries.sort(key=lambda item: item.name)
    return result
