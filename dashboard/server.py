#!/usr/bin/env python3
"""brain-zombies dashboard server — lightweight status API + static HTML."""

import http.server
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3333
PROJECT_ROOT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path.cwd()
BZ_DIR = PROJECT_ROOT / ".bz"
DASHBOARD_DIR = Path(__file__).parent

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
            # Try reading from claude config
            claude_config = Path.home() / ".claude" / ".credentials.json"
            if claude_config.exists():
                creds = json.loads(claude_config.read_text())
                api_key = creds.get("claudeAiOauth", {}).get("accessToken", "")
                if not api_key:
                    api_key = creds.get("apiKey", "")
        if api_key:
            result = subprocess.run(
                ["curl", "-s", "-H", f"x-api-key: {api_key}",
                 "-H", "anthropic-version: 2023-06-01",
                 "https://api.anthropic.com/v1/models"],
                capture_output=True, text=True, timeout=10
            )
            data = json.loads(result.stdout)
            for m in data.get("data", []):
                model_id = m.get("id", "")
                # Map both full ID and alias
                for name in [model_id, model_id.split("-")[1] if "-" in model_id else model_id]:
                    info[name] = {
                        "context_window": m.get("max_input_tokens", 200_000),
                        "max_output": m.get("max_tokens", 64_000),
                    }
                # Also map short aliases: opus, sonnet, haiku
                if "opus" in model_id:
                    info["opus"] = info[model_id]
                elif "sonnet" in model_id and model_id not in info.get("sonnet", {}).get("_id", ""):
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
        if api_key and not api_key.startswith("ey"):  # skip OAuth tokens
            result = subprocess.run(
                ["curl", "-s", "-H", f"Authorization: Bearer {api_key}",
                 "https://api.openai.com/v1/models"],
                capture_output=True, text=True, timeout=10
            )
            data = json.loads(result.stdout)
            for m in data.get("data", []):
                model_id = m.get("id", "")
                ctx = m.get("context_window", None)
                if ctx:
                    info[model_id] = {"context_window": ctx, "max_output": 0}
    except Exception:
        pass

    # Fallback defaults for common models if API calls failed
    defaults = {
        "opus": 1_000_000, "sonnet": 1_000_000, "haiku": 200_000,
        "claude-opus-4-6": 1_000_000, "claude-sonnet-4-6": 1_000_000,
        "claude-haiku-4-5": 200_000,
        "gpt-4.1-nano": 1_047_576, "gpt-4.1-mini": 1_047_576, "gpt-4.1": 1_047_576,
        "gpt-4o": 128_000, "gpt-4o-mini": 128_000,
        "gpt-5-nano": 1_047_576, "gpt-5-mini": 1_047_576, "gpt-5": 1_047_576,
        "gpt-5.4": 1_047_576,
        "gpt-5.3-codex-spark": 192_000, "gpt-5.3-codex": 192_000,
        "o3": 200_000, "o4-mini": 200_000,
    }
    for k, v in defaults.items():
        if k not in info:
            info[k] = {"context_window": v, "max_output": 0}

    _model_info_cache = info
    _model_cache_time = now
    return info


def get_context_window(model):
    """Get context window for a model, using API data when available."""
    info = _fetch_model_info()
    entry = info.get(model, info.get(model.split("/")[-1] if "/" in model else model, {}))
    return entry.get("context_window", 200_000)


def read_yaml():
    """Read bz.yaml config."""
    try:
        import yaml
        with open(PROJECT_ROOT / "bz.yaml") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def read_status(agent_id):
    """Read agent STATUS.md into dict."""
    path = BZ_DIR / "agents" / agent_id / "STATUS.md"
    if not path.exists():
        return {}
    fields = {}
    for line in path.read_text().splitlines():
        m = re.match(r'^([A-Za-z ]+):\s*(.+)$', line)
        if m:
            fields[m.group(1).strip().lower()] = m.group(2).strip()
    return fields


def get_phase(state):
    """Map state to standardized phase."""
    mapping = {
        "starting": "starting",
        "planning": "planning",
        "blocked": "blocked",
        "working": "coding",
        "coding": "coding",
        "testing": "testing",
        "review": "review",
        "ready-for-review": "review",
        "done": "done",
    }
    return mapping.get(state, state or "unknown")


def get_health(agent_id, state):
    """Determine health gate from tmux + git state."""
    config = read_yaml()
    project_name = config.get("project", {}).get("name", "unknown")
    sess = f"bz-{project_name}-{agent_id}"

    if state == "done":
        return "healthy"

    # Check tmux alive
    try:
        subprocess.run(["tmux", "has-session", "-t", sess],
                       capture_output=True, timeout=5, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "crashed"

    # Check for CLI process
    try:
        pane_pid = subprocess.run(
            ["tmux", "list-panes", "-t", sess, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip().split("\n")[0]
        if pane_pid:
            result = subprocess.run(
                ["pgrep", "-P", pane_pid, "-f", "claude|codex|aider"],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                return "idle"
    except Exception:
        pass

    # Check last commit time in worktree
    wt = BZ_DIR / "worktrees" / agent_id
    if wt.exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(wt), "log", "-1", "--format=%ct"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                last_commit = int(result.stdout.strip())
                elapsed = time.time() - last_commit
                if elapsed > 600:
                    return "stuck"
                elif elapsed > 300:
                    return "slow"
        except Exception:
            pass

    return "healthy"


def get_token_usage(agent_id):
    """Get real token usage from CLI session files.

    Returns dict with input_tokens, output_tokens, cache_read, cache_write, total, cost.
    Falls back to tmux pane estimation if session files unavailable.
    """
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0}

    # Try Claude Code session JSONL (~/.claude/projects/<slug>/*.jsonl)
    # Claude slugifies paths: /home/user/foo → -home-user-foo
    wt = BZ_DIR / "worktrees" / agent_id
    claude_dir = None

    # Check worktree path first, then project root
    for check_path in [wt, PROJECT_ROOT]:
        if not check_path.exists():
            continue
        slug = str(check_path.resolve()).replace("/", "-").lstrip("-")
        candidate = Path.home() / ".claude" / "projects" / slug
        if candidate.exists():
            claude_dir = candidate
            break

    if claude_dir is None:
        # Fallback: search for matching project dirs
        claude_projects = Path.home() / ".claude" / "projects"
        if claude_projects.exists():
            for d in claude_projects.iterdir():
                if agent_id in d.name and "worktree" in d.name:
                    claude_dir = d
                    break
    if claude_dir is not None and claude_dir.exists():
        for jsonl in sorted(claude_dir.glob("*.jsonl"), key=os.path.getmtime, reverse=True)[:1]:
            try:
                for line in jsonl.read_text().splitlines():
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and "usage" in msg:
                        u = msg["usage"]
                        usage["input_tokens"] += u.get("input_tokens", 0)
                        usage["output_tokens"] += u.get("output_tokens", 0)
                        usage["cache_read"] += u.get("cache_read_input_tokens", 0)
                        usage["cache_write"] += u.get("cache_creation_input_tokens", 0)
            except Exception:
                pass

    # Try Codex: parse "tokens used\nN" from tmux output
    if usage["input_tokens"] == 0 and usage["output_tokens"] == 0:
        config = read_yaml()
        project_name = config.get("project", {}).get("name", "unknown")
        sess = f"bz-{project_name}-{agent_id}"
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", sess, "-p", "-S", "-"],
                capture_output=True, text=True, timeout=5
            )
            output = result.stdout
            # Codex prints "tokens used\nN,NNN"
            m = re.search(r'tokens used\s*\n\s*([\d,]+)', output)
            if m:
                total = int(m.group(1).replace(",", ""))
                usage["input_tokens"] = int(total * 0.6)
                usage["output_tokens"] = int(total * 0.4)
            else:
                # Fallback: rough estimate from pane size
                chars = len(output)
                est = chars // 4
                usage["input_tokens"] = int(est * 0.6)
                usage["output_tokens"] = int(est * 0.4)
        except Exception:
            pass

    usage["total"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def estimate_cost_from_usage(usage, model):
    """Calculate cost from real token usage breakdown."""
    pricing = PRICING.get(model, {"input": 1.0, "output": 4.0})
    input_cost = usage["input_tokens"] * pricing["input"] / 1_000_000
    output_cost = usage["output_tokens"] * pricing["output"] / 1_000_000
    # Cache reads are typically 90% cheaper
    cache_cost = usage["cache_read"] * pricing["input"] * 0.1 / 1_000_000
    # Cache writes are 25% more expensive
    cache_write_cost = usage["cache_write"] * pricing["input"] * 1.25 / 1_000_000
    total = input_cost + output_cost + cache_cost + cache_write_cost
    return round(total, 4)


def get_commits(agent_id):
    """Get commit count and list for agent from all branches."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--oneline",
             f"--grep=[{agent_id}]"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.strip().split("\n") if l]
        return lines
    except Exception:
        return []


def resolve_model_display(runtime, model):
    """Resolve short model alias to full versioned name."""
    aliases = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5",
    }
    full = aliases.get(model, model)
    return full


def get_last_updated(status):
    """Get last updated timestamp from STATUS.md."""
    return status.get("last updated", "")


def get_message_log():
    """Build message log with role → target flow and timestamps."""
    messages = []

    # From reconcile log — parse nerve signals and brain wakes
    log_path = BZ_DIR / "logs" / "reconcile.log"
    if log_path.exists():
        for line in log_path.read_text().splitlines()[-50:]:
            # [nerve] HH:MM:SS State change: agent_name
            m = re.match(r'\[(\w+)\]\s+(\d{2}:\d{2}:\d{2})\s+(.*)', line)
            if m:
                source = m.group(1)
                ts = m.group(2)
                msg = m.group(3)

                if "WAKE" in msg and "brain" in source:
                    mode_m = re.search(r'\((\w+)\)', msg)
                    mode = mode_m.group(1) if mode_m else "signal"
                    messages.append({
                        "time": ts,
                        "from": "🔔 nerve",
                        "to": "🧠 brain",
                        "message": f"{mode}: {msg.split(':',1)[-1].strip()}",
                        "type": "signal",
                    })
                elif "→ 🧟" in msg:
                    # Brain decision to zombie
                    target_m = re.search(r'→ 🧟 (\S+):', msg)
                    target = target_m.group(1) if target_m else "?"
                    decision = msg.split(":", 1)[-1].strip() if ":" in msg else msg
                    messages.append({
                        "time": ts,
                        "from": "🧠 brain",
                        "to": f"🧟 {target}",
                        "message": decision,
                        "type": "decision",
                    })
                elif "RESPONSE" in msg:
                    messages.append({
                        "time": ts,
                        "from": "🧠 brain",
                        "to": "",
                        "message": msg.replace("RESPONSE: ", "")[:100],
                        "type": "brain",
                    })
                elif "State change" in msg:
                    agents = msg.split(":")[-1].strip()
                    messages.append({
                        "time": ts,
                        "from": f"🧟 {agents.strip()}",
                        "to": "🔔 nerve",
                        "message": "STATUS.md changed",
                        "type": "state",
                    })

    # From feedback log
    fb_path = BZ_DIR / "logs" / "feedback.log"
    if fb_path.exists():
        for line in fb_path.read_text().splitlines()[-20:]:
            parts = line.split(" | ", 2)
            if len(parts) >= 3:
                ts = parts[0][-8:] if len(parts[0]) > 8 else parts[0]  # HH:MM:SS
                target = parts[1].replace("target=", "")
                messages.append({
                    "time": ts,
                    "from": "👤 human",
                    "to": f"🧟 {target}",
                    "message": parts[2][:80],
                    "type": "feedback",
                })

    # From git log (all branches)
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all",
             "--format=%ct %s", "--since=2 hours ago"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                ts = datetime.fromtimestamp(int(parts[0])).strftime("%H:%M:%S")
                msg = parts[1]
                cm = re.match(r'\[(\w+[-\w]*)\]\s*(.*)', msg)
                if cm:
                    messages.append({
                        "time": ts,
                        "from": f"🧟 {cm.group(1)}",
                        "to": "📁 git",
                        "message": cm.group(2),
                        "type": "commit",
                    })
    except Exception:
        pass

    messages.sort(key=lambda m: m.get("time", ""))
    return messages[-100:]


def build_dashboard_data():
    """Build complete dashboard payload."""
    config = read_yaml()
    project = config.get("project", {})
    supervisor = config.get("supervisor", {})
    agents_config = config.get("agents", [])

    # Project info
    start_time = None
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--reverse",
             "--format=%ct", "-1"],
            capture_output=True, text=True, timeout=5
        )
        if result.stdout.strip():
            start_time = int(result.stdout.strip())
    except Exception:
        pass

    total_commits = 0
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--oneline"],
            capture_output=True, text=True, timeout=5
        )
        total_commits = len([l for l in result.stdout.strip().split("\n") if l])
    except Exception:
        pass

    # Zombies
    zombies = []
    total_tokens = 0
    total_cost = 0.0

    for ac in agents_config:
        aid = ac.get("id", "")
        model = ac.get("model", "")
        runtime = ac.get("runtime", "")
        status = read_status(aid)
        state = status.get("state", "unknown")
        phase = get_phase(state)
        health = get_health(aid, state)
        usage = get_token_usage(aid)
        cost = estimate_cost_from_usage(usage, model)
        commits = get_commits(aid)
        context_max = get_context_window(model)
        context_pct = round(usage["total"] / context_max * 100, 1) if context_max else 0

        total_tokens += usage["total"]
        total_cost += cost

        model_display = resolve_model_display(runtime, model)
        last_updated = get_last_updated(status)
        file_list = status.get("files touched", "none")
        file_count = len([f for f in file_list.split(",") if f.strip() and f.strip() != "none"]) if file_list != "none" else 0

        zombies.append({
            "id": aid,
            "runtime": runtime,
            "model": model,
            "model_display": model_display,
            "phase": phase,
            "health": health,
            "state": state,
            "summary": status.get("summary", "No summary"),
            "files": file_list,
            "file_count": file_count,
            "next_step": status.get("next step", ""),
            "blocker": status.get("blocker", "none"),
            "last_updated": last_updated,
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
        })

    # Brain stats
    brain_usage = get_token_usage("supervisor") if (BZ_DIR / "agents" / "supervisor").exists() else {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "total": 0}
    brain_tokens = brain_usage["total"]
    brain_cost = estimate_cost_from_usage(brain_usage, supervisor.get("model", ""))

    # All done?
    all_done = all(z["phase"] == "done" for z in zombies) if zombies else False
    zombies_done = sum(1 for z in zombies if z["phase"] == "done")

    # Elapsed: stop counting when all done (use last commit time as end)
    end_time = time.time()
    if all_done:
        try:
            result = subprocess.run(
                ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--format=%ct", "-1"],
                capture_output=True, text=True, timeout=5
            )
            if result.stdout.strip():
                end_time = int(result.stdout.strip())
        except Exception:
            pass
    elapsed = int(end_time - start_time) if start_time else 0

    supervisor_model_display = resolve_model_display(
        supervisor.get("runtime", ""), supervisor.get("model", ""))

    return {
        "project": {
            "name": project.get("name", "unknown"),
            "state": "complete" if all_done else "active",
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
            "tokens": brain_tokens,
            "estimated_cost": brain_cost,
            "status": "idle" if all_done else "monitoring",
        },
        "zombies": zombies,
        "cost": {
            "brain": brain_cost,
            "zombies": total_cost,
            "total": round(brain_cost + total_cost, 4),
        },
        "messages": get_message_log(),
        "updated_at": datetime.now().strftime("%H:%M:%S"),
    }


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def do_GET(self):
        if self.path == "/api/status":
            data = build_dashboard_data()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def log_message(self, format, *args):
        pass  # Suppress request logs


if __name__ == "__main__":
    print(f"🧠🧟 Dashboard at http://localhost:{PORT}")
    print(f"Project: {PROJECT_ROOT}")
    server = http.server.HTTPServer(("127.0.0.1", PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
