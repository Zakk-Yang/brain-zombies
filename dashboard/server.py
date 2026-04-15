#!/usr/bin/env python3
"""brain-zombies dashboard server — status API + config editor."""

from __future__ import annotations

import copy
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from project_layout import agent_memory_path as project_agent_memory_path
from project_layout import brain_agent_chatlog_path
from project_layout import brain_memory_path as project_brain_memory_path
from project_layout import shared_memory_path as project_shared_memory_path
from project_layout import user_brain_chatlog_path
import control_plane

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3333
PROJECT_ROOT = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path.cwd().resolve()
DEFAULT_BZ_EXECUTABLE = Path(shutil.which("bz") or (Path(__file__).resolve().parents[1] / "bz")).resolve()
BZ_EXECUTABLE = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else DEFAULT_BZ_EXECUTABLE
CONFIG_PATH = PROJECT_ROOT / "bz.yaml"
BZ_DIR = PROJECT_ROOT / ".bz"
DASHBOARD_DIR = Path(__file__).parent

RUNTIME_MODELS = {
    "claude": ["haiku", "sonnet", "opus"],
    "codex": ["gpt-4.1-mini", "gpt-4.1", "gpt-5.4"],
    "aider": [],
}
THINKING_LEVELS = ["", "medium", "high", "max"]
ROLE_OPTIONS = ["", "iterator"]
GIT_STRATEGIES = ["worktree", "shared"]
KNOWN_TOP_LEVEL_KEYS = {"project", "supervisor", "agents", "git"}
AGENT_DEPENDENCY_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*)\s+sets\s+State:\s+done$")

# Model pricing (per 1M tokens)
PRICING = {
    # Claude
    "haiku": {"input": 0.25, "output": 1.25},
    "sonnet": {"input": 3.0, "output": 15.0},
    "opus": {"input": 15.0, "output": 75.0},
    # OpenAI
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-5-nano": {"input": 0.05, "output": 0.40},
    "gpt-5-mini": {"input": 0.25, "output": 2.0},
    "gpt-5": {"input": 1.25, "output": 10.0},
    "gpt-5.4": {"input": 2.0, "output": 8.0},
    "gpt-5.3-codex-spark": {"input": 1.0, "output": 4.0},
    "gpt-5.3-codex": {"input": 1.5, "output": 6.0},
}


# Model info cache (populated on first request)
_model_info_cache = {}
_model_cache_time = 0
MODEL_CACHE_TTL = 3600  # refresh every hour


class BlockStyleDumper(yaml.SafeDumper):
    """Use block scalars for multi-line strings in generated YAML."""


def _represent_str(dumper, value):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


BlockStyleDumper.add_representer(str, _represent_str)


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def project_name_from_config(config: dict | None = None) -> str:
    name = ((config or {}).get("project", {}) or {}).get("name", "")
    return str(name).strip() or PROJECT_ROOT.name


def read_yaml() -> dict:
    """Read bz.yaml config."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    except Exception:
        return {}


def detect_available_runtimes() -> list[str]:
    available = [runtime for runtime in RUNTIME_MODELS if shutil.which(runtime)]
    return available or list(RUNTIME_MODELS.keys())


def default_runtime() -> str:
    available = detect_available_runtimes()
    for preferred in ("claude", "codex", "aider"):
        if preferred in available:
            return preferred
    return "claude"


def default_models(runtime: str) -> tuple[str, str]:
    if runtime == "claude":
        return "sonnet", "sonnet"
    if runtime == "codex":
        return "gpt-4.1", "gpt-4.1"
    return "", ""


def blank_agent(agent_id: str, runtime: str, model: str, task: str = "") -> dict:
    return {
        "id": agent_id,
        "runtime": runtime,
        "model": model,
        "task": task,
        "focus": [],
        "thinking": "",
        "role": "",
        "blocked_until": "",
        "dependency_mode": "immediate",
        "dependency_target": "",
        "custom_blocked_until": "",
    }


def preset_config(preset_id: str) -> dict:
    runtime = default_runtime()
    supervisor_model, agent_model = default_models(runtime)
    project_name = PROJECT_ROOT.name

    if preset_id == "single_builder":
        agents = [
            blank_agent(
                "builder",
                runtime,
                agent_model,
                "Implement the project from the brief and ship a working result.",
            )
        ]
        agents[0]["focus"] = ["src/"]
    elif preset_id == "custom_blank":
        agents = [blank_agent("agent-1", runtime, agent_model, "Define the work for this agent.")]
    else:
        researcher = blank_agent(
            "researcher",
            runtime,
            agent_model,
            "Research the requirements, APIs, data sources, and design constraints for this project. "
            "Write findings to documentation other agents can use.",
        )
        researcher["focus"] = ["docs/", "data/"]

        developer = blank_agent(
            "developer",
            runtime,
            agent_model,
            "Implement the project based on the brief and the research documentation. "
            "Write clean, working code.",
        )
        developer["focus"] = ["src/"]
        developer["blocked_until"] = "researcher sets State: done"
        developer["dependency_mode"] = "after-agent"
        developer["dependency_target"] = "researcher"
        developer["custom_blocked_until"] = developer["blocked_until"]
        agents = [researcher, developer]
        preset_id = "research_build"

    return {
        "project": {
            "name": project_name,
            "brief": "",
        },
        "supervisor": {
            "runtime": runtime,
            "model": supervisor_model,
            "thinking": "",
            "proactive_check_mins": 15,
            "zombie_heartbeat_mins": 10,
        },
        "agents": agents,
        "git": {
            "strategy": "worktree",
            "auto_pr": False,
        },
        "_selected_preset": preset_id,
    }


def build_presets() -> list[dict]:
    return [
        {
            "id": "research_build",
            "title": "Research → Build",
            "description": "Start with a researcher, then unblock a developer once the brief is clarified.",
            "config": preset_config("research_build"),
        },
        {
            "id": "single_builder",
            "title": "Single Builder",
            "description": "Use one agent to own the full implementation from brief to code.",
            "config": preset_config("single_builder"),
        },
        {
            "id": "custom_blank",
            "title": "Custom Blank",
            "description": "Start from a minimal scaffold and define the workflow yourself.",
            "config": preset_config("custom_blank"),
        },
    ]


def build_options() -> dict:
    available = detect_available_runtimes()
    return {
        "runtimes": [
            {
                "value": runtime,
                "label": runtime,
                "available": runtime in available,
            }
            for runtime in RUNTIME_MODELS
        ],
        "models": RUNTIME_MODELS,
        "thinking": [
            {"value": value, "label": value or "off"}
            for value in THINKING_LEVELS
        ],
        "roles": [
            {"value": value, "label": value or "standard"}
            for value in ROLE_OPTIONS
        ],
        "git_strategies": GIT_STRATEGIES,
    }


def normalize_focus(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = re.split(r"[\n,]", value)
        return [part.strip() for part in parts if part.strip()]
    return []


def sanitize_int(value, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, minimum)


def sanitize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def derive_dependency(blocked_until: str) -> tuple[str, str, str]:
    text = str(blocked_until or "").strip()
    if not text:
        return "immediate", "", ""
    match = AGENT_DEPENDENCY_RE.match(text)
    if match:
        target = match.group(1)
        return "after-agent", target, text
    return "custom", "", text


def resolve_blocked_until(agent: dict) -> str:
    dependency_mode = str(agent.get("dependency_mode", "") or "").strip() or None
    if dependency_mode is None and all(
        key not in agent for key in ("dependency_target", "custom_blocked_until")
    ):
        return str(agent.get("blocked_until", "") or "").strip()

    if dependency_mode == "after-agent":
        target = str(agent.get("dependency_target", "") or "").strip()
        return f"{target} sets State: done" if target else ""
    if dependency_mode == "custom":
        return str(agent.get("custom_blocked_until", "") or "").strip()
    return ""


def normalize_config(payload: dict | None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return preset_config("research_build"), ["Config payload must be an object."]

    extra_top_level = [key for key in payload if key not in KNOWN_TOP_LEVEL_KEYS and not key.startswith("_")]
    if extra_top_level:
        errors.append(
            "Unknown top-level config keys: " + ", ".join(sorted(extra_top_level))
        )

    runtime = default_runtime()
    default_supervisor_model, default_agent_model = default_models(runtime)
    normalized = preset_config("research_build")
    normalized["_selected_preset"] = str(payload.get("_selected_preset") or normalized.get("_selected_preset", "research_build"))

    project_in = copy.deepcopy(payload.get("project") or {})
    if not isinstance(project_in, dict):
        project_in = {}
        errors.append("`project` must be an object.")
    project = copy.deepcopy(project_in)
    project["name"] = str(project.get("name", "") or "").strip() or PROJECT_ROOT.name
    project["brief"] = str(project.get("brief", "") or "")
    normalized["project"] = project

    supervisor_in = copy.deepcopy(payload.get("supervisor") or {})
    if not isinstance(supervisor_in, dict):
        supervisor_in = {}
        errors.append("`supervisor` must be an object.")
    supervisor = copy.deepcopy(supervisor_in)
    supervisor["runtime"] = str(supervisor.get("runtime", "") or "").strip() or runtime
    supervisor_default_model, _ = default_models(supervisor["runtime"])
    supervisor["model"] = str(supervisor.get("model", "") or "").strip() or supervisor_default_model or default_supervisor_model
    supervisor["thinking"] = str(supervisor.get("thinking", "") or "").strip()
    supervisor["proactive_check_mins"] = sanitize_int(
        supervisor.get("proactive_check_mins", 15), 15
    )
    supervisor["zombie_heartbeat_mins"] = sanitize_int(
        supervisor.get("zombie_heartbeat_mins", 10), 10
    )
    normalized["supervisor"] = supervisor

    git_in = copy.deepcopy(payload.get("git") or {})
    if not isinstance(git_in, dict):
        git_in = {}
        errors.append("`git` must be an object.")
    git = copy.deepcopy(git_in)
    git["strategy"] = str(git.get("strategy", "") or "").strip() or "worktree"
    git["auto_pr"] = sanitize_bool(git.get("auto_pr", False))
    normalized["git"] = git

    agents_in = payload.get("agents")
    if agents_in is None:
        agents_in = []
    if not isinstance(agents_in, list):
        agents_in = []
        errors.append("`agents` must be a list.")

    agents: list[dict] = []
    seen_ids: set[str] = set()
    prior_ids: list[str] = []

    for idx, agent_payload in enumerate(agents_in):
        if not isinstance(agent_payload, dict):
            errors.append(f"Agent {idx + 1} must be an object.")
            continue

        agent = copy.deepcopy(agent_payload)
        agent["id"] = str(agent.get("id", "") or "").strip()
        agent["runtime"] = str(agent.get("runtime", "") or "").strip() or supervisor["runtime"]
        _, agent_default_model = default_models(agent["runtime"])
        agent["model"] = str(agent.get("model", "") or "").strip() or agent_default_model or default_agent_model
        agent["task"] = str(agent.get("task", "") or "")
        agent["focus"] = normalize_focus(agent.get("focus", []))
        agent["thinking"] = str(agent.get("thinking", "") or "").strip()
        agent["role"] = str(agent.get("role", "") or "").strip()
        agent["blocked_until"] = resolve_blocked_until(agent)
        dependency_mode, dependency_target, custom_blocked_until = derive_dependency(
            agent["blocked_until"]
        )
        agent["dependency_mode"] = dependency_mode
        agent["dependency_target"] = dependency_target
        agent["custom_blocked_until"] = custom_blocked_until

        if not agent["id"]:
            errors.append(f"Agent {idx + 1} is missing an id.")
        elif agent["id"] in seen_ids:
            errors.append(f"Agent id `{agent['id']}` is duplicated.")
        else:
            seen_ids.add(agent["id"])

        if not agent["task"].strip():
            label = agent["id"] or f"#{idx + 1}"
            errors.append(f"Agent `{label}` needs a task.")

        if any(not path for path in agent["focus"]):
            label = agent["id"] or f"#{idx + 1}"
            errors.append(f"Agent `{label}` has an empty focus entry.")

        if agent["dependency_mode"] == "after-agent":
            target = agent["dependency_target"]
            label = agent["id"] or f"#{idx + 1}"
            if not target:
                errors.append(f"Agent `{label}` needs a dependency target.")
            elif target not in prior_ids:
                errors.append(
                    f"Agent `{label}` must depend on an earlier agent. `{target}` is not available yet."
                )
        elif agent["dependency_mode"] == "custom":
            if not agent["custom_blocked_until"].strip():
                label = agent["id"] or f"#{idx + 1}"
                errors.append(f"Agent `{label}` needs a custom wait condition.")

        prior_ids.append(agent["id"])
        agents.append(agent)

    normalized["agents"] = agents

    if not agents:
        errors.append("Add at least one agent to the workflow.")

    return normalized, errors


def ordered_with_known_keys(source: dict, keys: list[str]) -> dict:
    ordered = {}
    for key in keys:
        if key in source:
            ordered[key] = source[key]
    for key, value in source.items():
        if key not in ordered and not key.startswith("_"):
            ordered[key] = value
    return ordered


def config_to_yaml_payload(config: dict) -> dict:
    project = ordered_with_known_keys(config.get("project", {}) or {}, ["name", "brief"])
    supervisor = ordered_with_known_keys(
        config.get("supervisor", {}) or {},
        ["runtime", "model", "thinking", "proactive_check_mins", "zombie_heartbeat_mins"],
    )
    if not str(supervisor.get("thinking", "") or "").strip():
        supervisor.pop("thinking", None)
    git = ordered_with_known_keys(config.get("git", {}) or {}, ["strategy", "auto_pr"])

    agents_out = []
    for raw_agent in config.get("agents", []) or []:
        agent = copy.deepcopy(raw_agent)
        for key in ("dependency_mode", "dependency_target", "custom_blocked_until"):
            agent.pop(key, None)
        if not str(agent.get("blocked_until", "") or "").strip():
            agent.pop("blocked_until", None)
        if not str(agent.get("thinking", "") or "").strip():
            agent.pop("thinking", None)
        if not str(agent.get("role", "") or "").strip():
            agent.pop("role", None)
        agent["focus"] = normalize_focus(agent.get("focus", []))
        agents_out.append(
            ordered_with_known_keys(
                agent,
                ["id", "runtime", "model", "task", "focus", "blocked_until", "thinking", "role"],
            )
        )

    return {
        "project": project,
        "supervisor": supervisor,
        "agents": agents_out,
        "git": git,
    }


def render_yaml(config: dict) -> str:
    payload = config_to_yaml_payload(config)
    return yaml.dump(
        payload,
        Dumper=BlockStyleDumper,
        sort_keys=False,
        allow_unicode=False,
        default_flow_style=False,
        width=100,
    )


def build_config_response() -> dict:
    existing_config = read_yaml() if config_exists() else preset_config("research_build")
    normalized_config, errors = normalize_config(existing_config)
    active_run = has_active_run(existing_config if config_exists() else normalized_config)
    yaml_preview = render_yaml(normalized_config)
    return {
        "exists": config_exists(),
        "editable": not active_run,
        "active_run": active_run,
        "has_run_state": run_state_exists(),
        "config": normalized_config,
        "yaml_preview": yaml_preview,
        "validation_errors": errors,
        "selected_preset": normalized_config.get("_selected_preset", "research_build"),
        "presets": build_presets(),
        "options": build_options(),
        "project_root": str(PROJECT_ROOT),
    }


def _fetch_model_info():
    """Fetch model context windows and pricing from APIs."""
    global _model_info_cache, _model_cache_time
    now = time.time()
    if _model_info_cache and (now - _model_cache_time) < MODEL_CACHE_TTL:
        return _model_info_cache

    info = {}

    # Anthropic Models API
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            claude_config = Path.home() / ".claude" / ".credentials.json"
            if claude_config.exists():
                creds = json.loads(claude_config.read_text())
                api_key = creds.get("claudeAiOauth", {}).get("accessToken", "")
                if not api_key:
                    api_key = creds.get("apiKey", "")
        if api_key:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-H",
                    f"x-api-key: {api_key}",
                    "-H",
                    "anthropic-version: 2023-06-01",
                    "https://api.anthropic.com/v1/models",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            for model in data.get("data", []):
                model_id = model.get("id", "")
                for name in [model_id, model_id.split("-")[1] if "-" in model_id else model_id]:
                    info[name] = {
                        "context_window": model.get("max_input_tokens", 200_000),
                        "max_output": model.get("max_tokens", 64_000),
                    }
                if "opus" in model_id:
                    info["opus"] = info[model_id]
                elif "sonnet" in model_id:
                    info["sonnet"] = info[model_id]
                elif "haiku" in model_id:
                    info["haiku"] = info[model_id]
    except Exception:
        pass

    # OpenAI Models API
    try:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            openai_auth = Path.home() / ".codex" / "auth.json"
            if openai_auth.exists():
                creds = json.loads(openai_auth.read_text())
                api_key = creds.get("api_key", creds.get("token", ""))
        if api_key and not api_key.startswith("ey"):
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-H",
                    f"Authorization: Bearer {api_key}",
                    "https://api.openai.com/v1/models",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            for model in data.get("data", []):
                model_id = model.get("id", "")
                ctx = model.get("context_window", None)
                if ctx:
                    info[model_id] = {"context_window": ctx, "max_output": 0}
    except Exception:
        pass

    defaults = {
        "opus": 1_000_000,
        "sonnet": 1_000_000,
        "haiku": 200_000,
        "claude-opus-4-6": 1_000_000,
        "claude-sonnet-4-6": 1_000_000,
        "claude-haiku-4-5": 200_000,
        "gpt-4.1-nano": 1_047_576,
        "gpt-4.1-mini": 1_047_576,
        "gpt-4.1": 1_047_576,
        "gpt-4o": 128_000,
        "gpt-4o-mini": 128_000,
        "gpt-5-nano": 1_047_576,
        "gpt-5-mini": 1_047_576,
        "gpt-5": 1_047_576,
        "gpt-5.4": 1_047_576,
        "gpt-5.3-codex-spark": 192_000,
        "gpt-5.3-codex": 192_000,
        "o3": 200_000,
        "o4-mini": 200_000,
    }
    for key, value in defaults.items():
        if key not in info:
            info[key] = {"context_window": value, "max_output": 0}

    _model_info_cache = info
    _model_cache_time = now
    return info


def get_context_window(model):
    """Get context window for a model, using API data when available."""
    info = _fetch_model_info()
    resolved = resolve_model_display("", model)
    entry = info.get(resolved) or info.get(model) or info.get(model.split("/")[-1] if "/" in model else model) or {}
    return entry.get("context_window", 200_000)


def read_status(agent_id):
    """Read agent STATUS.md into dict."""
    path = BZ_DIR / "agents" / agent_id / "STATUS.md"
    if not path.exists():
        return {}
    fields = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^([A-Za-z ]+):\s*(.+)$", line)
        if match:
            fields[match.group(1).strip().lower()] = match.group(2).strip()
    return fields


def memory_path(agent_id):
    """Return memory file path for an agent or supervisor."""
    if agent_id == "supervisor":
        return project_brain_memory_path(PROJECT_ROOT)
    return project_agent_memory_path(PROJECT_ROOT, agent_id)


def shared_memory_path():
    return project_shared_memory_path(PROJECT_ROOT)


def chatlog_path(actor_id):
    """Return chatlog path for brain/user or brain/agent conversation."""
    if actor_id == "supervisor":
        return user_brain_chatlog_path(PROJECT_ROOT)
    return brain_agent_chatlog_path(PROJECT_ROOT, actor_id)


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def read_text_tail(path: Path, max_chars: int = 20000) -> str:
    """Return bounded file content for dashboard panels."""
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if len(text) <= max_chars:
        return text
    return f"... truncated to last {max_chars} chars ...\n" + text[-max_chars:]


def read_markdown_artifact(path: Path, max_chars: int = 20000) -> dict:
    return {
        "path": _relative_path(path),
        "content": read_text_tail(path, max_chars=max_chars),
        "exists": path.exists(),
    }


def append_chatlog(path: Path, speaker: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with path.open("a") as handle:
        handle.write(f"\n## {timestamp} - {speaker}\n\n{message.strip()}\n")


def read_memory_excerpt(agent_id, lines=6):
    """Return a compact recent memory excerpt for dashboard display."""
    path = memory_path(agent_id)
    if not path.exists():
        if agent_id == "supervisor":
            path = BZ_DIR / "memory" / "brain.md"
        else:
            path = BZ_DIR / "memory" / "agents" / f"{agent_id}.md"
    if not path.exists():
        return ""

    text = path.read_text().splitlines()
    recent = []
    for line in reversed(text):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        recent.append(line)
        if len(recent) >= lines:
            break
    return " | ".join(reversed(recent))


def get_phase(state, agent_id=None):
    """Map state to standardized phase."""
    mapping = {
        "starting": "starting",
        "planning": "planning",
        "blocked": "blocked",
        "working": "coding",
        "coding": "coding",
        "executing": "executing",
        "running": "executing",
        "testing": "testing",
        "review": "ready-for-review",
        "ready-for-review": "ready-for-review",
    }

    if state in ("done", "ready-for-review", "finished") and agent_id:
        decision_file = BZ_DIR / "agents" / agent_id / "DECISION.md"
        if decision_file.exists():
            content = decision_file.read_text().lower()
            if any(word in content for word in ["accept", "complete", "proceed", "unblock", "done"]):
                return "finished"
        return "ready-for-review" if state == "ready-for-review" else "done"
    if state == "done":
        return "done"
    if state == "finished":
        return "finished"

    if agent_id and state not in ("done", "finished"):
        project_name = project_name_from_config(read_yaml())
        sess = f"bz-{project_name}-{agent_id}"
        try:
            subprocess.run(["tmux", "has-session", "-t", sess], capture_output=True, timeout=5, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return "crashed"

    return mapping.get(state, state or "unknown")


def get_token_usage(agent_id):
    """Get real token usage from CLI session files."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}

    wt = BZ_DIR / "worktrees" / agent_id
    claude_dir = None

    for check_path in [wt, PROJECT_ROOT]:
        if not check_path.exists():
            continue
        slug = str(check_path.resolve()).replace("/", "-").lstrip("-")
        candidate = Path.home() / ".claude" / "projects" / slug
        if candidate.exists():
            claude_dir = candidate
            break

    if claude_dir is None:
        claude_projects = Path.home() / ".claude" / "projects"
        if claude_projects.exists():
            for directory in claude_projects.iterdir():
                if agent_id in directory.name and "worktree" in directory.name:
                    claude_dir = directory
                    break

    if claude_dir is not None and claude_dir.exists():
        for jsonl_path in sorted(claude_dir.glob("*.jsonl"), key=os.path.getmtime, reverse=True)[:1]:
            try:
                for line in jsonl_path.read_text().splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and "usage" in msg:
                        data = msg["usage"]
                        usage["input_tokens"] += data.get("input_tokens", 0)
                        usage["output_tokens"] += data.get("output_tokens", 0)
                        usage["cache_read"] += data.get("cache_read_input_tokens", 0)
                        usage["cache_write"] += data.get("cache_creation_input_tokens", 0)
            except Exception:
                pass

    if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        config = read_yaml()
        project_name = project_name_from_config(config)
        sess = f"bz-{project_name}-{agent_id}"
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", sess, "-p", "-S", "-"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout
            match = re.search(r"tokens used\s*\n\s*([\d,]+)", output)
            if match:
                total = int(match.group(1).replace(",", ""))
                usage["input_tokens"] = int(total * 0.6)
                usage["output_tokens"] = int(total * 0.4)
            else:
                chars = len(output)
                est = chars // 4
                usage["input_tokens"] = int(est * 0.6)
                usage["output_tokens"] = int(est * 0.4)
        except Exception:
            pass

    usage["total"] = usage["input_tokens"] + usage["output_tokens"]
    usage["total_billed"] = (
        usage["input_tokens"] + usage["cache_write"] + usage["cache_read"] + usage["output_tokens"]
    )
    return usage


def estimate_cost_from_usage(usage, model):
    """Calculate cost from real token usage breakdown."""
    pricing = PRICING.get(model, {"input": 1.0, "output": 4.0})
    input_cost = usage["input_tokens"] * pricing["input"] / 1_000_000
    cache_write_cost = usage["cache_write"] * pricing["input"] / 1_000_000
    cache_read_cost = usage["cache_read"] * pricing["input"] * 0.1 / 1_000_000
    output_cost = usage["output_tokens"] * pricing["output"] / 1_000_000
    return round(input_cost + cache_write_cost + cache_read_cost + output_cost, 4)


def get_commits(agent_id):
    """Get commit count and list for agent from all branches."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--oneline", f"--grep=[{agent_id}]"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [line for line in result.stdout.strip().split("\n") if line]
    except Exception:
        return []


def resolve_model_display(runtime, model):
    """Resolve short model alias to full versioned name."""
    aliases = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
    }
    return aliases.get(model, model)


def get_thinking_mode_from_config(agent_id):
    """Read thinking level from bz.yaml for an agent."""
    try:
        config = read_yaml()
        if agent_id == "supervisor":
            return config.get("supervisor", {}).get("thinking", None)
        for agent in config.get("agents", []):
            if agent.get("id") == agent_id:
                return agent.get("thinking", None)
    except Exception:
        pass
    return None


def run_state_exists() -> bool:
    return (BZ_DIR / "project" / "state.duckdb").exists() and (BZ_DIR / "agents").exists()


def get_last_updated(status):
    """Get last updated timestamp from STATUS.md."""
    return status.get("last updated", "")


def get_message_log():
    """Build message log with role → target flow and timestamps."""
    messages = []

    log_path = BZ_DIR / "logs" / "reconcile.log"
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            match = re.match(r"\[(\w+)\]\s+(\d{2}:\d{2}:\d{2})\s+(.*)", line)
            if not match:
                continue
            source, ts, msg = match.groups()

            if "WAKE" in msg and "brain" in source:
                mode_match = re.search(r"\((\w+)\)", msg)
                mode = mode_match.group(1) if mode_match else "signal"
                messages.append(
                    {
                        "time": ts,
                        "from": "🔔 nerve",
                        "to": "🧠 brain",
                        "message": f"{mode}: {msg.split(':', 1)[-1].strip()}",
                        "type": "signal",
                    }
                )
            elif "→ 🧟" in msg:
                target_match = re.search(r"→ 🧟 (\S+):", msg)
                target = target_match.group(1) if target_match else "?"
                decision = msg.split(":", 1)[-1].strip() if ":" in msg else msg
                messages.append(
                    {
                        "time": ts,
                        "from": "🧠 brain",
                        "to": f"🧟 {target}",
                        "message": decision,
                        "type": "decision",
                    }
                )
            elif "RESPONSE" in msg:
                response = msg.replace("RESPONSE: ", "")
                target_match = re.search(r"DECISION:\s*(\S+)", response)
                target = target_match.group(1).rstrip(" —*") if target_match else ""
                messages.append(
                    {
                        "time": ts,
                        "from": "🧠 brain",
                        "to": f"🧟 {target}" if target else "",
                        "message": response[:100],
                        "type": "decision",
                    }
                )
            elif "State change" in msg:
                agents = msg.split(":")[-1].strip()
                messages.append(
                    {
                        "time": ts,
                        "from": f"🧟 {agents.strip()}",
                        "to": "🔔 nerve",
                        "message": "STATUS.md changed",
                        "type": "state",
                    }
                )

    fb_path = BZ_DIR / "logs" / "feedback.log"
    if fb_path.exists():
        for line in fb_path.read_text().splitlines():
            parts = line.split(" | ", 2)
            if len(parts) >= 3:
                ts = parts[0][-8:] if len(parts[0]) > 8 else parts[0]
                target = parts[1].replace("target=", "")
                messages.append(
                    {
                        "time": ts,
                        "from": "👤 human",
                        "to": f"🧟 {target}",
                        "message": parts[2][:80],
                        "type": "feedback",
                    }
                )

    try:
        config = read_yaml()
        agent_ids = {agent.get("id", "") for agent in config.get("agents", [])}
        branch_args = []
        for aid in agent_ids:
            branch_args.extend(["--glob", f"refs/heads/bz/{aid}"])
        branch_args.append("HEAD")

        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--format=%ct %s"] + branch_args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            ts = datetime.fromtimestamp(int(parts[0])).strftime("%H:%M:%S")
            message = parts[1]
            commit_match = re.match(r"\[(\w+[-\w]*)\]\s*(.*)", message)
            if commit_match and commit_match.group(1) in agent_ids:
                messages.append(
                    {
                        "time": ts,
                        "from": f"🧟 {commit_match.group(1)}",
                        "to": "📁 git",
                        "message": commit_match.group(2),
                        "type": "commit",
                    }
                )
    except Exception:
        pass

    messages.sort(key=lambda row: row.get("time", ""))
    return messages


def build_dashboard_data():
    """Build complete dashboard payload."""
    config = read_yaml()
    project = config.get("project", {})
    supervisor = config.get("supervisor", {})
    agents_config = config.get("agents", [])

    agent_ids = {agent.get("id", "") for agent in agents_config}
    branch_args = []
    for aid in agent_ids:
        branch_args.extend(["--glob", f"refs/heads/bz/{aid}"])

    start_time = None
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--reverse", "--format=%ct", "-1"] + branch_args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            start_time = int(result.stdout.strip())
    except Exception:
        pass

    total_commits = 0
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--oneline"] + branch_args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        total_commits = len([line for line in result.stdout.strip().split("\n") if line])
    except Exception:
        pass

    zombies = []
    total_tokens = 0
    total_cost = 0.0

    for agent_config in agents_config:
        aid = agent_config.get("id", "")
        model = agent_config.get("model", "")
        runtime = agent_config.get("runtime", "")
        status = read_status(aid)
        state = status.get("state", "unknown")
        action = status.get("action", "")
        depends_on = status.get("depends on", "none")
        needs_brain = status.get("needs brain", "no")
        phase = get_phase(state, aid)
        usage = get_token_usage(aid)
        cost = estimate_cost_from_usage(usage, model)
        commits = get_commits(aid)
        context_max = get_context_window(model)
        context_pct = round(usage["total"] / context_max * 100, 1) if context_max else 0

        total_tokens += usage.get("total_billed", usage["total"])
        total_cost += cost

        model_display = resolve_model_display(runtime, model)
        thinking_mode = get_thinking_mode_from_config(aid)
        last_updated = get_last_updated(status)
        file_list = status.get("files touched", "none")
        file_count = (
            len([item for item in file_list.split(",") if item.strip() and item.strip() != "none"])
            if file_list != "none"
            else 0
        )

        zombie_entry = {
            "id": aid,
            "runtime": runtime,
            "model": model,
            "model_display": model_display,
            "thinking_mode": thinking_mode,
            "phase": phase,
            "state": state,
            "action": action,
            "summary": status.get("summary", "No summary"),
            "files": file_list,
            "file_count": file_count,
            "depends_on": depends_on,
            "needs_brain": needs_brain,
            "next_step": status.get("next step", ""),
            "blocker": status.get("blocker", "none"),
            "last_updated": last_updated,
            "memory_path": status.get("memory", _relative_path(memory_path(aid))),
            "memory_excerpt": read_memory_excerpt(aid),
            "memory": read_markdown_artifact(memory_path(aid)),
            "chatlog": read_markdown_artifact(chatlog_path(aid)),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read": usage["cache_read"],
            "cache_write": usage["cache_write"],
            "total_tokens": usage["total"],
            "context_max": context_max,
            "context_pct": context_pct,
            "estimated_cost": cost,
            "commit_count": len(commits),
            "commits": commits[:5],
            "latest_message": status.get("summary", ""),
        }

        if agent_config.get("role") == "iterator":
            iterate_ledger = _load_iterate_ledger(aid)
            if iterate_ledger:
                zombie_entry["iterate"] = iterate_ledger

        zombies.append(zombie_entry)

    brain_usage = (
        get_token_usage("supervisor")
        if (BZ_DIR / "agents" / "supervisor").exists()
        else {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    )
    brain_tokens = brain_usage["total"]
    brain_cost = estimate_cost_from_usage(brain_usage, supervisor.get("model", ""))
    brain_requests = sum(
        1 for zombie in zombies if (zombie.get("needs_brain", "") or "").lower() not in ("", "no", "none")
    )

    all_finished = all(zombie["phase"] == "finished" for zombie in zombies) if zombies else False
    zombies_done = sum(1 for zombie in zombies if zombie["phase"] in ("done", "finished"))

    end_time = time.time()
    if all_finished:
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--format=%ct", "-1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip():
                end_time = int(result.stdout.strip())
        except Exception:
            pass
    elapsed = int(end_time - start_time) if start_time else 0

    supervisor_thinking = get_thinking_mode_from_config("supervisor")
    supervisor_model_display = resolve_model_display(supervisor.get("runtime", ""), supervisor.get("model", ""))

    return {
        "project": {
            "name": project.get("name", PROJECT_ROOT.name),
            "state": "complete" if all_finished else "active",
            "zombies_total": len(zombies),
            "zombies_done": zombies_done,
            "total_commits": total_commits,
            "elapsed_seconds": elapsed,
            "elapsed_display": f"{elapsed // 60}m {elapsed % 60}s",
            "total_cost": round(brain_cost + total_cost, 4),
        },
        "brain": {
            "runtime": supervisor.get("runtime", ""),
            "model": supervisor.get("model", ""),
            "model_display": supervisor_model_display,
            "thinking_mode": supervisor_thinking,
            "input_tokens": brain_usage["input_tokens"],
            "output_tokens": brain_usage["output_tokens"],
            "cache_read": brain_usage.get("cache_read", 0),
            "cache_write": brain_usage.get("cache_write", 0),
            "tokens": brain_usage.get("total_billed", brain_tokens),
            "estimated_cost": brain_cost,
            "status": "attention" if brain_requests else ("idle" if all(zombie.get("state") == "done" for zombie in zombies) else "monitoring"),
            "active_requests": brain_requests,
            "memory_path": _relative_path(memory_path("supervisor")),
            "memory_excerpt": read_memory_excerpt("supervisor", lines=8),
            "memory": read_markdown_artifact(memory_path("supervisor")),
            "shared_memory": read_markdown_artifact(shared_memory_path()),
            "chatlog": read_markdown_artifact(chatlog_path("supervisor")),
        },
        "zombies": zombies,
        "cost": {
            "brain": brain_cost,
            "zombies": total_cost,
            "total": round(brain_cost + total_cost, 4),
            "total_tokens": total_tokens + brain_tokens,
        },
        "messages": get_message_log(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


def _load_iterate_ledger(agent_id: str) -> dict | None:
    """Load iterate ledger for an iterator agent."""
    for base in [PROJECT_ROOT / ".bz" / "worktrees" / agent_id, PROJECT_ROOT]:
        ledger_path = base / ".bz" / "iterate" / "ledger.json"
        if ledger_path.exists():
            try:
                data = json.loads(ledger_path.read_text())
                champion = data.get("champion", {})
                return {
                    "goal": data.get("goal", {}),
                    "baseline": data.get("baseline", {}),
                    "champion_iteration": champion.get("iteration", 0),
                    "champion_metrics": champion.get("metrics", {}),
                    "used": data.get("budget", {}).get("used", 0),
                    "budget": data.get("budget", {}).get("max", 0),
                    "state": (
                        "complete"
                        if data.get("budget", {}).get("used", 0) >= data.get("budget", {}).get("max", 0)
                        else "iterating"
                    ),
                    "zombie_model": "sonnet",
                    "iterations": [
                        {
                            "id": item.get("id"),
                            "hypothesis": item.get("hypothesis", ""),
                            "metrics": item.get("metrics", {}),
                            "verdict": item.get("verdict", ""),
                            "kept": item.get("kept", False),
                            "duration_sec": item.get("duration_sec", 0),
                        }
                        for item in data.get("iterations", [])
                    ],
                }
            except (json.JSONDecodeError, KeyError):
                return None
    return None


def session_names_for_project(project_name: str) -> list[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return [name for name in result.stdout.strip().splitlines() if name.startswith(f"bz-{project_name}-")]
    except Exception:
        return []


def service_session_name(service: str, config: dict | None = None) -> str:
    return f"bz-{project_name_from_config(config or read_yaml())}-{service}"


def tmux_session_exists(session_name: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def reconcile_running() -> bool:
    pid_file = BZ_DIR / "reconcile.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, ValueError, OSError):
            pass
    return tmux_session_exists(service_session_name("nerve"))


def has_active_run(config: dict | None = None) -> bool:
    project_name = project_name_from_config(config or read_yaml())
    return reconcile_running() or bool(session_names_for_project(project_name))


def _handle_teardown():
    """Kill all tmux sessions and reconcile loop for this project."""
    killed = []
    project_name = project_name_from_config(read_yaml())

    pid_file = BZ_DIR / "reconcile.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 9)
            killed.append(f"reconcile (PID {pid})")
        except (ProcessLookupError, ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)

    for sess in session_names_for_project(project_name):
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
        killed.append(f"session: {sess}")

    return {"status": "ok", "killed": killed}


def _parse_request_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc


def _payload_to_normalized_config(payload: dict) -> tuple[dict, list[str]]:
    if "raw_yaml" in payload and str(payload.get("raw_yaml", "")).strip():
        try:
            parsed = yaml.safe_load(payload.get("raw_yaml", "")) or {}
        except yaml.YAMLError as exc:
            return preset_config("research_build"), [f"YAML parse error: {exc}"]
        return normalize_config(parsed)

    candidate = payload.get("config", payload)
    return normalize_config(candidate)


def _validate_request(payload: dict) -> tuple[dict, int]:
    normalized, errors = _payload_to_normalized_config(payload)
    response = {
        "valid": not errors,
        "errors": errors,
        "normalized_config": normalized,
        "yaml_preview": render_yaml(normalized),
    }
    return response, (200 if not errors else 400)


def _save_config(payload: dict) -> tuple[dict, int]:
    current = read_yaml() if config_exists() else None
    if has_active_run(current):
        return {"status": "error", "errors": ["Workflow editing is locked while a run is active. Teardown first."]}, 409

    normalized, errors = _payload_to_normalized_config(payload)
    if errors:
        return {
            "status": "error",
            "errors": errors,
            "normalized_config": normalized,
            "yaml_preview": render_yaml(normalized),
        }, 400

    CONFIG_PATH.write_text(render_yaml(normalized))
    return {
        "status": "ok",
        "saved": True,
        "normalized_config": normalized,
        "yaml_preview": render_yaml(normalized),
        "config_path": str(CONFIG_PATH),
    }, 200


def _launch_workflow(payload: dict) -> tuple[dict, int]:
    if has_active_run(read_yaml() if config_exists() else None):
        return {"status": "error", "errors": ["A workflow is already running. Teardown before launching again."]}, 409

    if payload:
        save_response, save_status = _save_config(payload)
        if save_status != 200:
            return save_response, save_status
    elif not config_exists():
        return {"status": "error", "errors": ["No `bz.yaml` exists yet. Save the workflow first."]}, 400

    try:
        result = subprocess.run(
            [str(BZ_EXECUTABLE), "launch", "--no-dashboard"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return {"status": "error", "errors": [f"Launch failed: {exc}"]}, 500

    if result.returncode != 0:
        return {
            "status": "error",
            "errors": [result.stderr.strip() or "Launch failed."],
            "stdout": result.stdout,
            "stderr": result.stderr,
        }, 500

    return {
        "status": "ok",
        "launched": True,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "active_run": has_active_run(read_yaml()),
    }, 200


def _brain_cli_command(runtime: str, model: str, prompt: str) -> list[str]:
    if runtime in ("claude", "claude-code"):
        cmd = ["claude", "--dangerously-skip-permissions"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-p", prompt])
        return cmd
    if runtime == "codex":
        cmd = ["codex", "exec", "--full-auto"]
        if model:
            cmd.extend(["--model", model])
        cmd.append(prompt)
        return cmd
    cmd = [runtime]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def _ensure_reconcile_running() -> bool:
    if reconcile_running():
        return False
    (BZ_DIR / "logs").mkdir(parents=True, exist_ok=True)
    pid_file = BZ_DIR / "reconcile.pid"
    log_file = BZ_DIR / "logs" / "reconcile.log"
    session = service_session_name("nerve")
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    cmd = (
        f"cd {shlex.quote(str(PROJECT_ROOT))} && "
        f"echo $$ > {shlex.quote(str(pid_file))} && "
        f"exec bash {shlex.quote(str(LIB_DIR / 'reconcile.sh'))} {shlex.quote(str(PROJECT_ROOT))} "
        f">> {shlex.quote(str(log_file))} 2>&1"
    )
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "160", "-y", "40", cmd],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Failed to start reconcile tmux session")
    return True


def _agent_config(agent_id: str) -> dict:
    for agent in (read_yaml().get("agents", []) or []):
        if agent.get("id") == agent_id:
            return agent
    return {}


def _session_name(agent_id: str) -> str:
    return f"bz-{project_name_from_config(read_yaml())}-{agent_id}"


def _cli_alive(session_name: str) -> bool:
    try:
        subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True, timeout=5, check=True)
        pane = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip().splitlines()
        if not pane:
            return False
        return subprocess.run(
            ["pgrep", "-P", pane[0], "-f", "claude|codex|aider"],
            capture_output=True,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def _resume_target_from_brain_chat(agent_id: str) -> None:
    latest = control_plane.latest_pending_action(PROJECT_ROOT, agent_id)
    kind = (latest or {}).get("kind", "guidance")
    summary = (latest or {}).get("summary", "Brain queued follow-up work.")
    phase = "blocked" if kind == "hold" else "working"
    control_plane.write_state(
        PROJECT_ROOT,
        agent_id=agent_id,
        phase=phase,
        action="resuming from brain chat",
        summary=summary,
        depends_on=["human"],
        needs_brain="no",
        next_step=f"Read .bz/control/contexts/{agent_id}.md and .bz/control/agents/{agent_id}/latest-action.md.",
        blocker="brain hold" if kind == "hold" else "none",
        files_touched=[],
        updated_by="brain",
        source="brain-chat",
    )


def _deliver_action_to_agent(agent_id: str) -> str:
    session = _session_name(agent_id)
    if _cli_alive(session):
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                session,
                f"NEW BRAIN CHAT ACTION queued. Read .bz/control/contexts/{agent_id}.md and .bz/control/agents/{agent_id}/latest-action.md now, then act.",
                "Enter",
            ],
            capture_output=True,
        )
        return "notified"

    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    agent = _agent_config(agent_id)
    runtime = agent.get("runtime", "claude")
    runtime = "claude" if runtime in ("claude", "claude-code") else runtime
    model = agent.get("model", "")
    work_dir = BZ_DIR / "worktrees" / agent_id
    if not work_dir.exists():
        work_dir = PROJECT_ROOT
    prompt = (
        "NEW BRAIN CHAT ACTION queued.\n\n"
        f"Read .bz/control/contexts/{agent_id}.md and .bz/control/agents/{agent_id}/latest-action.md first.\n"
        "Then execute the action immediately. Update state, task events, and memory as you work."
    )
    cmd = " ".join([repr(part) for part in _brain_cli_command(runtime, model, prompt)])
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-x", "200", "-y", "50"], capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", session, f"cd {repr(str(work_dir.resolve()))} && {cmd}", "Enter"], capture_output=True)
    return "restarted"


def _handle_brain_chat(payload: dict) -> tuple[dict, int]:
    message = str(payload.get("message", "") or "").strip()
    if not message:
        return {"status": "error", "errors": ["Message is required."]}, 400
    if not config_exists():
        return {"status": "error", "errors": ["No `bz.yaml` exists yet."]}, 400

    append_chatlog(chatlog_path("supervisor"), "User", message)
    control_plane.append_event(
        PROJECT_ROOT,
        event_type="user_brain_chat",
        source="human",
        target="brain",
        summary=message[:180],
        details=message,
    )

    config = read_yaml()
    supervisor = config.get("supervisor", {}) or {}
    runtime = supervisor.get("runtime", "claude")
    runtime = "claude" if runtime in ("claude", "claude-code") else runtime
    model = supervisor.get("model", "sonnet")
    context = control_plane.build_brain_context(PROJECT_ROOT)
    chatlog = read_text_tail(chatlog_path("supervisor"), max_chars=12000)
    prompt = f"""You are the brain supervisor for this brain-zombies project.

The user sent a message to the brain UI. Answer the user and, if needed, resume project work by queueing concrete actions for zombies.

## User Message
{message}

## Brain Context
{context}

## User-Brain Chatlog
{chatlog}

Return exactly one JSON object, with no markdown fences:
{{
  "reply": "short direct reply to the user",
  "brain_state": {{
    "phase": "monitoring",
    "action": "short brain action",
    "summary": "short summary",
    "depends_on": [],
    "needs_brain": "no",
    "next_step": "wait for next signal or queued zombie work",
    "blocker": "none"
  }},
  "brain_memory": [
    {{
      "scope": "private or shared",
      "kind": "decision | handoff | constraint | result | observation",
      "summary": "durable memory in one line",
      "details": "compact durable detail",
      "tags": ["optional-tag"],
      "related_agents": ["optional-agent-id"]
    }}
  ],
  "actions": [
    {{
      "to": "agent-id",
      "kind": "feedback | redirect | unblock | restart | hold | status-check",
      "summary": "one-line instruction",
      "details": "specific action the zombie should take next",
      "reason": "why this action is needed"
    }}
  ]
}}

Rules:
- Use actions=[] if no zombie work is needed.
- If the project was finished but the user asks for more work, queue actions for the relevant zombies.
- Keep reply concise and operational.
"""
    try:
        result = subprocess.run(
            _brain_cli_command(runtime, model, prompt),
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return {"status": "error", "errors": [f"Brain runtime `{runtime}` is not installed."]}, 500
    except Exception as exc:
        return {"status": "error", "errors": [f"Brain chat failed: {exc}"]}, 500

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return {"status": "error", "errors": [output[-1200:] or "Brain chat failed."]}, 500

    payload_json = control_plane.extract_json_payload(output) or {}
    reply = str(payload_json.get("reply") or "").strip() or "I received the message."
    append_chatlog(chatlog_path("supervisor"), "Brain", reply)

    queued_targets = control_plane.ingest_brain_output(PROJECT_ROOT, output, mode="user-chat", reason=message)
    delivery = {}
    for target in queued_targets:
        _resume_target_from_brain_chat(target)
        delivery[target] = _deliver_action_to_agent(target)

    reconcile_started = False
    if queued_targets:
        reconcile_started = _ensure_reconcile_running()

    return {
        "status": "ok",
        "reply": reply,
        "queued_targets": queued_targets,
        "delivery": delivery,
        "reconcile_started": reconcile_started,
    }, 200


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/status":
            self._send_json(build_dashboard_data())
        elif path == "/api/config":
            self._send_json(build_config_response())
        elif path in ("/", "/index.html"):
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            payload = _parse_request_body(self)
        except ValueError as exc:
            self._send_json({"status": "error", "errors": [str(exc)]}, 400)
            return

        if path == "/api/config/validate":
            response, status = _validate_request(payload)
            self._send_json(response, status)
        elif path == "/api/config/save":
            response, status = _save_config(payload)
            self._send_json(response, status)
        elif path == "/api/launch":
            response, status = _launch_workflow(payload)
            self._send_json(response, status)
        elif path == "/api/teardown":
            self._send_json(_handle_teardown())
        elif path == "/api/brain/chat":
            response, status = _handle_brain_chat(payload)
            self._send_json(response, status)
        else:
            self._send_json({"status": "error", "errors": ["Not found."]}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"🧠🧟 Dashboard at http://localhost:{PORT}")
    print(f"Project: {PROJECT_ROOT}")
    server = http.server.HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
