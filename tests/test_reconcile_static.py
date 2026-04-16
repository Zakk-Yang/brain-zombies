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
        self.assertIn('decision_fresh_for_status "$status_file" "$decision_file"', review_body)
        self.assertIn('pending="${pending} ${agent_id}"', review_body)
        self.assertNotIn('[[ ! -f "${agent_dir}/DECISION.md" ]]', review_body)

    def test_reconcile_releases_satisfied_dependencies_in_main_loop(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()
        release_body = script[
            script.index("release_satisfied_dependencies() {") : script.index("# ── Wake Triggers")
        ]

        self.assertIn("release_satisfied_dependencies()", script)
        self.assertIn('dependencies_satisfied "$depends" || continue', release_body)
        self.assertIn('--kind unblock', release_body)
        self.assertIn('--phase working', release_body)
        self.assertIn('deliver_action_to_agent "$agent_id"', release_body)
        self.assertNotIn('pending_action', release_body)
        self.assertIn('deliver_action_to_agent "$agent_id" >&2', release_body)
        self.assertIn('if released="$(release_satisfied_dependencies)"; then', script)

    def test_review_prompt_forbids_tool_use(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn(
            "- use only the supplied context; do not run tools, inspect git, or execute commands",
            script,
        )

    def test_crash_check_only_applies_to_active_phases(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()
        crash_body = script[
            script.index("check_zombie_alive() {") : script.index("# Check 3: active zombie missed heartbeat")
        ]

        self.assertIn("phase_expects_live_session()", script)
        self.assertIn('phase_expects_live_session "$state" || continue', crash_body)
        self.assertNotIn('[[ "$state" == "done" || "$state" == "finished" ]]', crash_body)

    def test_brain_error_circuit_breaker_suppresses_repeated_failures(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("BRAIN_ERROR_THRESHOLD=3", script)
        self.assertIn("BRAIN_ERROR_SUPPRESS_WINDOW=1800", script)
        self.assertIn("brain_trigger_key()", script)
        self.assertIn("brain_trigger_suppressed()", script)
        self.assertIn("brain_trigger_record_failure()", script)
        self.assertIn("brain_trigger_clear()", script)
        self.assertIn('if [[ -n "$trigger_key" ]] && brain_trigger_suppressed "$trigger_key"; then', script)
        self.assertIn('Suppressing brain wake for mode=${mode} reason=${reason}', script)
        self.assertIn('brain_trigger_record_failure "$trigger_key"', script)
        self.assertIn('brain_trigger_clear "$trigger_key"', script)

    def test_reconcile_enforces_total_runtime_budget_and_gates_proactive_wakes(self):
        script = (REPO_ROOT / "lib" / "reconcile.sh").read_text()

        self.assertIn("total_minutes_budget_reason()", script)
        self.assertIn("shutdown_budget_exhausted_run()", script)
        self.assertIn('if budget_reason="$(total_minutes_budget_reason 2>/dev/null)"; then', script)
        self.assertIn('shutdown_budget_exhausted_run "$budget_reason"', script)
        self.assertIn("has_actionable_proactive_targets()", script)
        self.assertIn('if proactive_targets="$(has_actionable_proactive_targets 2>/dev/null)"; then', script)
        self.assertIn('Skipping proactive brain wake; no actionable targets remain.', script)


if __name__ == "__main__":
    unittest.main()
