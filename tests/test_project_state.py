import sys
import tempfile
import unittest
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = REPO_ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import control_plane
import project_init
from state_store import DuckDBStateStore


class ProjectStateTests(unittest.TestCase):
    def write_config(self, root: Path):
        (root / "bz.yaml").write_text(
            "\n".join(
                [
                    "project:",
                    "  name: demo",
                    "  brief: Build demo",
                    "supervisor:",
                    "  runtime: claude",
                    "  model: sonnet",
                    "agents:",
                    "  - id: dev",
                    "    runtime: claude",
                    "    model: sonnet",
                    "    task: Build it",
                    "    focus: [src/]",
                    "git:",
                    "  strategy: worktree",
                    "  auto_pr: false",
                    "",
                ]
            )
        )

    def write_budget_config(self, root: Path):
        (root / "bz.yaml").write_text(
            "\n".join(
                [
                    "project:",
                    "  name: demo",
                    "  brief: Build demo",
                    "supervisor:",
                    "  runtime: claude",
                    "  model: sonnet",
                    "  max_brain_reviews: 1",
                    "  max_agent_restarts: 1",
                    "  max_agent_iterations: 1",
                    "agents:",
                    "  - id: dev",
                    "    runtime: claude",
                    "    model: sonnet",
                    "    task: Build it",
                    "    focus: [src/]",
                    "    max_iterations: 1",
                    "git:",
                    "  strategy: worktree",
                    "  auto_pr: false",
                    "",
                ]
            )
        )

    def load_dashboard_module(self, root: Path):
        server_path = REPO_ROOT / "dashboard" / "server.py"
        old_argv = sys.argv[:]
        try:
            sys.argv = ["server.py", "3333", str(root)]
            spec = importlib.util.spec_from_file_location("dashboard_server_test", server_path)
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
            return module
        finally:
            sys.argv = old_argv

    def test_project_init_creates_canonical_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)

            project_init.initialize_project(root, auto_yes=True)

            self.assertTrue((root / ".bz/project/PROJECT.md").exists())
            self.assertTrue((root / ".bz/project/TARGET.md").exists())
            self.assertTrue((root / ".bz/project/souls/brain_soul.md").exists())
            self.assertTrue((root / ".bz/project/souls/dev_soul.md").exists())
            self.assertTrue((root / ".bz/project/memories/shared_mem.md").exists())
            self.assertTrue((root / ".bz/project/plans/dev_plan.md").exists())
            self.assertTrue((root / ".bz/project/scheduler/policy.yaml").exists())
            self.assertTrue((root / ".bz/project/state.duckdb").exists())
            self.assertIn("zombie_heartbeat_mins: 10", (root / "bz.yaml").read_text())
            self.assertIn("max_brain_reviews: 8", (root / "bz.yaml").read_text())
            self.assertIn("max_agent_iterations: 5", (root / "bz.yaml").read_text())

    def test_agent_iteration_budget_blocks_extra_review_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_budget_config(root)
            project_init.initialize_project(root, auto_yes=True)

            first = control_plane.write_state(
                root,
                agent_id="dev",
                phase="ready-for-review",
                action="requesting review",
                summary="First attempt ready.",
                depends_on=[],
                needs_brain="review",
                next_step="Brain reviews.",
                blocker="none",
                files_touched=["src/app.py"],
                updated_by="agent",
                source="test",
            )
            self.assertEqual(first["phase"], "ready-for-review")

            control_plane.write_state(
                root,
                agent_id="dev",
                phase="working",
                action="addressing feedback",
                summary="Trying again.",
                depends_on=[],
                needs_brain="no",
                next_step="finish retry",
                blocker="none",
                files_touched=["src/app.py"],
                updated_by="agent",
                source="test",
            )
            second = control_plane.write_state(
                root,
                agent_id="dev",
                phase="ready-for-review",
                action="requesting review again",
                summary="Second attempt ready.",
                depends_on=[],
                needs_brain="review",
                next_step="Brain reviews.",
                blocker="none",
                files_touched=["src/app.py"],
                updated_by="agent",
                source="test",
            )

            self.assertEqual(second["phase"], "blocked")
            self.assertEqual(second["needs_brain"], "no")
            self.assertIn("budget exhausted", second["blocker"])
            self.assertIn("max_iterations reached", second["blocker"])

    def test_brain_action_budget_blocks_extra_retry_actions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_budget_config(root)
            project_init.initialize_project(root, auto_yes=True)

            first = control_plane.queue_action(
                root,
                from_actor="brain",
                to_agent="dev",
                kind="redirect",
                summary="Fix the first issue.",
                details="Try again with evidence.",
                reason="Review failed.",
            )
            self.assertEqual(first["kind"], "redirect")

            second = control_plane.queue_action(
                root,
                from_actor="brain",
                to_agent="dev",
                kind="redirect",
                summary="Fix another issue.",
                details="Try again.",
                reason="Review failed again.",
            )

            self.assertEqual(second["kind"], "budget-exhausted")
            state = control_plane.load_state(root, "dev")
            self.assertEqual(state["phase"], "blocked")
            self.assertIn("max_brain_reviews reached", state["blocker"])

    def test_control_plane_writes_state_task_events_and_memory_mirrors(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            state = control_plane.write_state(
                root,
                agent_id="dev",
                phase="working",
                action="building feature",
                summary="Feature started.",
                depends_on=[],
                needs_brain="no",
                next_step="continue implementation",
                blocker="none",
                files_touched=["src/app.py"],
                updated_by="agent",
                source="test",
            )
            self.assertEqual(state["phase"], "working")

            store = DuckDBStateStore(root)
            db_state = store.get_agent_state("dev")
            self.assertIsNotNone(db_state)
            self.assertEqual(db_state["phase"], "working")
            self.assertEqual(db_state["files_touched"], ["src/app.py"])

            events = store.list_task_events("dev")
            self.assertGreaterEqual(len(events), 1)
            self.assertEqual(events[0]["state"], "working")

            control_plane.add_memory(
                root,
                owner="agent:dev",
                scope="shared",
                kind="observation",
                summary="Shared note",
                details="Useful to the whole team.",
            )
            shared = (root / ".bz/project/memories/shared_mem.md").read_text()
            self.assertIn("Shared note", shared)

            context = control_plane.build_agent_context(root, "dev")
            self.assertIn("## Project", context)
            self.assertIn("## Your Soul", context)

    def test_brain_actions_are_recorded_in_agent_chatlog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            control_plane.queue_action(
                root,
                from_actor="brain",
                to_agent="dev",
                kind="unblock",
                summary="Continue with the playable build.",
                details="Start the game loop and verify the controls.",
                reason="User reported the game could not start.",
            )

            chatlog = (root / ".bz/project/chatlogs/brain_dev_chatlog.md").read_text()
            self.assertIn(" - Brain", chatlog)
            self.assertIn("[unblock] Continue with the playable build.", chatlog)
            self.assertIn("Reason: User reported the game could not start.", chatlog)
            self.assertIn("Start the game loop and verify the controls.", chatlog)

    def test_sync_status_preserves_newer_canonical_state_behind_pending_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            control_plane.queue_action(
                root,
                from_actor="brain",
                to_agent="dev",
                kind="accept",
                summary="Accepted rewritten batch.",
                details="Mark the work done.",
                reason="Review passed.",
            )
            control_plane.write_state(
                root,
                agent_id="dev",
                phase="done",
                action="waiting for merge or follow-up",
                summary="Brain accepted the work.",
                depends_on=[],
                needs_brain="no",
                next_step="none",
                blocker="none",
                files_touched=["src/app.py"],
                updated_by="brain",
                source="brain-review",
            )

            old_timestamp = (datetime.now().astimezone() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M")
            (root / ".bz/agents/dev/STATUS.md").write_text(
                "\n".join(
                    [
                        "# STATUS.md",
                        "State: ready-for-review",
                        "Action: waiting for review",
                        "Summary: Old review request.",
                        "Files touched: src/app.py",
                        "Depends on: none",
                        "Needs brain: review",
                        "Next step: Brain review",
                        "Blocker: none",
                        "Memory: .bz/project/memories/dev_mem.md",
                        "Updated by: agent",
                        f"Last updated: {old_timestamp}",
                        "",
                    ]
                )
            )

            state = control_plane.sync_agent_from_status(root, "dev")

            self.assertEqual(state["phase"], "done")
            self.assertEqual(state["needs_brain"], "no")
            self.assertEqual(state["updated_by"], "brain")

    def test_zombie_attention_state_is_recorded_in_agent_chatlog_once(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            status_path = root / ".bz/agents/dev/STATUS.md"
            status_path.write_text(
                "\n".join(
                    [
                        "# STATUS.md",
                        "State: ready-for-review",
                        "Action: waiting for visual review",
                        "Summary: Playable build is ready.",
                        "Files touched: src/game.js",
                        "Depends on: none",
                        "Needs brain: review",
                        "Next step: Brain should verify playability and visual design.",
                        "Blocker: none",
                        "Memory: .bz/project/memories/dev_mem.md",
                        "Updated by: agent",
                        "Last updated: 2026-04-15 12:00",
                        "",
                    ]
                )
            )

            control_plane.sync_agent_from_status(root, "dev")
            control_plane.sync_agent_from_status(root, "dev")

            chatlog = (root / ".bz/project/chatlogs/brain_dev_chatlog.md").read_text()
            self.assertIn(" - dev", chatlog)
            self.assertIn("State: ready-for-review", chatlog)
            self.assertIn("Needs brain: review", chatlog)
            self.assertIn("Brain should verify playability and visual design.", chatlog)
            self.assertEqual(chatlog.count("Needs brain: review"), 1)

    def test_stale_agents_uses_heartbeat_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)
            old = (datetime.now().astimezone() - timedelta(minutes=12)).replace(microsecond=0).isoformat()

            DuckDBStateStore(root).upsert_agent_state(
                {
                    "agent_id": "dev",
                    "phase": "working",
                    "action": "long task",
                    "summary": "No recent heartbeat.",
                    "depends_on": [],
                    "needs_brain": "no",
                    "next_step": "report status",
                    "blocker": "none",
                    "files_touched": [],
                    "updated_at": old,
                    "heartbeat_at": old,
                    "updated_by": "agent",
                    "source": "test",
                }
            )

            stale = DuckDBStateStore(root).stale_agents(10)
            self.assertEqual([row["agent_id"] for row in stale], ["dev"])

    def test_dashboard_status_includes_memory_and_chatlog_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)
            (root / ".bz/project/memories/dev_mem.md").write_text("dev memory body\n")
            (root / ".bz/project/chatlogs/brain_dev_chatlog.md").write_text("brain-dev chat\n")

            module = self.load_dashboard_module(root)
            data = module.build_dashboard_data()

            zombie = data["zombies"][0]
            self.assertEqual(zombie["memory"]["content"], "dev memory body\n")
            self.assertEqual(zombie["chatlog"]["content"], "brain-dev chat\n")
            self.assertIn("shared_mem.md", data["brain"]["shared_memory"]["path"])

    def test_dashboard_active_run_ignores_dashboard_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            module = self.load_dashboard_module(root)
            module.session_names_for_project = lambda project_name: ["bz-demo-dashboard"]
            module.reconcile_running = lambda: False

            self.assertFalse(module.has_active_run(module.read_yaml()))

            module.session_names_for_project = lambda project_name: ["bz-demo-dashboard", "bz-demo-dev"]
            self.assertTrue(module.has_active_run(module.read_yaml()))

    def test_dashboard_teardown_skips_dashboard_session_and_kills_background_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self.write_config(root)
            project_init.initialize_project(root, auto_yes=True)

            module = self.load_dashboard_module(root)
            (root / ".bz/reconcile.pid").write_text("12345\n")

            module.active_session_names_for_project = lambda project_name, config=None: [
                "bz-demo-nerve",
                "bz-demo-dev",
            ]

            killed_processes = []
            killed_sessions = []

            def fake_kill_process_tree(pid: int, sig: int):
                killed_processes.append((pid, sig))
                return [pid]

            def fake_kill_tmux_session_tree(session_name: str):
                killed_sessions.append(session_name)
                return [f"session: {session_name}"]

            module._kill_process_tree = fake_kill_process_tree
            module._kill_tmux_session_tree = fake_kill_tmux_session_tree

            payload = module._handle_teardown()

            self.assertEqual(payload["status"], "ok")
            self.assertIn((12345, module.signal.SIGTERM), killed_processes)
            self.assertIn((12345, module.signal.SIGKILL), killed_processes)
            self.assertEqual(killed_sessions, ["bz-demo-nerve", "bz-demo-dev"])
            self.assertFalse((root / ".bz/reconcile.pid").exists())


if __name__ == "__main__":
    unittest.main()
