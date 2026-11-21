"""The "Folio was updated" bar: notices changed code files, and restarts with the same command."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:
    from folio_common import update_notice as un
except ImportError:                                   # no PySide6 here
    un = None


@unittest.skipIf(un is None, "PySide6 isn't installed")
class TestUpdateNotice(unittest.TestCase):
    def test_stamp_changes_with_the_code(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "folio.py"
            f.write_text("x = 1")
            before = un.code_stamp([Path(d)])
            t = time.time() + 5
            f.write_text("x = 2")
            os.utime(f, (t, t))
            self.assertNotEqual(before, un.code_stamp([Path(d)]))
            (Path(d) / "notes.txt").write_text("not code")              # other files don't count
            self.assertEqual(un.code_stamp([Path(d)]), un.code_stamp([Path(d)]))

    def test_restart_uses_the_same_command(self):
        cmd = un.restart_command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertGreaterEqual(len(cmd), 2)


if __name__ == "__main__":
    unittest.main()
