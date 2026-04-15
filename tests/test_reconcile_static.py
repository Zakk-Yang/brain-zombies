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


if __name__ == "__main__":
    unittest.main()
