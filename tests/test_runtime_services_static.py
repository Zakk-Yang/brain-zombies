from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class RuntimeServicesStaticTests(unittest.TestCase):
    def test_launcher_uses_tmux_for_persistent_services(self):
        script = (REPO_ROOT / "bz").read_text()

        self.assertIn('start_reconcile_loop "${project_root_abs}"', script)
        self.assertIn("cmd_dashboard", script)
        self.assertIn('session_name "nerve"', script)
        self.assertIn('session_name "dashboard"', script)
        self.assertIn("tmux new-session -d -s \"$reconcile_session\"", script)
        self.assertIn("tmux new-session -d -s \"$dashboard_session\"", script)
        self.assertIn('if [[ "$auto_dashboard" -eq 1 ]]; then', script)
        self.assertNotIn('nohup bash "${SCRIPT_DIR}/lib/reconcile.sh"', script)
        self.assertNotIn('nohup python3 "${SCRIPT_DIR}/dashboard/server.py"', script)
        self.assertNotIn("Start dashboard? [Y/n]", script)

    def test_dashboard_restarts_reconcile_under_tmux(self):
        server = (REPO_ROOT / "dashboard" / "server.py").read_text()

        self.assertIn('service_session_name("nerve")', server)
        self.assertIn('"tmux", "new-session", "-d", "-s", session', server)
        self.assertNotIn("subprocess.Popen(\n        [\"bash\", str(LIB_DIR / \"reconcile.sh\")", server)

    def test_dashboard_preserves_message_log_scroll_on_refresh(self):
        index = (REPO_ROOT / "dashboard" / "index.html").read_text()

        self.assertIn('id="message-log-panel"', index)
        self.assertIn("captureScrollState()", index)
        self.assertIn("restoreScrollState(scrollState)", index)
        self.assertIn("state.logPinnedToBottom", index)

    def test_dashboard_does_not_treat_ready_for_review_as_finished(self):
        server = (REPO_ROOT / "dashboard" / "server.py").read_text()
        phase_body = server[server.index("def get_phase(") : server.index("def get_token_usage")]

        self.assertIn('if state == "ready-for-review":', phase_body)
        self.assertIn('return "ready-for-review"', phase_body)
        self.assertNotIn('"proceed", "unblock"', phase_body)


if __name__ == "__main__":
    unittest.main()
