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
- Updates STATUS.md with lifecycle state
- Commits after each file with `[zombie-id]` prefix
- Doesn't think — just executes

The brain:
- Wakes on zombie state changes (not polling)
- Sends orders (continue / redirect / done)
- Routes human feedback to the right zombie
- Manages handoffs between zombies

## Architecture

```
bz.yaml                    ← single config file
.bz/
  agents/
    researcher/
      BRIEF.md             ← task assignment (the order)
      STATUS.md             ← lifecycle: starting→working→done
    developer/
      BRIEF.md
      STATUS.md
  worktrees/
    researcher/             ← isolated git worktree
    developer/
  logs/
    reconcile.log
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
- `python3` + `pyyaml`

## Why not crewAI / LangChain?

Those frameworks make you define every tool (FileReadTool, ShellTool, etc.) and wrap LLM calls in Python. brain-zombies lets coding CLIs do what they're already great at — you just point the zombies at the target.

## Why not claude-squad?

claude-squad is a manual multiplexer — you're the brain. brain-zombies adds an autonomous supervisor that coordinates zombies: detecting when one finishes, unblocking the next, routing feedback. You sleep, zombies work.

## License

MIT
