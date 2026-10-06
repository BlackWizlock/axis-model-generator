import io
import zipfile
import struct
import zlib


def zip_bytes(entries, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return stream.getvalue()


def prop(kind, value, compressed=False):
    if kind in "YCBIFDL":
        fmt = {"Y": "h", "C": "?", "B": "b", "I": "i", "F": "f", "D": "d", "L": "q"}[kind]
        return kind.encode() + struct.pack("<" + fmt, value)
    if kind in "SR":
        raw = value.encode() if isinstance(value, str) else value
        return kind.encode() + struct.pack("<I", len(raw)) + raw
    fmt = {"f": "f", "d": "d", "i": "i", "l": "q", "b": "b", "c": "B"}[kind]
    raw = struct.pack("<" + fmt * len(value), *value)
    payload = zlib.compress(raw) if compressed else raw
    return kind.encode() + struct.pack("<III", len(value), int(compressed), len(payload)) + payload


def fbx_bytes(nodes, version=7400):
    def encode(node, start):
        name, properties, children = node
        name = name.encode()
        properties_blob = b"".join(properties)
        cursor = start + 13 + len(name) + len(properties_blob)
        child_blob = b""
        for child in children:
            encoded = encode(child, cursor)
            child_blob += encoded
            cursor += len(encoded)
        if children:
            child_blob += bytes(13)
            cursor += 13
        return struct.pack("<IIIB", cursor, len(properties), len(properties_blob), len(name)) + name + properties_blob + child_blob
    blob = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", version)
    for node in nodes:
        blob += encode(node, len(blob))
    return blob + bytes(13)


def png_bytes(width=256, height=256, depth=8, color=2, transparency=False):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    image = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, 0))
    if transparency:
        image += chunk(b"tRNS", bytes(6))
    image += chunk(b"IDAT", zlib.compress(bytes((width * 3 + 1) * height)))
    return image + chunk(b"IEND", b"")


def scene_bytes(indices=(0, 1, -3), vertices=(0, 0, 0, 1, 0, 0, 0, 1, 0), texture=True):
    objects = [("Geometry", [prop("L", 1), prop("S", "Mesh"), prop("S", "Mesh")], [
        ("Vertices", [prop("d", vertices)], []), ("PolygonVertexIndex", [prop("i", indices)], [])]),
        ("Model", [prop("L", 2), prop("S", "Building"), prop("S", "Mesh")], [
            ("Properties70", [], [("P", [prop("S", "Lcl Translation"), prop("S", ""), prop("S", ""), prop("S", ""), prop("D", 10), prop("D", 0), prop("D", 0)], [])])])]
    links = [("C", [prop("S", "OO"), prop("L", 1), prop("L", 2)], [])]
    if texture:
        objects += [("Video", [prop("L", 3), prop("S", "Texture"), prop("S", "Clip")], [
            ("Content", [prop("R", png_bytes())], []), ("Filename", [prop("S", r"C:\legacy\a.png")], [])]),
            ("Texture", [prop("L", 4), prop("S", "Texture"), prop("S", "TextureVideoClip")], []),
            ("Material", [prop("L", 5), prop("S", "Material"), prop("S", "")], [])]
        links += [("C", [prop("S", "OO"), prop("L", 3), prop("L", 4)], []),
                  ("C", [prop("S", "OP"), prop("L", 4), prop("L", 5), prop("S", "DiffuseColor")], []),
                  ("C", [prop("S", "OO"), prop("L", 5), prop("L", 2)], [])]
    return fbx_bytes([("GlobalSettings", [], [("Properties70", [], [
        ("P", [prop("S", "UnitScaleFactor"), prop("S", "double"), prop("S", ""), prop("S", ""), prop("D", 100)], [])])]),
        ("Objects", [], objects), ("Connections", [], links)])
