from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gravityclaw.ops import OperationsError, backup_home, database_health, restore_backup, verify_backup
from gravityclaw.store import Store


class OperationsTests(unittest.TestCase):
    def test_backup_verify_restore_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-ops-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(mode=0o700)
            store = Store(home / "gravityclaw.db")
            store.initialize()
            workspace = store.create_workspace("ops", home / "workspace")
            store.create_conversation(workspace.id, title="backup")
            (home / "SOUL.md").write_text("# Soul\n", encoding="utf-8")
            archive = root / "backup.tar.gz"
            backup_home(home, archive)
            verified = verify_backup(archive)
            self.assertEqual(verified["database"]["integrity_check"], "ok")
            restored = root / "restored"
            restore_backup(archive, restored)
            self.assertEqual(database_health(restored / "gravityclaw.db")["integrity_check"], "ok")
            self.assertEqual((restored / "SOUL.md").read_text(encoding="utf-8"), "# Soul\n")
            with self.assertRaises(OperationsError):
                restore_backup(archive, restored)

    def test_backup_rejects_destination_inside_live_home(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-ops-") as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir(mode=0o700)
            store = Store(home / "gravityclaw.db")
            store.initialize()
            with self.assertRaises(OperationsError):
                backup_home(home, home / "nested.tar.gz")


if __name__ == "__main__":
    unittest.main()
