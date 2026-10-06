"""PNG structure inspection, without pixel decoding."""

import struct
import zlib
from .limits import ReadError


def inspect_png(data):
    def fail(message):
        raise ReadError("png.integrity", message)

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        fail("Invalid PNG signature")
    cursor = 8
    result = None
    idat = False
    idat_ended = False
    palette = False
    transparency = False
    while cursor < len(data):
        if cursor + 12 > len(data):
            fail("Truncated PNG chunk")
        length = struct.unpack_from(">I", data, cursor)[0]
        kind = data[cursor+4:cursor+8]
        if any(not (65 <= byte <= 90 or 97 <= byte <= 122) for byte in kind):
            fail("PNG chunk type must contain four ASCII letters")
        end = cursor + 12 + length
        if end > len(data):
            fail("PNG chunk extends beyond file")
        payload = data[cursor+8:end-4]
        crc = struct.unpack_from(">I", data, end-4)[0]
        if zlib.crc32(kind+payload) != crc:
            fail("Invalid PNG CRC")
        if result is None and kind != b"IHDR":
            fail("IHDR must be first")
        if kind == b"IHDR":
            if result is not None or length != 13:
                fail("Invalid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            allowed = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if not width or not height or depth not in allowed.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                fail("Unsupported or invalid IHDR values")
            result = {"width": width, "height": height, "bit_depth": depth,
                      "color_type": color, "alpha": color in {4, 6}, "bytes": len(data),
                      "pixel_decoding": "not_checked"}
        elif kind == b"PLTE":
            if result["color_type"] in {0, 4} or palette or idat or not length or length % 3 or length > 768:
                fail("Invalid palette")
            palette = True
        elif kind == b"tRNS":
            color = result["color_type"]
            if transparency or idat or color in {4, 6} or (color == 2 and length != 6) or (color == 0 and length != 2) or (color == 3 and (not palette or not length or length > 256)):
                fail("Invalid transparency chunk")
            transparency = True
            result["alpha"] = True
        elif kind == b"IDAT":
            if idat_ended or (result["color_type"] == 3 and not palette):
                fail("Invalid IDAT ordering")
            idat = True
        elif kind == b"IEND":
            if length or not idat or end != len(data):
                fail("Invalid IEND or trailing data")
            return result
        elif kind and kind[0] & 32 == 0:
            fail("Unknown critical PNG chunk")
        if idat and kind != b"IDAT":
            idat_ended = True
        cursor = end
    fail("Missing IEND")
