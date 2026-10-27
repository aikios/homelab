"""Tests for deploy/migrate.py. Run: uv run python -m unittest discover -s deploy/tests"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrate


class EnvKeys(unittest.TestCase):
    def test_renames_keys_only(self):
        text = "# FARM_LOG=old comment\nFARM_LOG=/x\nexport FARM_LOCK = y\nFARM_LOGX=z\n"
        out, done = migrate.rename_env_keys(
            text, {"FARM_LOG": "MT_LOG", "FARM_LOCK": "MT_LOCK", "NOPE": "N"})
        self.assertEqual(out, "# FARM_LOG=old comment\nMT_LOG=/x\nexport MT_LOCK = y\nFARM_LOGX=z\n")
        self.assertEqual(done, ["FARM_LOG", "FARM_LOCK"])


class Remap(unittest.TestCase):
    def test_prefix(self):
        paths = {"payloads": "static"}
        self.assertEqual(migrate.remap(Path("payloads/a/b.html"), paths), Path("static/a/b.html"))
        self.assertEqual(migrate.remap(Path("payloads"), paths), Path("static"))
        self.assertEqual(migrate.remap(Path("payloadsx/a"), paths), Path("payloadsx/a"))
        self.assertEqual(migrate.remap(Path("state/x.json"), paths), Path("state/x.json"))


class MoveLeftovers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.old = self.repo / "services/old"
        self.new = self.repo / "services/new"
        (self.old / "state").mkdir(parents=True)
        (self.old / "__pycache__").mkdir()
        (self.old / "tracked.py").write_text("x")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        (self.old / ".env").write_text("A=1\n")
        (self.old / "state/s.json").write_text("{}")
        (self.old / "__pycache__/m.pyc").write_text("")
        self.patch = mock.patch.object(migrate, "REPO", self.repo)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_moves_untracked_only(self):
        self.assertEqual(migrate.leftovers(self.old), [Path(".env"), Path("state/s.json")])
        (self.old / "tracked.py").unlink()  # as after the rename commit
        subprocess.run(["git", "-C", str(self.repo), "rm", "-q", "--cached", "services/old/tracked.py"], check=True)
        self.assertEqual(migrate.move_leftovers(self.old, self.new, {}), [])
        self.assertEqual((self.new / ".env").read_text(), "A=1\n")
        self.assertTrue((self.new / "state/s.json").exists())
        self.assertFalse(self.old.exists())

    def test_clash_is_not_overwritten(self):
        self.new.mkdir(parents=True)
        (self.new / ".env").write_text("B=2\n")
        clashes = migrate.move_leftovers(self.old, self.new, {})
        self.assertEqual(len(clashes), 1)
        self.assertEqual((self.new / ".env").read_text(), "B=2\n")
        self.assertEqual((self.old / ".env").read_text(), "A=1\n")
        self.assertTrue(self.old.exists())


if __name__ == "__main__":
    unittest.main()
