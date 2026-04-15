from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReconcileStaticTests(unittest.TestCase):
    def test_reconcile_auto_shutdown_on_all_done(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()
        all_done_body = script[
            script.index("all_done() {") : script.index("shutdown_finished_run() {")
        ]

        self.assertIn("shutdown_finished_run()", script)
        self.assertIn("--phase done", script)
        self.assertIn("--type project_finished", script)
        self.assertIn('rm -f "${BZ_DIR}/reconcile.pid"', script)
        self.assertIn("shutdown_finished_run", script)
        self.assertIn("exit 0", script)
        self.assertIn('[[ "$state" != "done" && "$state" != "finished" ]]', all_done_body)
        self.assertNotIn('"ready-for-review"', all_done_body)
        self.assertIn('needs_brain="$(status_field "${agent_dir}/STATUS.md" "Needs brain"', all_done_body)
        self.assertIn('blocker="$(status_field "${agent_dir}/STATUS.md" "Blocker"', all_done_body)
        self.assertIn("*-supervisor|*-dashboard|*-nerve)", script)

    def test_reconcile_syncs_project_outputs_across_worktrees(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("sync_outputs_from_worktrees()", script)
        self.assertIn("sync_outputs_to_worktrees()", script)
        self.assertIn('sync_outputs_from_worktrees 2>/dev/null || true', script)
        self.assertIn('sync_outputs_to_worktrees 2>/dev/null || true', script)
        self.assertIn('"${PROJECT_STATE_DIR}/outputs"/*/*', script)

    def test_reconcile_promotes_accepted_root_deliverables(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("promote_worktree_deliverables()", script)
        self.assertIn("promote_done_worktrees()", script)
        self.assertIn("worktree_deliverable_files()", script)
        self.assertIn('""|.bz|.bz/*|.git|.git/*|.codex|.codex/*', script)
        self.assertIn('cp "$src" "$dest"', script)
        self.assertIn("deliverables_promoted", script)
        self.assertIn("promote_done_worktrees 2>/dev/null || true", script)

    def test_ready_for_review_ignores_stale_decision_files(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()
        review_body = script[
            script.index("check_pending_review() {") : script.index("# Gather an agent's actual work")
        ]

        self.assertIn('if [[ "$state" == "ready-for-review" || "$needs_brain" == "review" ]]', review_body)
        self.assertIn('pending="${pending} ${agent_id}"', review_body)
        self.assertNotIn('[[ ! -f "${agent_dir}/DECISION.md" ]]', review_body)


if __name__ == "__main__":
    unittest.main()
