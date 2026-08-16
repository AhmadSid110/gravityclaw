from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gravityclaw.config import RuntimeLayout, ensure_control_token, load_config, write_default_config
from gravityclaw.ops import backup_layout, restore_layout
from gravityclaw.release import activate_candidate, rollback
from gravityclaw.store import Store
from gravityclaw.identity import IdentityStore


class PackagingTests(unittest.TestCase):
    def test_canonical_layout_setup_and_restore_rebases_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-m10-") as temporary:
            root = Path(temporary) / "a"
            layout = RuntimeLayout.for_user(root)
            layout.create()
            write_default_config(layout)
            ensure_control_token(layout.secret_dir / "gravityclaw-control-token")
            Store(layout.database).initialize()
            IdentityStore(layout.identity_dir, runtime_home=layout.data_dir).bootstrap()
            (layout.identity_dir / "SOUL.md").write_text("# Personal soul\n", encoding="utf-8")
            archive = Path(temporary) / "backup.tar.gz"
            backup_layout(layout, archive)
            restored_root = Path(temporary) / "b"
            restore_layout(archive, restored_root)
            restored = RuntimeLayout.for_user(restored_root)
            config = load_config(restored.config_file)
            self.assertEqual(Path(config["database"]["path"]), restored.database)
            self.assertEqual(Path(config["backup"]["directory"]), restored.backup_dir)
            self.assertIn(str(restored.runtime_dir), restored.config_file.read_text(encoding="utf-8"))
            self.assertEqual((restored.identity_dir / "SOUL.md").read_text(encoding="utf-8"), "# Personal soul\n")

    def test_release_switch_is_atomic_and_rollback_is_available(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-release-") as temporary:
            layout = RuntimeLayout.for_user(Path(temporary) / "home")
            layout.create()
            first = Path(temporary) / "first"
            second = Path(temporary) / "second"
            first.mkdir()
            second.mkdir()
            (first / "release.txt").write_text("one", encoding="utf-8")
            (second / "release.txt").write_text("two", encoding="utf-8")
            activate_candidate(layout, first, "1.0.0")
            activate_candidate(layout, second, "2.0.0")
            self.assertEqual((layout.current_link / "payload" / "release.txt").read_text(), "two")
            rollback(layout)
            self.assertEqual((layout.current_link / "payload" / "release.txt").read_text(), "one")

    def test_settings_load_canonical_config_without_exposing_secret(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gravityclaw-settings-") as temporary:
            root = Path(temporary)
            layout = RuntimeLayout.for_user(root)
            layout.create()
            write_default_config(layout)
            token = ensure_control_token(layout.secret_dir / "gravityclaw-control-token")
            with patch.dict("os.environ", {
                "XDG_CONFIG_HOME": str(root / "config-root"),
                "XDG_DATA_HOME": str(root / "data-root"),
                "XDG_STATE_HOME": str(root / "state-root"),
                "XDG_RUNTIME_DIR": str(root / "runtime-root"),
                "GRAVITYCLAW_CONFIG": str(layout.config_file),
            }, clear=True):
                from gravityclaw.api import Settings
                # Explicit config paths are valid even when XDG roots are changed;
                # this is useful for systemd Environment= deployments.
                settings = Settings.from_environment()
                self.assertEqual(settings.home, layout.data_dir)
                self.assertEqual(settings.control_token, token)
            self.assertTrue(token)


if __name__ == "__main__":
    unittest.main()
