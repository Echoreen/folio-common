"""After an update changes how info files are read, a normal scan re-reads them (Artist Info
included) by itself: no "Re-read every file"."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from folio_common import library


def nfo(aliases=""):
    return ('<?xml version="1.0"?><artist><name>Takase Toya</name>'
            f'<musicBrainzArtistID>toya-id</musicBrainzArtistID>{aliases}</artist>')


class TestNfoReread(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "music"
        (self.root / "Takase Toya" / "[8]").mkdir(parents=True)
        (self.root / "Takase Toya" / "[8]" / "album.nfo").write_text(
            '<?xml version="1.0"?><album><title>[8]</title><artist>Takase Toya</artist></album>')
        self.info = self.tmp / "Artist Info"
        (self.info / "Takase Toya").mkdir(parents=True)
        self.page = self.info / "Takase Toya" / "artist.nfo"
        self.page.write_text(nfo())
        self.db = self.tmp / "music.db"
        self._version = library.NFO_PARSER_VERSION

    def tearDown(self):
        library.NFO_PARSER_VERSION = self._version
        shutil.rmtree(self.tmp)

    def aliases(self):
        return library.load_artist_info(self.db)["takase toya"]["nfo"].get("aliases")

    def test_new_reading_code_rereads_artist_info_on_a_normal_scan(self):
        library.scan(self.root, self.db, artist_info=self.info)
        self.assertEqual(self.aliases(), [])
        # the file gains something, but its date stays the same (like a page saved by an old version)
        st = self.page.stat()
        self.page.write_text(nfo("<alias>Takasetoya</alias>"))
        os.utime(self.page, (st.st_atime, st.st_mtime))
        library.scan(self.root, self.db, artist_info=self.info)
        self.assertEqual(self.aliases(), [])                      # same code, same date: kept as it was
        library.NFO_PARSER_VERSION = "an update"                  # the reading code changed
        library.scan(self.root, self.db, artist_info=self.info)
        self.assertEqual(self.aliases(), ["Takasetoya"])          # re-read by itself

    def test_the_stamp_follows_the_code(self):
        a = library.code_fingerprint(library.nfolib.__file__)
        self.assertEqual(a, library.code_fingerprint(library.nfolib.__file__))
        extra = self.tmp / "reader.py"
        extra.write_text("x = 1")
        b = library.code_fingerprint(library.nfolib.__file__, extra)
        extra.write_text("x = 2")
        self.assertNotEqual(b, library.code_fingerprint(library.nfolib.__file__, extra))


if __name__ == "__main__":
    unittest.main()
