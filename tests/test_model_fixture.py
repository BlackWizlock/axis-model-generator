"""The model the web tests read is a file of the project writer, never a hand-made one."""
from array import array
import base64
from pathlib import Path
import struct
import unittest
import zlib

from model_generator.fbx_write import ExportMaterial, ExportMesh, ExportTexture, write_fbx

FIXTURE = Path(__file__).resolve().parents[1] / 'web' / 'tests' / 'support' / 'model-fixture.mjs'


def picture(red, green, blue, size=4):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))
    rows = (b'\x00' + bytes((red, green, blue)) * size) * size
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows, 9)) + chunk(b'IEND', b''))


def fixture_meshes():
    """A wall of 40 triangles, a sign sharing its picture, a block of two plain materials."""
    red, grey = picture(255, 0, 0), picture(128, 128, 128, 8)
    wall = ExportMaterial('Wall', (0.5, 0.5, 0.5), (ExportTexture('DiffuseColor', 'wall.png', red),
                                                    ExportTexture('ShininessExponent', 'rough.png', grey)))
    sign = ExportMaterial('Sign', textures=(ExportTexture('DiffuseColor', 'wall.png', red),))
    plain, glass = ExportMaterial('Plain', (0.2, 0.7, 0.2)), ExportMaterial('Glass', (0.1, 0.4, 0.9))
    positions, triangles, uvs = array('d'), array('I'), array('d')
    for j in range(5):
        for i in range(6):
            positions.extend((100 + i * 1.5, 7.0, j * 0.75))
    for j in range(4):
        for i in range(5):
            a = j * 6 + i
            for corner in (a, a + 1, a + 7, a, a + 7, a + 6):
                triangles.append(corner)
                uvs.extend(((corner % 6) / 5, (corner // 6) / 4))
    square = array('d', (120, 7, 0, 122, 7, 0, 122, 7, 1, 120, 7, 1))
    island = array('d', (0.25, 0.25, 0.75, 0.25, 0.75, 0.75, 0.25, 0.25, 0.75, 0.75, 0.25, 0.75))
    block = array('d', (130, 0, 0, 131, 0, 0, 131, 1, 0, 130, 1, 0, 130.5, 0.5, 3))
    return [ExportMesh('Wall', positions, triangles, (wall,), uvs=uvs),
            ExportMesh('Sign', square, array('I', (0, 1, 2, 0, 2, 3)), (sign,), uvs=island),
            ExportMesh('Block', block, array('I', (0, 1, 4, 1, 2, 4, 2, 3, 4, 3, 0, 4)), (plain, glass),
                       array('i', (0, 1, 1, 0)))]


def fixture_module():
    data, red = write_fbx(fixture_meshes()), picture(255, 0, 0)
    return ('// Записано записывающим модулем проекта, сверяется тестом tests/test_model_fixture.py.\n'
            f"export const MODEL_BASE64='{base64.b64encode(data).decode('ascii')}';\n"
            f"export const PICTURE_BASE64='{base64.b64encode(red).decode('ascii')}';\n")


class ModelFixtureTests(unittest.TestCase):
    def test_web_fixture_is_the_file_the_writer_produces_today(self):
        self.assertEqual(FIXTURE.read_text(encoding='utf-8'), fixture_module())


if __name__ == '__main__':
    FIXTURE.write_text(fixture_module(), encoding='utf-8')
