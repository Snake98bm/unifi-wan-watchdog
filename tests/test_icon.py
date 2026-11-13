"""Tests for the app icon. Run: python tests/test_icon.py"""
import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import icon  # noqa: E402


class IconTests(unittest.TestCase):
    def test_png_is_valid(self):
        data = icon.png(32)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (32, 32))
        raw = zlib.decompress(data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8])
        self.assertEqual(len(raw), 32 * (1 + 32 * 4))

    def test_corners_and_center(self):
        size = 64
        raw = zlib.decompress(icon.png(size)[41:-20])
        px = lambda x, y: raw[y * (1 + size * 4) + 1 + x * 4:][:4]
        self.assertEqual(px(0, 0)[3], 0)                 # rounded corner is transparent
        self.assertEqual(tuple(px(32, 3)), (*icon.TILE, 255))
        full = zlib.decompress(icon.png(size, full_bleed=True)[41:-20])
        self.assertEqual(full[1 + 3], 255)               # apple icon has no transparent corner

    def test_build_writes_every_file(self):
        with tempfile.TemporaryDirectory() as d:
            icon.build(d)
            for f in icon.FILES:
                self.assertGreater(os.path.getsize(os.path.join(d, f)), 100, f)
        self.assertIn("<svg", icon.svg())


if __name__ == "__main__":
    unittest.main()
