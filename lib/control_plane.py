#!/usr/bin/env python3
"""Structured control plane for brain-zombies.

This module keeps a canonical record of:
- agent state
- pending actions from brain/human/system
- durable memories for brain and agents
- compact context snapshots for brain and each agent

STATUS.md remains a compatibility artifact generated from the canonical state.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from project_layout import (
    agent_memory_path as project_agent_memory_path,
    agent_output_dir,
    agent_plan_path,
    agent_soul_path,
    brain_memory_path as project_brain_memory_path,
    brain_output_dir,
    brain_soul_path,
    ensure_project_layout,
    project_paths,
    rel_to_root,
    shared_memory_path,
)
from state_store import DuckDBStateStore


STATE_VERSION = 1
ACTION_VERSION = 1
MEMORY_VERSION = 1
EVENT_VERSION = 1

PHASE_ALIASES = {
    "starting": "starting",
    "monitoring": "monitoring",
    "planning": "planning",
    "working": "working",
    "coding": "working",
    "executing": "executing",
    "running": "executing",
    "testing": "testing",
    "blocked": "blocked",
    "review": "ready-for-review",
    "ready-for-review": "ready-for-review",
    "done": "done",
    "finished": "done",
    "crashed": "crashed",
    "iterating": "iterating",
    "unknown": "unknown",
}


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def now_display() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def parse_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).astimezone().replace(microsecond=0).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).astimezone().replace(microsecond=0).isoformat()
    except ValueError:
        return None


def iso_to_display(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def normalize_phase(value: str | None) -> str:
    if not value:
        return "unknown"
    return PHASE_ALIASES.get(value.strip().lower(), value.strip().lower())


def flatten_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(str(value).strip().split())


def parse_csv(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [item.strip() for item in value.split(",")]
    return [item for item in parts if item and item.lower() != "none"]


def csv_or_none(values: Iterable[str]) -> str:
    items = [flatten_text(v) for v in values if flatten_text(v)]
    return ", ".join(items) if items else "none"


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


def write_json(path: Path, payload: Any) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_parent(path)
    lines = [json.dumps(row, sort_keys=True) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""))


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    rows = read_jsonl(path)
    rows.append(payload)
    write_jsonl(path, rows)


def project_root_path(value: str | Path) -> Path:
    return Path(value).resolve()


def bz_dir(root: Path) -> Path:
    return project_paths(root).bz_dir


def control_dir(root: Path) -> Path:
    return project_paths(root).control_dir


def control_agents_dir(root: Path) -> Path:
    return project_paths(root).control_agents_dir


def control_memories_dir(root: Path) -> Path:
    return project_paths(root).control_memories_dir


def control_contexts_dir(root: Path) -> Path:
    return project_paths(root).control_contexts_dir


def events_path(root: Path) -> Path:
    return control_dir(root) / "events.jsonl"


def agent_state_path(root: Path, agent_id: str) -> Path:
    return control_agents_dir(root) / agent_id / "state.json"


def agent_actions_path(root: Path, agent_id: str) -> Path:
    return control_agents_dir(root) / agent_id / "actions.jsonl"


def latest_action_path(root: Path, agent_id: str) -> Path:
    return control_agents_dir(root) / agent_id / "latest-action.md"


def agent_status_path(root: Path, agent_id: str) -> Path:
    return bz_dir(root) / "agents" / agent_id / "STATUS.md"


def agent_decision_path(root: Path, agent_id: str) -> Path:
    return bz_dir(root) / "agents" / agent_id / "DECISION.md"


def worktree_root(root: Path, agent_id: str) -> Path | None:
    path = bz_dir(root) / "worktrees" / agent_id
    return path if path.exists() else None


def worktree_mirror_path(root: Path, agent_id: str, rel_path: str) -> Path | None:
    wt = worktree_root(root, agent_id)
    if wt is None:
        return None
    return wt / rel_path


def memory_path(root: Path, owner: str) -> Path:
    key = owner.strip()
    if key == "brain":
        return control_memories_dir(root) / "brain.jsonl"
    if key.startswith("agent:"):
        return control_memories_dir(root) / f"{key.split(':', 1)[1]}.jsonl"
    return control_memories_dir(root) / f"{key}.jsonl"


def memory_markdown_path(root: Path, owner: str) -> Path:
    key = owner.strip()
    if key in {"brain", "supervisor"}:
        return project_brain_memory_path(root)
    if key.startswith("agent:"):
        return project_agent_memory_path(root, key.split(":", 1)[1])
    return project_agent_memory_path(root, key)


def memory_markdown_ref(root: Path, owner: str) -> str:
    return rel_to_root(root, memory_markdown_path(root, owner))


def context_path(root: Path, viewer: str) -> Path:
    if viewer == "brain":
        return control_contexts_dir(root) / "brain.md"
    if viewer.startswith("agent:"):
        return control_contexts_dir(root) / f"{viewer.split(':', 1)[1]}.md"
    return control_contexts_dir(root) / f"{viewer}.md"


def ensure_layout(root: Path, agent_ids: Iterable[str] = ()) -> None:
    ensure_project_layout(root, agent_ids)
    DuckDBStateStore(root).initialize(agent_ids)


def list_agent_ids(root: Path) -> list[str]:
    ids: set[str] = set()
    agents_root = bz_dir(root) / "agents"
    if agents_root.exists():
        ids.update(path.name for path in agents_root.iterdir() if path.is_dir())
    control_root = control_agents_dir(root)
    if control_root.exists():
        ids.update(path.name for path in control_root.iterdir() if path.is_dir())
    try:
        ids.update(DuckDBStateStore(root).list_agent_ids())
    except Exception:
        pass
    return sorted(ids)


def read_status_markdown(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    if not path.exists():
        return fields
    for line in path.read_text().splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def infer_action(fields: dict[str, str]) -> str:
    return (
        flatten_text(fields.get("action"))
        or flatten_text(fields.get("next step"))
        or flatten_text(fields.get("summary"))
        or "waiting for next step"
    )


def status_markdown_from_state(root: Path, state: dict[str, Any]) -> str:
    files_touched = state.get("files_touched", [])
    agent_id = state.get("agent_id", "")
    memory_ref = memory_markdown_ref(root, "brain" if agent_id == "supervisor" else f"agent:{agent_id}")
    lines = [
        "# STATUS.md",
        f"State: {state.get('phase', 'unknown')}",
        f"Action: {state.get('action', 'waiting for next step')}",
        f"Summary: {state.get('summary', '') or 'No summary.'}",
        f"Files touched: {csv_or_none(files_touched)}",
        f"Depends on: {csv_or_none(state.get('depends_on', []))}",
        f"Needs brain: {state.get('needs_brain', 'no') or 'no'}",
        f"Next step: {state.get('next_step', '') or 'none'}",
        f"Blocker: {state.get('blocker', 'none') or 'none'}",
        f"Memory: {memory_ref}",
        f"Updated by: {state.get('updated_by', 'system')}",
        f"Last updated: {iso_to_display(state.get('updated_at')) or now_display()}",
    ]
    return "\n".join(lines) + "\n"


def write_text_and_mirror(root: Path, agent_id: str, rel_path: str, content: str) -> None:
    root_path = root / rel_path
    ensure_parent(root_path)
    root_path.write_text(content)
    mirror_path = worktree_mirror_path(root, agent_id, rel_path)
    if mirror_path is not None:
        ensure_parent(mirror_path)
        mirror_path.write_text(content)


def append_event(
    root: Path,
    event_type: str,
    source: str,
    target: str = "",
    summary: str = "",
    details: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    event = {
        "id": f"evt-{uuid.uuid4().hex[:12]}",
        "schema_version": EVENT_VERSION,
        "timestamp": now_iso(),
        "type": event_type,
        "source": source,
        "target": target,
        "summary": flatten_text(summary),
        "details": flatten_text(details),
        "payload": payload or {},
    }
    DuckDBStateStore(root).add_event(event)
    append_jsonl(events_path(root), event)


def load_state(root: Path, agent_id: str) -> dict[str, Any]:
    try:
        state = DuckDBStateStore(root).get_agent_state(agent_id)
        if state:
            return state
    except Exception:
        pass
    return read_json(agent_state_path(root, agent_id), {})


def enrich_state_paths(root: Path, state: dict[str, Any]) -> None:
    agent_id = state.get("agent_id", "")
    if not agent_id:
        return
    if agent_id == "supervisor":
        state.setdefault("soul_path", rel_to_root(root, brain_soul_path(root)))
        state.setdefault("memory_path", rel_to_root(root, project_brain_memory_path(root)))
        state.setdefault("plan_path", "")
        state.setdefault("output_path", rel_to_root(root, brain_output_dir(root)))
        return
    state.setdefault("soul_path", rel_to_root(root, agent_soul_path(root, agent_id)))
    state.setdefault("memory_path", rel_to_root(root, project_agent_memory_path(root, agent_id)))
    state.setdefault("plan_path", rel_to_root(root, agent_plan_path(root, agent_id)))
    state.setdefault("output_path", rel_to_root(root, agent_output_dir(root, agent_id)))


def save_state(root: Path, agent_id: str, state: dict[str, Any]) -> dict[str, Any]:
    ensure_layout(root, [agent_id])
    state["schema_version"] = STATE_VERSION
    state["agent_id"] = agent_id
    enrich_state_paths(root, state)
    DuckDBStateStore(root).upsert_agent_state(state)
    write_json(agent_state_path(root, agent_id), state)
    status_text = status_markdown_from_state(root, state)
    write_text_and_mirror(root, agent_id, f".bz/agents/{agent_id}/STATUS.md", status_text)
    return state


def sync_agent_from_status(root: Path, agent_id: str) -> dict[str, Any]:
    path = agent_status_path(root, agent_id)
    if not path.exists():
        return load_state(root, agent_id)
    fields = read_status_markdown(path)
    existing = load_state(root, agent_id)
    parsed_updated = parse_timestamp(fields.get("last updated")) or datetime.fromtimestamp(path.stat().st_mtime).astimezone().replace(microsecond=0).isoformat()
    state = {
        "agent_id": agent_id,
        "role": "brain" if agent_id == "supervisor" else "agent",
        "phase": normalize_phase(fields.get("state") or existing.get("phase")),
        "action": infer_action(fields),
        "summary": flatten_text(fields.get("summary")) or existing.get("summary", ""),
        "depends_on": parse_csv(fields.get("depends on")) or existing.get("depends_on", []),
        "needs_brain": flatten_text(fields.get("needs brain")) or existing.get("needs_brain", "no") or "no",
        "next_step": flatten_text(fields.get("next step")) or existing.get("next_step", ""),
        "blocker": flatten_text(fields.get("blocker")) or existing.get("blocker", "none") or "none",
        "files_touched": parse_csv(fields.get("files touched")) or existing.get("files_touched", []),
        "updated_at": parsed_updated,
        "updated_by": flatten_text(fields.get("updated by")) or existing.get("updated_by", "agent"),
        "source": "status-md",
        "reported_last_updated": flatten_text(fields.get("last updated")),
    }
    state["action"] = state["action"] or "waiting for next step"
    state["summary"] = state["summary"] or "No summary."
    save_state(root, agent_id, state)
    return state


def sync_all(root: Path) -> None:
    ensure_layout(root)
    for agent_id in list_agent_ids(root):
        if (bz_dir(root) / "agents" / agent_id / "STATUS.md").exists():
            sync_agent_from_status(root, agent_id)
    refresh_contexts(root)


def write_state(
    root: Path,
    agent_id: str,
    phase: str | None = None,
    action: str | None = None,
    summary: str | None = None,
    depends_on: list[str] | None = None,
    needs_brain: str | None = None,
    next_step: str | None = None,
    blocker: str | None = None,
    files_touched: list[str] | None = None,
    updated_by: str = "system",
    source: str = "control-plane",
) -> dict[str, Any]:
    existing = load_state(root, agent_id)
    state = {
        "agent_id": agent_id,
        "role": "brain" if agent_id == "supervisor" else "agent",
        "phase": normalize_phase(phase or existing.get("phase") or "unknown"),
        "action": flatten_text(action) or existing.get("action") or "waiting for next step",
        "summary": flatten_text(summary) or existing.get("summary") or "No summary.",
        "depends_on": depends_on if depends_on is not None else existing.get("depends_on", []),
        "needs_brain": flatten_text(needs_brain) or existing.get("needs_brain") or "no",
        "next_step": flatten_text(next_step) or existing.get("next_step") or "none",
        "blocker": flatten_text(blocker) or existing.get("blocker") or "none",
        "files_touched": files_touched if files_touched is not None else existing.get("files_touched", []),
        "updated_at": now_iso(),
        "updated_by": updated_by,
        "source": source,
    }
    save_state(root, agent_id, state)
    append_event(
        root,
        event_type="state_changed",
        source=updated_by,
        target=f"agent:{agent_id}",
        summary=f"{agent_id} -> {state['phase']}",
        details=f"{state['action']} | {state['summary']}",
        payload={
            "phase": state["phase"],
            "action": state["action"],
            "summary": state["summary"],
            "depends_on": state["depends_on"],
            "needs_brain": state["needs_brain"],
            "blocker": state["blocker"],
        },
    )
    if agent_id != "supervisor":
        DuckDBStateStore(root).add_task_event(
            zombie_name=agent_id,
            task=state["action"],
            sub_task=state["next_step"],
            state=state["phase"],
            notes=state["summary"],
            source=source,
            timestamp=state["updated_at"],
        )
    refresh_contexts(root)
    return state


def load_actions(root: Path, agent_id: str) -> list[dict[str, Any]]:
    try:
        actions = DuckDBStateStore(root).load_actions(agent_id)
        if actions:
            return actions
    except Exception:
        pass
    return read_jsonl(agent_actions_path(root, agent_id))


def latest_pending_action(root: Path, agent_id: str) -> dict[str, Any] | None:
    actions = load_actions(root, agent_id)
    for action in reversed(actions):
        if action.get("status") in {"pending", "delivered", "acknowledged"}:
            return action
    return None


def pending_targets(root: Path) -> list[str]:
    targets: list[str] = []
    for agent_id in list_agent_ids(root):
        if agent_id == "supervisor":
            continue
        if latest_pending_action(root, agent_id):
            targets.append(agent_id)
    return targets


def render_action_markdown(action: dict[str, Any]) -> str:
    lines = [
        f"# Action {action.get('id', '')}",
        f"From: {action.get('from', '')}",
        f"To: {action.get('to', '')}",
        f"Kind: {action.get('kind', '')}",
        f"Status: {action.get('status', '')}",
        f"Summary: {action.get('summary', '')}",
        f"Reason: {action.get('reason', '') or 'none'}",
        "",
        action.get("details", "") or "(no details)",
        "",
        f"Created: {iso_to_display(action.get('created_at'))}",
    ]
    return "\n".join(lines).strip() + "\n"


def queue_action(
    root: Path,
    from_actor: str,
    to_agent: str,
    kind: str,
    summary: str,
    details: str = "",
    reason: str = "",
    replace_pending: bool = True,
) -> dict[str, Any]:
    ensure_layout(root, [to_agent])
    actions = load_actions(root, to_agent)
    if replace_pending:
        DuckDBStateStore(root).supersede_pending_actions(to_agent)
        updated = False
        for action in actions:
            if action.get("status") in {"pending", "delivered", "acknowledged"}:
                action["status"] = "superseded"
                action["closed_at"] = now_iso()
                updated = True
        if updated:
            write_jsonl(agent_actions_path(root, to_agent), actions)
    action = {
        "id": f"act-{uuid.uuid4().hex[:12]}",
        "schema_version": ACTION_VERSION,
        "created_at": now_iso(),
        "from": from_actor,
        "to": to_agent,
        "kind": flatten_text(kind) or "guidance",
        "status": "pending",
        "summary": flatten_text(summary),
        "details": details.strip(),
        "reason": flatten_text(reason),
    }
    actions.append(action)
    DuckDBStateStore(root).add_action(action)
    write_jsonl(agent_actions_path(root, to_agent), actions)
    action_markdown = render_action_markdown(action)
    write_text_and_mirror(root, to_agent, f".bz/control/agents/{to_agent}/latest-action.md", action_markdown)
    if from_actor == "brain":
        write_text_and_mirror(root, to_agent, f".bz/agents/{to_agent}/DECISION.md", action_markdown)
    append_event(
        root,
        event_type="action_queued",
        source=from_actor,
        target=f"agent:{to_agent}",
        summary=f"{kind}: {summary}",
        details=details,
        payload={"kind": kind, "action_id": action["id"]},
    )
    refresh_contexts(root)
    return action


def add_memory(
    root: Path,
    owner: str,
    scope: str,
    kind: str,
    summary: str,
    details: str = "",
    tags: list[str] | None = None,
    related_agents: list[str] | None = None,
) -> dict[str, Any]:
    ensure_layout(root)
    entry = {
        "id": f"mem-{uuid.uuid4().hex[:12]}",
        "schema_version": MEMORY_VERSION,
        "created_at": now_iso(),
        "owner": owner,
        "scope": flatten_text(scope) or "private",
        "kind": flatten_text(kind) or "note",
        "summary": flatten_text(summary),
        "details": details.strip(),
        "tags": [flatten_text(tag) for tag in (tags or []) if flatten_text(tag)],
        "related_agents": [flatten_text(agent) for agent in (related_agents or []) if flatten_text(agent)],
    }
    DuckDBStateStore(root).add_memory(entry)
    append_jsonl(memory_path(root, owner), entry)
    append_event(
        root,
        event_type="memory_added",
        source=owner,
        summary=entry["summary"],
        details=entry["details"],
        payload={"kind": entry["kind"], "scope": entry["scope"], "memory_id": entry["id"]},
    )
    render_memory_mirrors(root)
    refresh_contexts(root)
    return entry


def load_all_memories(root: Path) -> list[dict[str, Any]]:
    try:
        memories = DuckDBStateStore(root).load_memories()
        if memories:
            return memories
    except Exception:
        pass
    rows: list[dict[str, Any]] = []
    mem_root = control_memories_dir(root)
    if not mem_root.exists():
        return rows
    for path in sorted(mem_root.glob("*.jsonl")):
        rows.extend(read_jsonl(path))
    return sorted(rows, key=lambda row: row.get("created_at", ""))


def memory_visible_to(memory: dict[str, Any], viewer: str) -> bool:
    scope = memory.get("scope", "private")
    owner = memory.get("owner", "")
    if viewer == "brain":
        return True
    if scope == "shared":
        return True
    return owner == viewer


def load_visible_memories(root: Path, viewer: str) -> list[dict[str, Any]]:
    return [memory for memory in load_all_memories(root) if memory_visible_to(memory, viewer)]


def owner_label(owner: str) -> str:
    if owner == "brain":
        return "brain"
    if owner.startswith("agent:"):
        return owner.split(":", 1)[1]
    return owner


def action_summary(action: dict[str, Any]) -> str:
    return f"[{action.get('kind', 'guidance')}] {action.get('summary', '')}".strip()


def doc_excerpt(path: Path, max_lines: int = 40) -> list[str]:
    if not path.exists():
        return ["- missing"]
    lines = path.read_text(errors="replace").splitlines()
    excerpt = lines[:max_lines]
    if len(lines) > max_lines:
        excerpt.append("...")
    return excerpt or ["- empty"]


def state_age_minutes(state: dict[str, Any]) -> int | None:
    updated_at = state.get("updated_at")
    if not updated_at:
        return None
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    return max(0, int(delta.total_seconds() // 60))


def attention_items(root: Path, states: list[dict[str, Any]]) -> list[str]:
    items: list[str] = []
    for state in states:
        agent_id = state.get("agent_id", "")
        if agent_id == "supervisor":
            continue
        phase = state.get("phase", "unknown")
        age = state_age_minutes(state)
        pending = latest_pending_action(root, agent_id)
        blocker = state.get("blocker", "none")
        needs_brain = flatten_text(state.get("needs_brain")) or "no"
        if phase in {"blocked", "crashed", "ready-for-review"}:
            items.append(
                f"- {agent_id}: phase={phase}, action={state.get('action', '')}, needs_brain={needs_brain}, blocker={blocker}, age={age or 0}m"
            )
            continue
        if needs_brain not in {"", "no", "none"}:
            items.append(
                f"- {agent_id}: explicit brain request={needs_brain}, action={state.get('action', '')}, blocker={blocker}"
            )
            continue
        if pending:
            items.append(
                f"- {agent_id}: pending action {pending.get('kind')} from {pending.get('from')} -> {pending.get('summary')}"
            )
        elif not state.get("action"):
            items.append(f"- {agent_id}: missing current action")
        elif age is not None and age >= 10 and phase not in {"done", "monitoring"}:
            items.append(
                f"- {agent_id}: stale for {age}m while phase={phase}, summary={state.get('summary', '')}"
            )
    return items


def format_state_line(root: Path, state: dict[str, Any]) -> str:
    agent_id = state.get("agent_id", "")
    age = state_age_minutes(state)
    pending = latest_pending_action(root, agent_id)
    pending_text = f", pending_action={action_summary(pending)}" if pending else ""
    depends_text = csv_or_none(state.get("depends_on", []))
    return (
        f"- {agent_id}: phase={state.get('phase', 'unknown')}, action={state.get('action', '')}, "
        f"summary={state.get('summary', '')}, next={state.get('next_step', '')}, "
        f"depends_on={depends_text}, needs_brain={state.get('needs_brain', 'no')}, "
        f"blocker={state.get('blocker', 'none')}, age={age or 0}m{pending_text}"
    )


def recent_action_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for agent_id in list_agent_ids(root):
        if agent_id == "supervisor":
            continue
        action = latest_pending_action(root, agent_id)
        if action:
            lines.append(
                f"- {agent_id}: {action.get('kind')} from {action.get('from')} -> {action.get('summary')}"
            )
    return lines


def grouped_memory_lines(memories: list[dict[str, Any]], limit_per_owner: int = 3) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for memory in memories:
        grouped.setdefault(memory.get("owner", "unknown"), []).append(memory)
    lines: list[str] = []
    for owner in sorted(grouped):
        lines.append(f"### {owner_label(owner)}")
        for memory in grouped[owner][-limit_per_owner:]:
            tags = f" tags={','.join(memory.get('tags', []))}" if memory.get("tags") else ""
            lines.append(
                f"- [{memory.get('scope', 'private')}/{memory.get('kind', 'note')}] {memory.get('summary', '')}{tags}"
            )
    return lines


def render_memory_document(title: str, memories: list[dict[str, Any]]) -> str:
    lines = [f"# {title}", "", f"Last rendered: {now_display()}", ""]
    if not memories:
        lines.extend(["## Summary", "- none"])
        return "\n".join(lines).strip() + "\n"
    grouped: dict[str, list[dict[str, Any]]] = {}
    for memory in memories:
        grouped.setdefault(memory.get("kind", "note"), []).append(memory)
    for kind in sorted(grouped):
        lines.append(f"## {kind.title()}")
        for memory in grouped[kind][-20:]:
            owner = owner_label(memory.get("owner", "unknown"))
            scope = memory.get("scope", "private")
            summary = memory.get("summary", "")
            details = memory.get("details", "")
            lines.append(f"- [{scope}] {owner}: {summary}")
            if details:
                lines.append(f"  - {details}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_memory_mirrors(root: Path) -> None:
    ensure_project_layout(root, list_agent_ids(root))
    memories = load_all_memories(root)
    brain_items = [memory for memory in memories if memory.get("owner") == "brain"]
    shared_items = [memory for memory in memories if memory.get("scope") == "shared"]

    brain_path = project_brain_memory_path(root)
    brain_path.write_text(render_memory_document("Brain Memory", brain_items))
    shared_path = shared_memory_path(root)
    shared_path.write_text(render_memory_document("Shared Memory", shared_items))

    legacy_brain = bz_dir(root) / "memory" / "brain.md"
    ensure_parent(legacy_brain)
    legacy_brain.write_text(brain_path.read_text())

    for agent_id in list_agent_ids(root):
        if agent_id == "supervisor":
            continue
        for path in (brain_path, shared_path):
            mirror = worktree_mirror_path(root, agent_id, rel_to_root(root, path))
            if mirror is not None:
                ensure_parent(mirror)
                mirror.write_text(path.read_text())
        owner = f"agent:{agent_id}"
        own_items = [memory for memory in memories if memory.get("owner") == owner]
        agent_path = project_agent_memory_path(root, agent_id)
        agent_path.write_text(render_memory_document(f"Agent Memory: {agent_id}", own_items))
        mirror = worktree_mirror_path(root, agent_id, rel_to_root(root, agent_path))
        if mirror is not None:
            ensure_parent(mirror)
            mirror.write_text(agent_path.read_text())
        legacy_agent = bz_dir(root) / "memory" / "agents" / f"{agent_id}.md"
        ensure_parent(legacy_agent)
        legacy_agent.write_text(agent_path.read_text())


def build_brain_context(root: Path) -> str:
    states = [load_state(root, agent_id) for agent_id in list_agent_ids(root) if load_state(root, agent_id)]
    visible_memories = load_visible_memories(root, "brain")
    lines = [
        "# Brain Context",
        f"Generated: {now_display()}",
        "",
        "## Project",
    ]
    lines.extend(doc_excerpt(project_paths(root).project_md, max_lines=24))
    lines.extend(["", "## Target Criteria"])
    lines.extend(doc_excerpt(project_paths(root).target_md, max_lines=24))
    lines.extend(["", "## Brain Soul"])
    lines.extend(doc_excerpt(brain_soul_path(root), max_lines=24))
    lines.extend([
        "",
        "## Attention Queue",
    ])
    attention = attention_items(root, states)
    lines.extend(attention or ["- none"])
    lines.extend(["", "## Agent Snapshot"])
    lines.extend([format_state_line(root, state) for state in states if state.get("agent_id") != "supervisor"] or ["- none"])
    lines.extend(["", "## Pending Actions"])
    lines.extend(recent_action_lines(root) or ["- none"])
    lines.extend(["", "## Memory Ledger"])
    lines.extend(grouped_memory_lines(visible_memories) or ["- none"])
    return "\n".join(lines).strip() + "\n"


def build_agent_context(root: Path, agent_id: str) -> str:
    viewer = f"agent:{agent_id}"
    state = load_state(root, agent_id)
    visible_memories = load_visible_memories(root, viewer)
    own_memories = [memory for memory in visible_memories if memory.get("owner") == viewer]
    shared_team_memories = [
        memory for memory in visible_memories
        if memory.get("owner") != viewer and memory.get("scope") == "shared"
    ]
    pending = latest_pending_action(root, agent_id)
    lines = [
        f"# Agent Context: {agent_id}",
        f"Generated: {now_display()}",
        "",
        "## Project",
    ]
    lines.extend(doc_excerpt(project_paths(root).project_md, max_lines=18))
    lines.extend(["", "## Target Criteria"])
    lines.extend(doc_excerpt(project_paths(root).target_md, max_lines=18))
    lines.extend(["", "## Your Soul"])
    lines.extend(doc_excerpt(agent_soul_path(root, agent_id), max_lines=24))
    lines.extend(["", "## Your Plan"])
    lines.extend(doc_excerpt(agent_plan_path(root, agent_id), max_lines=24))
    lines.extend([
        "",
        "## Your State",
        format_state_line(root, state) if state else "- no canonical state yet",
        "",
        "## Latest Action",
    ])
    if pending:
        lines.extend(
            [
                f"- from={pending.get('from')} kind={pending.get('kind')} summary={pending.get('summary')}",
                f"- details: {pending.get('details') or 'none'}",
            ]
        )
    else:
        lines.append("- none")
    lines.extend(["", "## Shared Team Memory"])
    lines.extend(grouped_memory_lines(shared_team_memories, limit_per_owner=2) or ["- none"])
    lines.extend(["", "## Your Memory"])
    lines.extend(grouped_memory_lines(own_memories, limit_per_owner=4) or ["- none"])
    return "\n".join(lines).strip() + "\n"


def refresh_contexts(root: Path) -> None:
    ensure_layout(root)
    render_memory_mirrors(root)
    brain_text = build_brain_context(root)
    ensure_parent(context_path(root, "brain"))
    context_path(root, "brain").write_text(brain_text)
    for agent_id in list_agent_ids(root):
        if agent_id == "supervisor":
            continue
        ctx = build_agent_context(root, agent_id)
        path = context_path(root, f"agent:{agent_id}")
        ensure_parent(path)
        path.write_text(ctx)
        mirror_path = worktree_mirror_path(root, agent_id, f".bz/control/contexts/{agent_id}.md")
        if mirror_path is not None:
            ensure_parent(mirror_path)
            mirror_path.write_text(ctx)
        latest = latest_pending_action(root, agent_id)
        if latest:
            action_md = render_action_markdown(latest)
            write_text_and_mirror(root, agent_id, f".bz/control/agents/{agent_id}/latest-action.md", action_md)


def load_events(root: Path, limit: int | None = None) -> list[dict[str, Any]]:
    try:
        events = DuckDBStateStore(root).load_events(limit=limit)
        if events:
            return events
    except Exception:
        pass
    rows = read_jsonl(events_path(root))
    return rows[-limit:] if limit else rows


def extract_json_payload(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    starts = [idx for idx, char in enumerate(stripped) if char == "{"]
    for start in starts:
        candidate = stripped[start:]
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            continue
    return None


def ingest_brain_output(root: Path, output_text: str, mode: str = "", reason: str = "") -> list[str]:
    queued_targets: list[str] = []
    payload = extract_json_payload(output_text)
    if payload:
        brain_state = payload.get("brain_state", {})
        if isinstance(brain_state, dict):
            write_state(
                root,
                agent_id="supervisor",
                phase=brain_state.get("phase") or "monitoring",
                action=brain_state.get("action") or f"assessing {mode or 'signal'}",
                summary=brain_state.get("summary") or f"Handled {mode or 'brain'} signal",
                depends_on=brain_state.get("depends_on") if isinstance(brain_state.get("depends_on"), list) else [],
                needs_brain=brain_state.get("needs_brain") or "no",
                next_step=brain_state.get("next_step") or "wait for next signal",
                blocker=brain_state.get("blocker") or "none",
                files_touched=[],
                updated_by="brain",
                source="brain-output",
            )
        memories = payload.get("brain_memory", [])
        if isinstance(memories, list):
            for memory in memories:
                if not isinstance(memory, dict):
                    continue
                add_memory(
                    root,
                    owner="brain",
                    scope=memory.get("scope", "private"),
                    kind=memory.get("kind", "decision"),
                    summary=memory.get("summary", ""),
                    details=memory.get("details", ""),
                    tags=memory.get("tags") if isinstance(memory.get("tags"), list) else [],
                    related_agents=memory.get("related_agents") if isinstance(memory.get("related_agents"), list) else [],
                )
        actions = payload.get("actions", [])
        if isinstance(actions, list):
            for item in actions:
                if not isinstance(item, dict):
                    continue
                target = flatten_text(item.get("to"))
                if not target:
                    continue
                kind = flatten_text(item.get("kind")) or "guidance"
                summary = flatten_text(item.get("summary"))
                details = item.get("details", "")
                reason_text = flatten_text(item.get("reason")) or flatten_text(reason)
                queue_action(
                    root,
                    from_actor="brain",
                    to_agent=target,
                    kind=kind,
                    summary=summary,
                    details=details,
                    reason=reason_text,
                    replace_pending=True,
                )
                if kind == "accept":
                    current = load_state(root, target)
                    write_state(
                        root,
                        agent_id=target,
                        phase="done",
                        action="waiting for merge or follow-up",
                        summary=summary or current.get("summary") or "Brain accepted the work.",
                        depends_on=current.get("depends_on", []),
                        needs_brain="no",
                        next_step="none",
                        blocker="none",
                        files_touched=current.get("files_touched", []),
                        updated_by="brain",
                        source="brain-review",
                    )
                queued_targets.append(target)
        return sorted(set(queued_targets))

    for raw_line in output_text.splitlines():
        line = raw_line.strip().strip("*`")
        if not line.startswith("DECISION:"):
            continue
        payload_text = line.split("DECISION:", 1)[1].strip()
        parts = [part.strip() for part in payload_text.split("—") if part.strip()]
        if len(parts) < 2:
            parts = [part.strip() for part in payload_text.split("|") if part.strip()]
        if len(parts) < 2:
            continue
        target = parts[0]
        kind = "guidance"
        summary = parts[1]
        details = " | ".join(parts[2:]) if len(parts) > 2 else ""
        queue_action(
            root,
            from_actor="brain",
            to_agent=target,
            kind=kind,
            summary=summary,
            details=details,
            reason=reason,
            replace_pending=True,
        )
        queued_targets.append(target)
    if queued_targets:
        write_state(
            root,
            agent_id="supervisor",
            phase="monitoring",
            action=f"handled {mode or 'signal'}",
            summary=f"Created {len(set(queued_targets))} action(s)",
            depends_on=[],
            needs_brain="no",
            next_step="wait for next signal",
            blocker="none",
            files_touched=[],
            updated_by="brain",
            source="brain-output",
        )
    return sorted(set(queued_targets))


def dashboard_state(root: Path, agent_id: str) -> dict[str, Any]:
    state = load_state(root, agent_id)
    if not state:
        return {}
    latest = latest_pending_action(root, agent_id)
    memories = load_all_memories(root)
    memory_count = sum(
        1
        for memory in memories
        if memory.get("owner") in {f"agent:{agent_id}", "brain"} and (
            memory.get("owner") == f"agent:{agent_id}" or memory.get("scope") == "shared"
        )
    )
    return {
        "state": state.get("phase", "unknown"),
        "action": state.get("action", ""),
        "summary": state.get("summary", ""),
        "files touched": csv_or_none(state.get("files_touched", [])),
        "depends on": csv_or_none(state.get("depends_on", [])),
        "needs brain": state.get("needs_brain", "no"),
        "next step": state.get("next_step", ""),
        "blocker": state.get("blocker", "none"),
        "last updated": iso_to_display(state.get("updated_at")),
        "pending_action": latest,
        "memory_count": memory_count,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="brain-zombies control plane")
    parser.add_argument("--project-root", default=".", help="Project root containing .bz/")
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Initialize control plane layout")
    init_parser.add_argument("--agents", nargs="*", default=[])

    sync_parser = sub.add_parser("sync-all", help="Sync STATUS.md into canonical state")
    sync_parser.add_argument("--quiet", action="store_true")

    sync_one = sub.add_parser("sync-status", help="Sync one agent STATUS.md")
    sync_one.add_argument("--agent", required=True)

    write_parser = sub.add_parser("write-state", help="Write canonical state and STATUS.md")
    write_parser.add_argument("--agent", required=True)
    write_parser.add_argument("--phase")
    write_parser.add_argument("--action")
    write_parser.add_argument("--summary")
    write_parser.add_argument("--depends-on", default="")
    write_parser.add_argument("--needs-brain", default="")
    write_parser.add_argument("--next-step")
    write_parser.add_argument("--blocker")
    write_parser.add_argument("--files", default="")
    write_parser.add_argument("--updated-by", default="system")
    write_parser.add_argument("--source", default="control-plane")

    action_parser = sub.add_parser("queue-action", help="Queue a structured action")
    action_parser.add_argument("--from", dest="from_actor", required=True)
    action_parser.add_argument("--to", required=True)
    action_parser.add_argument("--kind", required=True)
    action_parser.add_argument("--summary", required=True)
    action_parser.add_argument("--details", default="")
    action_parser.add_argument("--reason", default="")
    action_parser.add_argument("--keep-pending", action="store_true")

    pending_parser = sub.add_parser("pending-targets", help="Print targets with pending actions")

    latest_parser = sub.add_parser("latest-action", help="Print latest pending action")
    latest_parser.add_argument("--agent", required=True)
    latest_parser.add_argument("--format", choices=["markdown", "summary", "json"], default="markdown")

    memory_parser = sub.add_parser("add-memory", help="Store a memory item")
    memory_parser.add_argument("--owner", required=True)
    memory_parser.add_argument("--scope", default="private")
    memory_parser.add_argument("--kind", default="note")
    memory_parser.add_argument("--summary", required=True)
    memory_parser.add_argument("--details", default="")
    memory_parser.add_argument("--tags", default="")
    memory_parser.add_argument("--related", default="")

    task_parser = sub.add_parser("task-event", help="Append a task/subtask state event")
    task_parser.add_argument("--agent", required=True)
    task_parser.add_argument("--task", required=True)
    task_parser.add_argument("--sub-task", default="")
    task_parser.add_argument("--state", required=True)
    task_parser.add_argument("--notes", default="")
    task_parser.add_argument("--source", default="control-plane")

    render_parser = sub.add_parser("render-context", help="Render brain or agent context")
    render_parser.add_argument("--viewer", required=True)

    mirror_parser = sub.add_parser("render-mirrors", help="Regenerate Markdown mirrors from DuckDB")

    stale_parser = sub.add_parser("stale-agents", help="Print active agents missing heartbeat")
    stale_parser.add_argument("--heartbeat-mins", type=int, default=10)
    stale_parser.add_argument("--format", choices=["names", "json"], default="names")

    scheduler_parser = sub.add_parser("record-scheduler-check", help="Append a scheduler check row")
    scheduler_parser.add_argument("--agent", required=True)
    scheduler_parser.add_argument("--type", required=True)
    scheduler_parser.add_argument("--result", required=True)
    scheduler_parser.add_argument("--notes", default="")

    ingest_parser = sub.add_parser("ingest-brain-output", help="Convert brain output into control-plane records")
    ingest_parser.add_argument("--output-file", required=True)
    ingest_parser.add_argument("--mode", default="")
    ingest_parser.add_argument("--reason", default="")

    event_parser = sub.add_parser("record-event", help="Append a generic event")
    event_parser.add_argument("--type", required=True)
    event_parser.add_argument("--source", required=True)
    event_parser.add_argument("--target", default="")
    event_parser.add_argument("--summary", default="")
    event_parser.add_argument("--details", default="")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = project_root_path(args.project_root)

    if args.command == "init":
        ensure_layout(root, args.agents)
        for agent_id in args.agents:
            (control_agents_dir(root) / agent_id).mkdir(parents=True, exist_ok=True)
        refresh_contexts(root)
        return 0

    if args.command == "sync-all":
        sync_all(root)
        if not args.quiet:
            print("\n".join(list_agent_ids(root)))
        return 0

    if args.command == "sync-status":
        state = sync_agent_from_status(root, args.agent)
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    if args.command == "write-state":
        state = write_state(
            root,
            agent_id=args.agent,
            phase=args.phase,
            action=args.action,
            summary=args.summary,
            depends_on=parse_csv(args.depends_on),
            needs_brain=args.needs_brain,
            next_step=args.next_step,
            blocker=args.blocker,
            files_touched=parse_csv(args.files),
            updated_by=args.updated_by,
            source=args.source,
        )
        print(json.dumps(state, indent=2, sort_keys=True))
        return 0

    if args.command == "queue-action":
        action = queue_action(
            root,
            from_actor=args.from_actor,
            to_agent=args.to,
            kind=args.kind,
            summary=args.summary,
            details=args.details,
            reason=args.reason,
            replace_pending=not args.keep_pending,
        )
        print(json.dumps(action, indent=2, sort_keys=True))
        return 0

    if args.command == "pending-targets":
        for target in pending_targets(root):
            print(target)
        return 0

    if args.command == "latest-action":
        action = latest_pending_action(root, args.agent)
        if not action:
            return 0
        if args.format == "summary":
            print(action_summary(action))
        elif args.format == "json":
            print(json.dumps(action, indent=2, sort_keys=True))
        else:
            print(render_action_markdown(action), end="")
        return 0

    if args.command == "add-memory":
        entry = add_memory(
            root,
            owner=args.owner,
            scope=args.scope,
            kind=args.kind,
            summary=args.summary,
            details=args.details,
            tags=parse_csv(args.tags),
            related_agents=parse_csv(args.related),
        )
        print(json.dumps(entry, indent=2, sort_keys=True))
        return 0

    if args.command == "task-event":
        entry = DuckDBStateStore(root).add_task_event(
            zombie_name=args.agent,
            task=args.task,
            sub_task=args.sub_task,
            state=args.state,
            notes=args.notes,
            source=args.source,
        )
        append_event(
            root,
            event_type="task_event",
            source=args.source,
            target=f"agent:{args.agent}",
            summary=f"{args.agent} {args.state}: {args.task}",
            details=args.notes,
            payload=entry,
        )
        refresh_contexts(root)
        print(json.dumps(entry, indent=2, sort_keys=True))
        return 0

    if args.command == "render-context":
        refresh_contexts(root)
        if args.viewer == "brain":
            print(build_brain_context(root), end="")
        else:
            print(build_agent_context(root, args.viewer.split(":", 1)[-1]), end="")
        return 0

    if args.command == "render-mirrors":
        refresh_contexts(root)
        return 0

    if args.command == "stale-agents":
        stale = DuckDBStateStore(root).stale_agents(args.heartbeat_mins)
        for row in stale:
            DuckDBStateStore(root).add_scheduler_check(
                row.get("agent_id", ""),
                "heartbeat",
                "stale",
                f"age={row.get('age_minutes', 0)}m phase={row.get('phase', 'unknown')}",
            )
        if args.format == "json":
            print(json.dumps(stale, indent=2, sort_keys=True))
        else:
            for row in stale:
                print(f"{row.get('agent_id')}({row.get('age_minutes', 0)}m)")
        return 0

    if args.command == "record-scheduler-check":
        entry = DuckDBStateStore(root).add_scheduler_check(
            args.agent,
            args.type,
            args.result,
            args.notes,
        )
        print(json.dumps(entry, indent=2, sort_keys=True))
        return 0

    if args.command == "ingest-brain-output":
        output_path = Path(args.output_file)
        queued = ingest_brain_output(root, output_path.read_text() if output_path.exists() else "", mode=args.mode, reason=args.reason)
        for target in queued:
            print(target)
        return 0

    if args.command == "record-event":
        append_event(
            root,
            event_type=args.type,
            source=args.source,
            target=args.target,
            summary=args.summary,
            details=args.details,
        )
        refresh_contexts(root)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
