#!/usr/bin/env python3
"""Interactive `.bz/project` initializer for target projects."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

import yaml

from project_layout import (
    agent_memory_path,
    agent_output_dir,
    agent_plan_path,
    agent_soul_path,
    brain_agent_chatlog_path,
    brain_memory_path,
    brain_output_dir,
    brain_soul_path,
    ensure_project_layout,
    project_paths,
    rel_to_root,
    shared_memory_path,
    user_brain_chatlog_path,
)
from state_store import DuckDBStateStore, now_iso


class BlockStyleDumper(yaml.SafeDumper):
    """Use block scalars for multi-line YAML strings."""


def _represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockStyleDumper.add_representer(str, _represent_str)


def read_config(root: Path) -> dict[str, Any]:
    config_path = root / "bz.yaml"
    if not config_path.exists():
        raise SystemExit("No bz.yaml found. Generate config before initializing .bz/project.")
    return yaml.safe_load(config_path.read_text()) or {}


def write_config(root: Path, config: dict[str, Any]) -> None:
    text = yaml.dump(
        config,
        Dumper=BlockStyleDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
        width=100,
    )
    (root / "bz.yaml").write_text(text)


def default_summary(brief: str, project_name: str) -> str:
    for line in brief.splitlines():
        line = line.strip(" -#\t")
        if line:
            return line[:220]
    return f"Build and coordinate the {project_name} project."


def prompt_line(label: str, default: str, auto_yes: bool) -> str:
    if auto_yes or not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    answer = input(f"{label}{suffix}: ").strip()
    return answer or default


def write_if_missing(path: Path, content: str, overwrite: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not path.exists():
        path.write_text(content.rstrip() + "\n")


def project_markdown(project_name: str, brief: str, summary: str, agents: list[dict[str, Any]]) -> str:
    lines = [
        f"# Project: {project_name}",
        "",
        "## Summary",
        summary,
        "",
        "## Brief",
        brief.strip() or "No brief provided.",
        "",
        "## Agents",
    ]
    for agent in agents:
        lines.append(f"- `{agent.get('id', '')}`: {str(agent.get('task', '')).strip() or 'No task specified.'}")
    return "\n".join(lines)


def target_markdown(criteria: str) -> str:
    return "\n".join(
        [
            "# Target Criteria",
            "",
            "## Success Criteria",
            criteria.strip() or "- The project meets the brief and all zombie work has been reviewed by the brain.",
            "",
            "## Completion Rule",
            "- The brain is the only actor that can mark a zombie `done` after review evidence is sufficient.",
        ]
    )


def brain_soul_markdown(soul: str) -> str:
    return "\n".join(
        [
            "# Brain Soul",
            "",
            soul.strip(),
            "",
            "## Operating Rules",
            "- Read PROJECT.md and TARGET.md before making coordination decisions.",
            "- Maintain shared_mem.md as the compact team memory.",
            "- Review zombie plans, outputs, memory, and state history before accepting work.",
            "- Wake proactively when a zombie misses its heartbeat or asks for guidance.",
        ]
    )


def zombie_soul_markdown(agent_id: str, task: str, soul: str) -> str:
    return "\n".join(
        [
            f"# Zombie Soul: {agent_id}",
            "",
            "## Identity",
            soul.strip(),
            "",
            "## Assigned Task",
            task.strip() or "No task specified.",
            "",
            "## Operating Rules",
            "- Read PROJECT.md, TARGET.md, your soul, shared memory, and your private memory before planning.",
            f"- Write your plan to `.bz/project/plans/{agent_id}_plan.md` before implementation.",
            "- Update DuckDB state after each meaningful step and at least every 10 minutes while active.",
            "- Keep your memory concise: durable decisions, progress, blockers, and handoff notes only.",
            "- Ask the brain for review when the task is ready to be accepted.",
        ],
    )


def memory_markdown(title: str, body: str) -> str:
    return "\n".join([f"# {title}", "", body.strip() or "- none"])


def plan_markdown(agent_id: str) -> str:
    return "\n".join(
        [
            f"# Plan: {agent_id}",
            "",
            "Status: draft",
            "",
            "The zombie must replace this draft with a concrete step-by-step plan before implementation.",
        ]
    )


def scheduler_policy(config: dict[str, Any]) -> str:
    supervisor = config.get("supervisor", {}) or {}
    heartbeat = int(supervisor.get("zombie_heartbeat_mins", 10) or 10)
    proactive = int(supervisor.get("proactive_check_mins", 15) or 15)
    max_brain_reviews = int(supervisor.get("max_brain_reviews", 8) or 0)
    max_agent_restarts = int(supervisor.get("max_agent_restarts", 2) or 0)
    max_total_minutes = int(supervisor.get("max_total_minutes", 45) or 0)
    return "\n".join(
        [
            f"zombie_heartbeat_mins: {heartbeat}",
            f"proactive_check_mins: {proactive}",
            f"max_brain_reviews: {max_brain_reviews}",
            f"max_agent_restarts: {max_agent_restarts}",
            f"max_total_minutes: {max_total_minutes}",
            "stale_action: status-check",
            "second_miss: wake-brain",
        ]
    )


def initialize_project(
    root: Path,
    auto_yes: bool = False,
    overwrite_docs: bool = False,
    write_config_file: bool = True,
) -> None:
    config = read_config(root)
    project = config.setdefault("project", {})
    supervisor = config.setdefault("supervisor", {})
    supervisor.setdefault("zombie_heartbeat_mins", 10)
    supervisor.setdefault("proactive_check_mins", 15)
    supervisor.setdefault("max_brain_reviews", 8)
    supervisor.setdefault("max_agent_restarts", 2)
    supervisor.setdefault("max_total_minutes", 45)
    supervisor.setdefault("max_agent_iterations", 5)
    agents = config.get("agents", []) or []
    for agent in agents:
        if isinstance(agent, dict):
            agent.setdefault("max_iterations", supervisor.get("max_agent_iterations", 5))

    project_name = str(project.get("name") or root.name)
    brief = str(project.get("brief") or "")
    summary = prompt_line("Project summary", default_summary(brief, project_name), auto_yes)
    target = prompt_line(
        "Success criteria",
        "All project goals in the brief are implemented, verified, and accepted by the brain.",
        auto_yes,
    )
    brain_soul = prompt_line(
        "Brain soul",
        "You are the supervisor: decompose work, monitor zombies, maintain shared memory, and accept only verified progress.",
        auto_yes,
    )

    agent_souls: dict[str, str] = {}
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        task = str(agent.get("task") or "").strip()
        if not agent_id:
            continue
        agent_souls[agent_id] = prompt_line(
            f"Soul for zombie `{agent_id}`",
            f"You are `{agent_id}`. Own your assigned task, report concise state, and produce reviewable work.",
            auto_yes,
        )
        agent["soul"] = agent_souls[agent_id]

    paths = ensure_project_layout(root, [agent.get("id", "") for agent in agents] + ["supervisor"])
    if write_config_file:
        write_config(root, config)

    write_if_missing(paths.project_md, project_markdown(project_name, brief, summary, agents), overwrite_docs)
    write_if_missing(paths.target_md, target_markdown(target), overwrite_docs)
    write_if_missing(brain_soul_path(root), brain_soul_markdown(brain_soul), overwrite_docs)
    write_if_missing(brain_memory_path(root), memory_markdown("Brain Memory", "- Initialized project."), False)
    write_if_missing(shared_memory_path(root), memory_markdown("Shared Memory", "- Project initialized."), False)
    write_if_missing(paths.scheduler_policy, scheduler_policy(config), True)
    write_if_missing(user_brain_chatlog_path(root), "# User Brain Chatlog\n", False)

    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        task = str(agent.get("task") or "").strip()
        write_if_missing(
            agent_soul_path(root, agent_id),
            zombie_soul_markdown(agent_id, task, agent_souls.get(agent_id, "")),
            overwrite_docs,
        )
        write_if_missing(agent_memory_path(root, agent_id), memory_markdown(f"Agent Memory: {agent_id}", "- Waiting to begin."), False)
        write_if_missing(agent_plan_path(root, agent_id), plan_markdown(agent_id), False)
        write_if_missing(brain_agent_chatlog_path(root, agent_id), f"# Brain {agent_id} Chatlog\n", False)
        agent_output_dir(root, agent_id).mkdir(parents=True, exist_ok=True)

    brain_output_dir(root).mkdir(parents=True, exist_ok=True)

    store = DuckDBStateStore(root)
    agent_ids = [str(agent.get("id")) for agent in agents if str(agent.get("id") or "").strip()]
    store.initialize(agent_ids + ["supervisor"])
    now = now_iso()
    if not store.load_memories():
        store.add_memory(
            {
                "id": f"mem-{uuid.uuid4().hex[:12]}",
                "schema_version": 1,
                "created_at": now,
                "owner": "brain",
                "scope": "private",
                "kind": "observation",
                "summary": "Project initialized.",
                "details": "PROJECT.md, TARGET.md, souls, memories, plans, scheduler policy, and state.duckdb were created.",
                "tags": ["init"],
                "related_agents": agent_ids,
            }
        )
        store.add_memory(
            {
                "id": f"mem-{uuid.uuid4().hex[:12]}",
                "schema_version": 1,
                "created_at": now,
                "owner": "brain",
                "scope": "shared",
                "kind": "handoff",
                "summary": "All zombies should read PROJECT.md, TARGET.md, their soul, shared memory, and their private memory before planning.",
                "details": "",
                "tags": ["init"],
                "related_agents": agent_ids,
            }
        )
    store.upsert_agent_state(
        {
            "agent_id": "supervisor",
            "role": "brain",
            "phase": "monitoring",
            "action": "project initialized",
            "summary": "Project files and state database initialized.",
            "depends_on": agent_ids,
            "needs_brain": "no",
            "next_step": "Wait for launch.",
            "blocker": "none",
            "files_touched": [
                rel_to_root(root, paths.project_md),
                rel_to_root(root, paths.target_md),
                rel_to_root(root, brain_soul_path(root)),
            ],
            "updated_at": now,
            "updated_by": "system",
            "source": "project-init",
        }
    )
    for agent in agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        store.upsert_agent_state(
            {
                "agent_id": agent_id,
                "role": "agent",
                "runtime": str(agent.get("runtime") or ""),
                "model": str(agent.get("model") or ""),
                "phase": "starting",
                "action": "waiting for launch",
                "summary": "Initialized and waiting for launch.",
                "depends_on": [],
                "needs_brain": "no",
                "next_step": "Read project files and write plan after launch.",
                "blocker": "none",
                "files_touched": [],
                "updated_at": now,
                "updated_by": "system",
                "source": "project-init",
            }
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize .bz/project files")
    parser.add_argument("--project-root", default=".")
    parser.add_argument("--yes", action="store_true", help="Use defaults without interactive prompts")
    parser.add_argument("--overwrite-docs", action="store_true", help="Overwrite PROJECT/TARGET/soul docs")
    parser.add_argument("--no-config-write", action="store_true", help="Do not rewrite bz.yaml")
    args = parser.parse_args(argv)
    initialize_project(
        Path(args.project_root).resolve(),
        auto_yes=args.yes,
        overwrite_docs=args.overwrite_docs,
        write_config_file=not args.no_config_write,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
