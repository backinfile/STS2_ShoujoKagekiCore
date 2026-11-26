import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import tempfile
import unittest

from install_support import ReleaseTransaction, database_backup, rollback_database


class InstallRollbackTests(unittest.TestCase):
    def test_database_rollback_preserves_failed_migration_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live, before, failed = (root / name for name in ("live.sqlite3", "before.sqlite3", "failed.sqlite3"))
            db = sqlite3.connect(live)
            with db:
                db.execute("CREATE TABLE runs(id INTEGER)")
                db.execute("INSERT INTO runs VALUES (1)")
            db.close()
            database_backup(live, before)
            db = sqlite3.connect(live)
            with db:
                db.execute("ALTER TABLE runs ADD COLUMN migrated INTEGER")
            db.close()
            rollback_database(live, before, failed)
            for path, columns in ((live, ["id"]), (failed, ["id", "migrated"])):
                db = sqlite3.connect(path)
                try:
                    self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(runs)")], columns)
                    self.assertEqual(db.execute("SELECT id FROM runs").fetchone(), (1,))
                finally:
                    db.close()

    @unittest.skipIf(os.name == "nt", "The installer switches Linux deployment symlinks")
    def test_restores_files_links_units_and_nginx_after_candidate_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            new_file = root / "unit"
            previous, candidate = root / "previous", root / "candidate"
            previous.mkdir()
            candidate.mkdir()
            current = root / "current"
            current.symlink_to(previous, target_is_directory=True)
            config.write_text("old", encoding="utf-8")
            config.chmod(0o600)
            original_mode = stat.S_IMODE(config.stat().st_mode)
            active = {"collector.service": True, "collector-backup.timer": False}
            enabled = {"collector.service": "enabled", "collector-backup.timer": "disabled"}
            calls = []

            def command(args, **_):
                action, *targets = args[1:]
                calls.append((action, tuple(targets)))
                if action == "is-active":
                    return subprocess.CompletedProcess(args, 0 if active[targets[-1]] else 3)
                if action == "is-enabled":
                    return subprocess.CompletedProcess(args, 0, enabled[targets[-1]])
                if action in ("stop", "start"):
                    active[targets[-1]] = action == "start"
                if action in ("enable", "disable"):
                    enabled[targets[-1]] = "enabled" if action == "enable" else "disabled"
                return subprocess.CompletedProcess(args, 0)

            with self.assertRaisesRegex(RuntimeError, "candidate failed"):
                with ReleaseTransaction([config, new_file, current], list(active), "collector.service", command) as release:
                    release.write(config, "new", mode=0o644)
                    release.write(new_file, "unit")
                    release.switch(current, candidate)
                    release.reload_units()
                    release.unit("start", "collector.service")
                    release.unit("enable", "collector-backup.timer")
                    release.unit("start", "collector-backup.timer")
                    release.reload_nginx()
                    raise RuntimeError("candidate failed")
            self.assertEqual(config.read_text(encoding="utf-8"), "old")
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), original_mode)
            self.assertFalse(new_file.exists())
            self.assertEqual(current.resolve(), previous.resolve())
            self.assertEqual(active, {"collector.service": True, "collector-backup.timer": False})
            self.assertEqual(enabled, {"collector.service": "enabled", "collector-backup.timer": "disabled"})
            self.assertEqual(calls.count(("reload", ("nginx",))), 2)


if __name__ == "__main__":
    unittest.main()
