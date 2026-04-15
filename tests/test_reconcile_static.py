from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReconcileStaticTests(unittest.TestCase):
    def test_reconcile_auto_shutdown_on_all_done(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("shutdown_finished_run()", script)
        self.assertIn("--phase done", script)
        self.assertIn("--type project_finished", script)
        self.assertIn('rm -f "${BZ_DIR}/reconcile.pid"', script)
        self.assertIn("shutdown_finished_run", script)
        self.assertIn("exit 0", script)

    def test_reconcile_syncs_project_outputs_across_worktrees(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("sync_outputs_from_worktrees()", script)
        self.assertIn("sync_outputs_to_worktrees()", script)
        self.assertIn('sync_outputs_from_worktrees 2>/dev/null || true', script)
        self.assertIn('sync_outputs_to_worktrees 2>/dev/null || true', script)
        self.assertIn('"${PROJECT_STATE_DIR}/outputs"/*/*', script)


if __name__ == "__main__":
    unittest.main()
