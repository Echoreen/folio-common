"""Files Folio / Numa save get normal permissions (not the owner-only ones of a temporary file)."""
import os
import stat
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from folio_common.api import Http
from folio_nfo.core import FILE_MODE, write_elements


class TestFileMode(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        for p in sorted(self.tmp.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        self.tmp.rmdir()

    def mode(self, p: Path) -> int:
        return stat.S_IMODE(os.stat(p).st_mode)

    def test_mode_follows_umask(self):
        umask = os.umask(0)
        os.umask(umask)
        self.assertEqual(FILE_MODE, 0o666 & ~umask)

    def test_nfo_is_readable_by_others(self):
        p = self.tmp / "album.nfo"
        write_elements(p, [ET.Element("album")])
        self.assertEqual(self.mode(p), FILE_MODE)
        self.assertEqual(list(self.tmp.glob(".folio-*")), [])     # no temporary file left behind

    def test_download_is_readable_by_others(self):
        src = self.tmp / "src.jpg"
        src.write_bytes(b"jpeg")
        dest = self.tmp / "art" / "poster.jpg"
        Http(cache_dir=None, min_interval=0).download(src.as_uri(), dest)
        self.assertEqual(dest.read_bytes(), b"jpeg")
        self.assertEqual(self.mode(dest), FILE_MODE)


if __name__ == "__main__":
    unittest.main()
