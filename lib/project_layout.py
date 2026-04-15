#!/usr/bin/env python3
"""Path helpers for the per-target-project brain-zombies layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


BZ_DIRNAME = ".bz"
PROJECT_DIRNAME = "project"


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    bz_dir: Path
    project_dir: Path
    project_md: Path
    target_md: Path
    state_db: Path
    scheduler_dir: Path
    scheduler_policy: Path
    souls_dir: Path
    memories_dir: Path
    plans_dir: Path
    outputs_dir: Path
    chatlogs_dir: Path
    legacy_agents_dir: Path
    legacy_memory_dir: Path
    legacy_agent_memory_dir: Path
    control_dir: Path
    control_agents_dir: Path
    control_memories_dir: Path
    control_contexts_dir: Path
    logs_dir: Path
    bin_dir: Path
    worktrees_dir: Path


def project_paths(root: str | Path) -> ProjectPaths:
    root_path = Path(root).resolve()
    bz_dir = root_path / BZ_DIRNAME
    project_dir = bz_dir / PROJECT_DIRNAME
    control_dir = bz_dir / "control"
    legacy_memory_dir = bz_dir / "memory"
    return ProjectPaths(
        root=root_path,
        bz_dir=bz_dir,
        project_dir=project_dir,
        project_md=project_dir / "PROJECT.md",
        target_md=project_dir / "TARGET.md",
        state_db=project_dir / "state.duckdb",
        scheduler_dir=project_dir / "scheduler",
        scheduler_policy=project_dir / "scheduler" / "policy.yaml",
        souls_dir=project_dir / "souls",
        memories_dir=project_dir / "memories",
        plans_dir=project_dir / "plans",
        outputs_dir=project_dir / "outputs",
        chatlogs_dir=project_dir / "chatlogs",
        legacy_agents_dir=bz_dir / "agents",
        legacy_memory_dir=legacy_memory_dir,
        legacy_agent_memory_dir=legacy_memory_dir / "agents",
        control_dir=control_dir,
        control_agents_dir=control_dir / "agents",
        control_memories_dir=control_dir / "memories",
        control_contexts_dir=control_dir / "contexts",
        logs_dir=bz_dir / "logs",
        bin_dir=bz_dir / "bin",
        worktrees_dir=bz_dir / "worktrees",
    )


def normalize_agent_id(agent_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", str(agent_id).strip())
    cleaned = cleaned.strip("-_")
    return cleaned or "agent"


def brain_soul_path(root: str | Path) -> Path:
    return project_paths(root).souls_dir / "brain_soul.md"


def agent_soul_path(root: str | Path, agent_id: str) -> Path:
    return project_paths(root).souls_dir / f"{normalize_agent_id(agent_id)}_soul.md"


def brain_memory_path(root: str | Path) -> Path:
    return project_paths(root).memories_dir / "brain_mem.md"


def shared_memory_path(root: str | Path) -> Path:
    return project_paths(root).memories_dir / "shared_mem.md"


def agent_memory_path(root: str | Path, agent_id: str) -> Path:
    return project_paths(root).memories_dir / f"{normalize_agent_id(agent_id)}_mem.md"


def agent_plan_path(root: str | Path, agent_id: str) -> Path:
    return project_paths(root).plans_dir / f"{normalize_agent_id(agent_id)}_plan.md"


def agent_output_dir(root: str | Path, agent_id: str) -> Path:
    return project_paths(root).outputs_dir / normalize_agent_id(agent_id)


def brain_output_dir(root: str | Path) -> Path:
    return project_paths(root).outputs_dir / "brain"


def user_brain_chatlog_path(root: str | Path) -> Path:
    return project_paths(root).chatlogs_dir / "user_brain_chatlog.md"


def brain_agent_chatlog_path(root: str | Path, agent_id: str) -> Path:
    return project_paths(root).chatlogs_dir / f"brain_{normalize_agent_id(agent_id)}_chatlog.md"


def rel_to_root(root: str | Path, path: str | Path) -> str:
    root_path = Path(root).resolve()
    path_obj = Path(path).resolve()
    try:
        return str(path_obj.relative_to(root_path))
    except ValueError:
        return str(path_obj)


def ensure_project_layout(root: str | Path, agent_ids: Iterable[str] = ()) -> ProjectPaths:
    paths = project_paths(root)
    dirs = [
        paths.bz_dir,
        paths.project_dir,
        paths.scheduler_dir,
        paths.souls_dir,
        paths.memories_dir,
        paths.plans_dir,
        paths.outputs_dir,
        paths.chatlogs_dir,
        paths.legacy_agents_dir,
        paths.legacy_memory_dir,
        paths.legacy_agent_memory_dir,
        paths.control_agents_dir,
        paths.control_memories_dir,
        paths.control_contexts_dir,
        paths.logs_dir,
        paths.bin_dir,
        paths.worktrees_dir,
        brain_output_dir(root),
    ]
    for agent_id in agent_ids:
        normalized = normalize_agent_id(agent_id)
        dirs.extend(
            [
                paths.legacy_agents_dir / normalized,
                paths.control_agents_dir / normalized,
                agent_output_dir(root, normalized),
            ]
        )
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)
    return paths
