# 🧠🧟 brain-zombies

> One brain. Many zombies. Ship code while you sleep.

brain-zombies is a lightweight orchestrator that manages headless coding CLI agents. The **brain** (supervisor) gives orders. The **zombies** (Claude Code, Codex, Aider) mindlessly execute — writing code, committing, and reporting back.

**No tool definitions. No gateway. No SDK.** Just tmux + git + your favorite coding CLI.

## Quick Start

```bash
# Install
git clone https://github.com/Zakk-Yang/brain-zombies.git
export PATH="$PATH:$(pwd)/brain-zombies"

# Create a project
mkdir my-project && cd my-project
bz new --brief PROJECT_BRIEF.md

# Unleash the zombies
bz launch

# Watch them work
bz status

# Peek at a zombie
bz attach developer
```

## How It Works

```
You write a brief → brain spawns zombies → zombies code autonomously
                                                    ↕
                                            nerve signal (30s)
                                            detects state changes
                                                    ↓
                                            brain wakes up
                                            coordinates handoffs
```

Each zombie:
- Runs a real coding CLI (Claude Code, Codex, Aider)
- Has its own git worktree (isolated branch)
- Reads `.bz/project/PROJECT.md`, `.bz/project/TARGET.md`, its soul, shared memory, and private memory before planning
- Writes `.bz/project/plans/<zombie>_plan.md` before implementation
- Updates DuckDB state with lifecycle state, task/subtask events, dependencies, and explicit brain-request fields
- Maintains its own memory file for decisions, blockers, progress, and handoff context
- Sends a heartbeat at least every 10 minutes while active
- Commits after each file with `[zombie-id]` prefix
- Doesn't think — just executes

The brain:
- Wakes on zombie state changes (not polling)
- Wakes immediately when a zombie explicitly requests help via `Needs brain`
- Wakes when a zombie misses its heartbeat and proactively asks for status/unblock decisions
- Sends orders (continue / redirect / done)
- Routes human feedback to the right zombie
- Manages handoffs between zombies with bounded brain/agent/shared memory excerpts

## Architecture

```
bz.yaml                    ← single config file
.bz/
  project/
    PROJECT.md             ← clarified project intent
    TARGET.md              ← criteria for success
    state.duckdb           ← canonical state database
    scheduler/
      policy.yaml          ← heartbeat/proactive supervision policy
    souls/
      brain_soul.md
      developer_soul.md
    memories/
      brain_mem.md
      shared_mem.md        ← brain-maintained team memory
      developer_mem.md
    plans/
      developer_plan.md
    outputs/
      brain/
      developer/
    chatlogs/
      user_brain_chatlog.md
      brain_developer_chatlog.md
  agents/
    researcher/
      BRIEF.md             ← compatibility task assignment
      STATUS.md            ← rendered lifecycle + action/dependency mirror
    developer/
      BRIEF.md
      STATUS.md
  control/
    contexts/              ← rendered bounded context for brain/zombies
    agents/                ← pending actions and compatibility state
  worktrees/
    researcher/             ← isolated git worktree
    developer/
  logs/
    reconcile.log
```

## Status Protocol

DuckDB is canonical. `STATUS.md` remains a compatibility mirror carrying a small, standardized control surface:

- `State` for lifecycle
- `Action` for the current concrete task
- `Depends on` for cross-agent dependency
- `Needs brain` for explicit supervisor intervention
- `Summary`, `Files touched`, `Next step`, `Blocker`, `Memory`, and `Last updated` for handoff context

That lets the brain coordinate from explicit action-oriented state instead of inferring everything from freeform summaries.

Zombies also append task/subtask events through:

```bash
.bz/bin/bzctl task-event --agent developer --task "build API" --sub-task "add route" --state start --notes "starting route work"
```

## Commands

| Command | Description |
|---------|-------------|
| `bz new` | Create project from brief |
| `bz launch` | Unleash the zombies |
| `bz status` | Check zombie states |
| `bz attach <id>` | Possess a zombie (attach to tmux) |
| `bz logs <id>` | Read zombie's mind |
| `bz teardown` | Recall all zombies |
| `bz clean` | Full purge |

## Supported Zombie Runtimes

| Runtime | CLI | Status |
|---------|-----|--------|
| Claude Code | `claude` | ✅ |
| Codex | `codex` | ✅ |
| Aider | `aider` | 🔜 |

## Requirements

- `tmux`
- `git`
- At least one coding CLI (`claude` or `codex`)
- `python3` + `pyyaml` + `duckdb`

## Why not crewAI / LangChain?

Those frameworks make you define every tool (FileReadTool, ShellTool, etc.) and wrap LLM calls in Python. brain-zombies lets coding CLIs do what they're already great at — you just point the zombies at the target.

## Why not claude-squad?

claude-squad is a manual multiplexer — you're the brain. brain-zombies adds an autonomous supervisor that coordinates zombies: detecting when one finishes, unblocking the next, routing feedback. You sleep, zombies work.

## License

MIT
