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


# Model context window sizes
CONTEXT_WINDOWS = {
    "haiku": 200_000,
    "sonnet": 200_000,
    "opus": 200_000,
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
    "gpt-5.2-codex": 192_000,
    "gpt-5.1-codex": 192_000,
    "gpt-5.1-codex-mini": 192_000,
    "gpt-5.1-codex-max": 512_000,
    "o3": 200_000,
    "o4-mini": 200_000,
}


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
    if claude_dir.exists():
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
    """Get commit count and list for agent."""
    wt = BZ_DIR / "worktrees" / agent_id
    if not wt.exists():
        wt = PROJECT_ROOT
    try:
        result = subprocess.run(
            ["git", "-C", str(wt), "log", "--oneline", f"--grep=[{agent_id}]"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.strip().split("\n") if l]
        return lines
    except Exception:
        return []


def get_message_log():
    """Build message log from reconcile log + git log + status changes."""
    messages = []

    # From reconcile log
    log_path = BZ_DIR / "logs" / "reconcile.log"
    if log_path.exists():
        for line in log_path.read_text().splitlines()[-50:]:
            m = re.match(r'\[(\w+)\]\s+(\S+)\s+(.*)', line)
            if m:
                messages.append({
                    "time": m.group(2),
                    "source": f"🧠 {m.group(1).upper()}",
                    "message": m.group(3),
                })

    # From feedback log
    fb_path = BZ_DIR / "logs" / "feedback.log"
    if fb_path.exists():
        for line in fb_path.read_text().splitlines()[-20:]:
            parts = line.split(" | ", 2)
            if len(parts) >= 3:
                messages.append({
                    "time": parts[0][:19],
                    "source": "👤 HUMAN",
                    "message": parts[2],
                })

    # From git log (all branches)
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "log", "--all", "--oneline",
             "--format=%ct %s", "--since=1 hour ago"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                ts = datetime.fromtimestamp(int(parts[0])).strftime("%H:%M:%S")
                msg = parts[1]
                # Extract agent from commit prefix
                cm = re.match(r'\[(\w+[-\w]*)\]\s*(.*)', msg)
                if cm:
                    messages.append({
                        "time": ts,
                        "source": f"🧟 {cm.group(1)}",
                        "message": f"COMMIT: {cm.group(2)}",
                    })
    except Exception:
        pass

    # Sort by time
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
        context_max = CONTEXT_WINDOWS.get(model, 200_000)
        context_pct = round(usage["total"] / context_max * 100, 1) if context_max else 0

        total_tokens += usage["total"]
        total_cost += cost

        zombies.append({
            "id": aid,
            "runtime": runtime,
            "model": model,
            "phase": phase,
            "health": health,
            "state": state,
            "summary": status.get("summary", "No summary"),
            "files": status.get("files touched", "none"),
            "next_step": status.get("next step", ""),
            "blocker": status.get("blocker", "none"),
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

    elapsed = int(time.time() - start_time) if start_time else 0

    return {
        "project": {
            "name": project.get("name", "unknown"),
            "state": "complete" if all_done else "active",
            "zombies_total": len(zombies),
            "zombies_done": zombies_done,
            "total_commits": total_commits,
            "elapsed_seconds": elapsed,
            "elapsed_display": f"{elapsed // 60}m {elapsed % 60}s",
        },
        "brain": {
            "runtime": supervisor.get("runtime", ""),
            "model": supervisor.get("model", ""),
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
