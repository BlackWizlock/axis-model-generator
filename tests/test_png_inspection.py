import struct
import zlib
import unittest
from fixtures.builders import png_bytes
from model_generator.png_inspection import inspect_png
from model_generator.limits import ReadError


class PngTests(unittest.TestCase):
    def test_metadata_and_alpha(self):
        for color, trns, alpha in [(2, False, False), (6, False, True), (2, True, True)]:
            with self.subTest(color=color, trns=trns):
                result = inspect_png(png_bytes(color=color, transparency=trns))
                self.assertEqual(result["alpha"], alpha)
                self.assertEqual(result["width"], 256)
                self.assertEqual(result["bit_depth"], 8)
        self.assertEqual(inspect_png(png_bytes(depth=16))["bit_depth"], 16)
        self.assertEqual(inspect_png(png_bytes(width=300))["width"], 300)

    def test_invalid_chunks_and_crc(self):
        valid = png_bytes()
        bad_crc = bytearray(valid)
        bad_crc[29] ^= 1
        for data in [b"no png", bytes(bad_crc), valid[:-1], valid[:-12], valid[:8]+valid[33:],
                     valid+b"trailing"]:
            with self.subTest(data=data[:32]), self.assertRaises(ReadError):
                inspect_png(data)

    def test_grayscale_cannot_have_palette(self):
        for color in (0, 4):
            with self.subTest(color=color), self.assertRaises(ReadError):
                inspect_png(self.with_chunk(png_bytes(color=color), b"PLTE", b"\0\0\0"))

    def test_chunk_name_requires_ascii_letters(self):
        for kind in (b"a1AA", b"a AA", b"a\xffAA", b"a_AA"):
            with self.subTest(kind=kind), self.assertRaises(ReadError):
                inspect_png(self.with_chunk(png_bytes(), kind))
        self.assertEqual(inspect_png(self.with_chunk(png_bytes(), b"vpAg"))["width"], 256)

    @staticmethod
    def with_chunk(image, kind, payload=b""):
        chunk = struct.pack(">I", len(payload)) + kind + payload
        chunk += struct.pack(">I", zlib.crc32(kind + payload))
        return image[:33] + chunk + image[33:]
