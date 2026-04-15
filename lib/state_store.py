#!/usr/bin/env python3
"""DuckDB-backed canonical state for brain-zombies target projects."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from project_layout import ensure_project_layout, project_paths


STATE_SCHEMA_VERSION = 1


def require_duckdb():
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "DuckDB is required for brain-zombies state. Install it with: "
            "python3 -m pip install duckdb"
        ) from exc
    return duckdb


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else [], sort_keys=True)


def _json_loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


class DuckDBStateStore:
    """Small persistence facade around `.bz/project/state.duckdb`."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.paths = project_paths(self.root)

    def connect(self):
        duckdb = require_duckdb()
        ensure_project_layout(self.root)
        return duckdb.connect(str(self.paths.state_db))

    def _write(self, fn: Callable[[Any], Any], attempts: int = 5) -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            con = None
            try:
                con = self.connect()
                con.execute("BEGIN TRANSACTION")
                result = fn(con)
                con.execute("COMMIT")
                return result
            except Exception as exc:  # pragma: no cover - lock timing is environment specific.
                last_error = exc
                try:
                    if con is not None:
                        con.execute("ROLLBACK")
                except Exception:
                    pass
                if "lock" not in str(exc).lower() and "conflict" not in str(exc).lower():
                    raise
                time.sleep(0.1 * (attempt + 1))
            finally:
                if con is not None:
                    con.close()
        assert last_error is not None
        raise last_error

    def initialize(self, agent_ids: Iterable[str] = ()) -> None:
        ensure_project_layout(self.root, agent_ids)

        def op(con):
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR,
                    updated_at VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id VARCHAR PRIMARY KEY,
                    role VARCHAR,
                    runtime VARCHAR,
                    model VARCHAR,
                    soul_path VARCHAR,
                    memory_path VARCHAR,
                    plan_path VARCHAR,
                    output_path VARCHAR,
                    current_phase VARCHAR,
                    current_action VARCHAR,
                    summary VARCHAR,
                    depends_on_json VARCHAR,
                    needs_brain VARCHAR,
                    next_step VARCHAR,
                    blocker VARCHAR,
                    files_touched_json VARCHAR,
                    updated_at VARCHAR,
                    updated_by VARCHAR,
                    source VARCHAR,
                    heartbeat_at VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id VARCHAR PRIMARY KEY,
                    timestamp VARCHAR,
                    zombie_name VARCHAR,
                    task VARCHAR,
                    sub_task VARCHAR,
                    state VARCHAR,
                    notes VARCHAR,
                    source VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    id VARCHAR PRIMARY KEY,
                    schema_version INTEGER,
                    created_at VARCHAR,
                    from_actor VARCHAR,
                    to_agent VARCHAR,
                    kind VARCHAR,
                    status VARCHAR,
                    summary VARCHAR,
                    details VARCHAR,
                    reason VARCHAR,
                    closed_at VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id VARCHAR PRIMARY KEY,
                    schema_version INTEGER,
                    created_at VARCHAR,
                    owner VARCHAR,
                    scope VARCHAR,
                    kind VARCHAR,
                    summary VARCHAR,
                    details VARCHAR,
                    tags_json VARCHAR,
                    related_agents_json VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id VARCHAR PRIMARY KEY,
                    schema_version INTEGER,
                    timestamp VARCHAR,
                    type VARCHAR,
                    source VARCHAR,
                    target VARCHAR,
                    summary VARCHAR,
                    details VARCHAR,
                    payload_json VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id VARCHAR PRIMARY KEY,
                    timestamp VARCHAR,
                    from_actor VARCHAR,
                    to_actor VARCHAR,
                    message VARCHAR,
                    source VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id VARCHAR PRIMARY KEY,
                    timestamp VARCHAR,
                    owner VARCHAR,
                    kind VARCHAR,
                    path VARCHAR,
                    summary VARCHAR
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduler_checks (
                    id VARCHAR PRIMARY KEY,
                    timestamp VARCHAR,
                    agent_id VARCHAR,
                    check_type VARCHAR,
                    result VARCHAR,
                    notes VARCHAR
                )
                """
            )
            con.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?, ?)",
                ["schema_version", str(STATE_SCHEMA_VERSION), now_iso()],
            )
            for agent_id in agent_ids:
                self._insert_agent_if_missing(con, agent_id)

        self._write(op)

    def _insert_agent_if_missing(self, con, agent_id: str) -> None:
        existing = con.execute(
            "SELECT agent_id FROM agents WHERE agent_id = ?", [agent_id]
        ).fetchone()
        if existing:
            return
        now = now_iso()
        role = "brain" if agent_id == "supervisor" else "agent"
        con.execute(
            """
            INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                agent_id,
                role,
                "",
                "",
                "",
                "",
                "",
                "",
                "unknown",
                "waiting for next step",
                "No summary.",
                _json_dumps([]),
                "no",
                "none",
                "none",
                _json_dumps([]),
                now,
                "system",
                "init",
                now,
            ],
        )

    def upsert_agent_state(self, state: dict[str, Any]) -> dict[str, Any]:
        agent_id = state["agent_id"]
        self.initialize([agent_id])
        existing = self.get_agent_state(agent_id) or {}

        def op(con):
            self._insert_agent_if_missing(con, agent_id)
            row = {
                "agent_id": agent_id,
                "role": state.get("role") or existing.get("role") or ("brain" if agent_id == "supervisor" else "agent"),
                "runtime": state.get("runtime") or existing.get("runtime") or "",
                "model": state.get("model") or existing.get("model") or "",
                "soul_path": state.get("soul_path") or existing.get("soul_path") or "",
                "memory_path": state.get("memory_path") or existing.get("memory_path") or "",
                "plan_path": state.get("plan_path") or existing.get("plan_path") or "",
                "output_path": state.get("output_path") or existing.get("output_path") or "",
                "current_phase": state.get("phase") or state.get("current_phase") or existing.get("phase") or "unknown",
                "current_action": state.get("action") or state.get("current_action") or existing.get("action") or "waiting for next step",
                "summary": state.get("summary") or existing.get("summary") or "No summary.",
                "depends_on_json": _json_dumps(state.get("depends_on", existing.get("depends_on", []))),
                "needs_brain": state.get("needs_brain") or existing.get("needs_brain") or "no",
                "next_step": state.get("next_step") or existing.get("next_step") or "none",
                "blocker": state.get("blocker") or existing.get("blocker") or "none",
                "files_touched_json": _json_dumps(state.get("files_touched", existing.get("files_touched", []))),
                "updated_at": state.get("updated_at") or now_iso(),
                "updated_by": state.get("updated_by") or existing.get("updated_by") or "system",
                "source": state.get("source") or existing.get("source") or "control-plane",
                "heartbeat_at": state.get("heartbeat_at") or state.get("updated_at") or now_iso(),
            }
            con.execute("DELETE FROM agents WHERE agent_id = ?", [agent_id])
            con.execute(
                """
                INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["agent_id"],
                    row["role"],
                    row["runtime"],
                    row["model"],
                    row["soul_path"],
                    row["memory_path"],
                    row["plan_path"],
                    row["output_path"],
                    row["current_phase"],
                    row["current_action"],
                    row["summary"],
                    row["depends_on_json"],
                    row["needs_brain"],
                    row["next_step"],
                    row["blocker"],
                    row["files_touched_json"],
                    row["updated_at"],
                    row["updated_by"],
                    row["source"],
                    row["heartbeat_at"],
                ],
            )

        self._write(op)
        return self.get_agent_state(agent_id) or {}

    def get_agent_state(self, agent_id: str) -> dict[str, Any] | None:
        self.initialize([])
        con = self.connect()
        try:
            row = con.execute(
                """
                SELECT agent_id, role, runtime, model, soul_path, memory_path, plan_path, output_path,
                       current_phase, current_action, summary, depends_on_json, needs_brain,
                       next_step, blocker, files_touched_json, updated_at, updated_by, source, heartbeat_at
                FROM agents WHERE agent_id = ?
                """,
                [agent_id],
            ).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return {
            "agent_id": row[0],
            "role": row[1],
            "runtime": row[2],
            "model": row[3],
            "soul_path": row[4],
            "memory_path": row[5],
            "plan_path": row[6],
            "output_path": row[7],
            "phase": row[8],
            "action": row[9],
            "summary": row[10],
            "depends_on": _json_loads(row[11], []),
            "needs_brain": row[12],
            "next_step": row[13],
            "blocker": row[14],
            "files_touched": _json_loads(row[15], []),
            "updated_at": row[16],
            "updated_by": row[17],
            "source": row[18],
            "heartbeat_at": row[19],
        }

    def list_agent_ids(self) -> list[str]:
        self.initialize([])
        con = self.connect()
        try:
            rows = con.execute("SELECT agent_id FROM agents ORDER BY agent_id").fetchall()
        finally:
            con.close()
        return [row[0] for row in rows]

    def add_task_event(
        self,
        zombie_name: str,
        task: str,
        sub_task: str,
        state: str,
        notes: str = "",
        source: str = "control-plane",
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        self.initialize([zombie_name])
        event = {
            "id": f"task-{uuid.uuid4().hex[:12]}",
            "timestamp": timestamp or now_iso(),
            "zombie_name": _flatten(zombie_name),
            "task": _flatten(task),
            "sub_task": _flatten(sub_task),
            "state": _flatten(state),
            "notes": str(notes or "").strip(),
            "source": _flatten(source) or "control-plane",
        }

        def op(con):
            con.execute(
                "INSERT INTO task_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    event["id"],
                    event["timestamp"],
                    event["zombie_name"],
                    event["task"],
                    event["sub_task"],
                    event["state"],
                    event["notes"],
                    event["source"],
                ],
            )

        self._write(op)
        return event

    def list_task_events(self, zombie_name: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        self.initialize([])
        con = self.connect()
        try:
            params: list[Any] = []
            where = ""
            if zombie_name:
                where = "WHERE zombie_name = ?"
                params.append(zombie_name)
            limit_sql = f"LIMIT {int(limit)}" if limit else ""
            rows = con.execute(
                f"""
                SELECT id, timestamp, zombie_name, task, sub_task, state, notes, source
                FROM task_events {where}
                ORDER BY timestamp DESC {limit_sql}
                """,
                params,
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "id": row[0],
                "timestamp": row[1],
                "zombie_name": row[2],
                "task": row[3],
                "sub_task": row[4],
                "state": row[5],
                "notes": row[6],
                "source": row[7],
            }
            for row in rows
        ]

    def supersede_pending_actions(self, to_agent: str) -> None:
        self.initialize([to_agent])

        def op(con):
            con.execute(
                """
                UPDATE actions
                SET status = 'superseded', closed_at = ?
                WHERE to_agent = ? AND status IN ('pending', 'delivered', 'acknowledged')
                """,
                [now_iso(), to_agent],
            )

        self._write(op)

    def add_action(self, action: dict[str, Any]) -> dict[str, Any]:
        self.initialize([action["to"]])

        def op(con):
            con.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    action["id"],
                    int(action.get("schema_version", 1)),
                    action["created_at"],
                    action["from"],
                    action["to"],
                    action["kind"],
                    action["status"],
                    action["summary"],
                    action.get("details", ""),
                    action.get("reason", ""),
                    action.get("closed_at", ""),
                ],
            )

        self._write(op)
        return action

    def load_actions(self, agent_id: str) -> list[dict[str, Any]]:
        self.initialize([agent_id])
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT id, schema_version, created_at, from_actor, to_agent, kind, status,
                       summary, details, reason, closed_at
                FROM actions
                WHERE to_agent = ?
                ORDER BY created_at
                """,
                [agent_id],
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "id": row[0],
                "schema_version": row[1],
                "created_at": row[2],
                "from": row[3],
                "to": row[4],
                "kind": row[5],
                "status": row[6],
                "summary": row[7],
                "details": row[8],
                "reason": row[9],
                "closed_at": row[10],
            }
            for row in rows
        ]

    def add_memory(self, memory: dict[str, Any]) -> dict[str, Any]:
        self.initialize([])

        def op(con):
            con.execute(
                "INSERT INTO memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    memory["id"],
                    int(memory.get("schema_version", 1)),
                    memory["created_at"],
                    memory["owner"],
                    memory["scope"],
                    memory["kind"],
                    memory["summary"],
                    memory.get("details", ""),
                    _json_dumps(memory.get("tags", [])),
                    _json_dumps(memory.get("related_agents", [])),
                ],
            )

        self._write(op)
        return memory

    def load_memories(self) -> list[dict[str, Any]]:
        self.initialize([])
        con = self.connect()
        try:
            rows = con.execute(
                """
                SELECT id, schema_version, created_at, owner, scope, kind, summary,
                       details, tags_json, related_agents_json
                FROM memories
                ORDER BY created_at
                """
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "id": row[0],
                "schema_version": row[1],
                "created_at": row[2],
                "owner": row[3],
                "scope": row[4],
                "kind": row[5],
                "summary": row[6],
                "details": row[7],
                "tags": _json_loads(row[8], []),
                "related_agents": _json_loads(row[9], []),
            }
            for row in rows
        ]

    def add_event(self, event: dict[str, Any]) -> dict[str, Any]:
        self.initialize([])

        def op(con):
            con.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    event["id"],
                    int(event.get("schema_version", 1)),
                    event["timestamp"],
                    event["type"],
                    event["source"],
                    event.get("target", ""),
                    event.get("summary", ""),
                    event.get("details", ""),
                    _json_dumps(event.get("payload", {})),
                ],
            )

        self._write(op)
        return event

    def load_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        self.initialize([])
        con = self.connect()
        try:
            limit_sql = f"LIMIT {int(limit)}" if limit else ""
            rows = con.execute(
                f"""
                SELECT id, schema_version, timestamp, type, source, target, summary, details, payload_json
                FROM events
                ORDER BY timestamp DESC {limit_sql}
                """
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "id": row[0],
                "schema_version": row[1],
                "timestamp": row[2],
                "type": row[3],
                "source": row[4],
                "target": row[5],
                "summary": row[6],
                "details": row[7],
                "payload": _json_loads(row[8], {}),
            }
            for row in reversed(rows)
        ]

    def add_scheduler_check(self, agent_id: str, check_type: str, result: str, notes: str = "") -> dict[str, Any]:
        self.initialize([agent_id])
        row = {
            "id": f"sched-{uuid.uuid4().hex[:12]}",
            "timestamp": now_iso(),
            "agent_id": agent_id,
            "check_type": _flatten(check_type),
            "result": _flatten(result),
            "notes": str(notes or "").strip(),
        }

        def op(con):
            con.execute(
                "INSERT INTO scheduler_checks VALUES (?, ?, ?, ?, ?, ?)",
                [row["id"], row["timestamp"], row["agent_id"], row["check_type"], row["result"], row["notes"]],
            )

        self._write(op)
        return row

    def stale_agents(self, heartbeat_mins: int) -> list[dict[str, Any]]:
        states = [self.get_agent_state(agent_id) for agent_id in self.list_agent_ids()]
        active_phases = {"starting", "planning", "working", "executing", "testing", "iterating"}
        stale: list[dict[str, Any]] = []
        threshold = int(heartbeat_mins)
        for state in states:
            if not state or state.get("agent_id") == "supervisor":
                continue
            if state.get("phase") not in active_phases:
                continue
            stamp = state.get("heartbeat_at") or state.get("updated_at")
            try:
                age = datetime.now().astimezone() - datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                age_minutes = threshold
            else:
                age_minutes = max(0, int(age.total_seconds() // 60))
            if age_minutes >= threshold:
                enriched = dict(state)
                enriched["age_minutes"] = age_minutes
                stale.append(enriched)
        return stale
