"""Bounded independent binary FBX 7400 reader."""

from array import array
from dataclasses import dataclass
import struct
import sys
import zlib

from .limits import Limits, ReadError

MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"


@dataclass
class Node:
    name: str
    props: list
    children: list

    def child(self, name):
        return next((item for item in self.children if item.name == name), None)


def parse_fbx(data, limits=None):
    limits = limits or Limits()
    if not data.startswith(MAGIC):
        raise ReadError("fbx.unsupported", "Only binary FBX 7400 is supported")
    if len(data) < 27:
        raise ReadError("fbx.integrity", "Truncated FBX header")
    if struct.unpack_from("<I", data, 23)[0] != 7400:
        raise ReadError("fbx.unsupported", "Unsupported binary FBX version")
    position = 27
    nodes_used = 0
    arrays_used = 0

    def take(size, bound):
        nonlocal position
        if size < 0 or position + size > bound:
            raise ReadError("fbx.integrity", f"Truncated property at byte {position}")
        raw = data[position:position+size]
        position += size
        return raw

    def unpack(fmt, bound):
        return struct.unpack("<" + fmt, take(struct.calcsize("<" + fmt), bound))

    def property_value(bound):
        nonlocal arrays_used
        kind = take(1, bound)
        scalars = {b"Y": "h", b"C": "?", b"B": "b", b"I": "i", b"F": "f", b"D": "d", b"L": "q"}
        if kind in scalars:
            return unpack(scalars[kind], bound)[0]
        if kind in {b"S", b"R"}:
            value = take(unpack("I", bound)[0], bound)
            return value.decode("utf-8", errors="replace") if kind == b"S" else value
        arrays = {b"f": "f", b"d": "d", b"i": "i", b"l": "q", b"b": "b", b"c": "B"}
        if kind not in arrays:
            raise ReadError("fbx.integrity", f"Unknown property type {kind!r}")
        count, encoding, length = unpack("III", bound)
        fmt = arrays[kind]
        expected = count * struct.calcsize("<" + fmt)
        if expected > limits.fbx_array_bytes - arrays_used:
            raise ReadError("fbx.budget", "Array budget exceeded")
        arrays_used += expected
        payload = take(length, bound)
        if encoding == 0:
            raw = payload
        elif encoding == 1:
            try:
                decoder = zlib.decompressobj()
                raw = decoder.decompress(payload, expected + 1)
                if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                    raise ReadError("fbx.integrity", "Incomplete or excessive compressed array")
            except zlib.error as exc:
                raise ReadError("fbx.integrity", "Invalid zlib array") from exc
        else:
            raise ReadError("fbx.integrity", "Unsupported array encoding")
        if len(raw) != expected:
            raise ReadError("fbx.integrity", "Array length mismatch")
        values = array(fmt)
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        return values

    def node(bound, depth):
        nonlocal position, nodes_used
        if position + 13 > bound:
            raise ReadError("fbx.integrity", "Truncated node header")
        end, count, length, name_length = unpack("IIIB", bound)
        if end == count == length == name_length == 0:
            return None
        nodes_used += 1
        if nodes_used > limits.fbx_nodes or depth > limits.fbx_depth or count > limits.fbx_properties:
            raise ReadError("fbx.budget", "Node, depth or property budget exceeded")
        if end < position + name_length + length or end > bound:
            raise ReadError("fbx.integrity", "Invalid node end offset")
        name = take(name_length, end).decode("utf-8", errors="replace")
        props_end = position + length
        props = [property_value(props_end) for _ in range(count)]
        if position != props_end:
            raise ReadError("fbx.integrity", "Property byte count mismatch")
        children = []
        if position < end:
            terminated = False
            while position < end:
                child = node(end, depth+1)
                if child is None:
                    terminated = True
                    break
                children.append(child)
            if not terminated or position != end:
                raise ReadError("fbx.integrity", "Invalid child terminator")
        return Node(name, props, children)

    roots = []
    while position < len(data):
        root = node(len(data), 1)
        if root is None:
            return roots
        roots.append(root)
    raise ReadError("fbx.integrity", "Missing root terminator")
