# bz iterate — Autonomous Improvement Loop

## Overview

A **generalized** framework for iterative improvement of any measurable system.
Brain plans, zombie codes, runner evaluates, git keeps or discards.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch),
extended with brain/zombie separation, structured stop conditions, and keep/discard
git history.

## Design Principles

1. **Runner is sacred** — evaluation cannot be gamed by the zombie
2. **Scope is explicit** — zombie edits ONLY specified files
3. **Keep/discard via git** — branch history = only improvements
4. **Brain = search strategy** — LLM judgment, not grid search
5. **Fixed time budget** — experiments are comparable
6. **One change per iteration** — isolate variables

## Command

```bash
bz iterate \
  --goal "ic > 0.20, hit_rate > 0.65" \
  --runner "./run.sh" \
  --scope "train.py" \
  --budget 20 \
  --time-limit 60 \
  --brain opus \
  --zombie sonnet
```

| Flag | Default | Description |
|------|---------|-------------|
| `--goal` | required | Comma-separated `metric > value` conditions |
| `--runner` | required | Command that outputs JSON metrics to stdout |
| `--scope` | required | Comma-separated files/dirs zombie can edit |
| `--budget` | 20 | Max iterations |
| `--time-limit` | 300 | Seconds per runner execution |
| `--brain` | opus | Model for strategic planning |
| `--zombie` | sonnet | Model for code changes |
| `--verbose` | false | Show runner stderr on failure |

## Architecture

```
┌────────────────────────────────────────────────────────┐
│                    bz iterate loop                      │
│                                                         │
│  ┌──────────┐    ┌──────────┐    ┌──────────────────┐  │
│  │  BRAIN    │───→│  ZOMBIE  │───→│  RUNNER          │  │
│  │  (opus)   │    │  (sonnet)│    │  (user script)   │  │
│  │           │    │          │    │                   │  │
│  │ • analyze │    │ • edit   │    │ • execute         │  │
│  │ • plan    │    │ • verify │    │ • measure         │  │
│  │ • decide  │    │          │    │ • output JSON     │  │
│  └─────┬─────┘    └──────────┘    └────────┬─────────┘  │
│        │                                    │           │
│        └────────────── LEDGER ◄─────────────┘           │
│                                                         │
│  KEEP ──→ advance commit (new champion)                 │
│  DISCARD → git reset --hard (revert to champion)        │
└────────────────────────────────────────────────────────┘
```

## Contracts

### Runner Contract

The runner is a **black box** the zombie cannot modify. It must:

1. Execute the current code (train, predict, etc.)
2. Evaluate results against a **fixed** test set
3. Output a JSON object to stdout with numeric metrics
4. Exit 0 on success, non-zero on failure
5. Complete within `--time-limit` seconds

```bash
# Example runner (run.sh)
#!/bin/bash
set -e
python3 train.py        # mutable — zombie edits this
python3 evaluate.py      # sacred — fixed evaluation
```

### Scope Contract

The zombie can ONLY edit files listed in `--scope`. Everything else is read-only.
This is autoresearch's `prepare.py` vs `train.py` generalized.

```
--scope "train.py"                    # single file
--scope "train.py,configs/"           # file + directory
--scope "src/model.py,src/features/"  # multiple paths
```

### Goal Contract

Goals are metric conditions parsed from `--goal`:

```
"ic > 0.20"                    # single metric
"accuracy > 0.95, loss < 0.05" # multiple (all must be met)
"latency < 100, throughput > 1000"  # non-ML works too
```

## Loop Flow

```
1. INIT
   ├── Check clean git worktree
   ├── Run baseline (first runner execution)
   └── Create ledger with baseline as champion

2. LOOP (while budget > 0 and not plateau)
   ├── BRAIN: reads goal + champion + history + scoped files
   │   └── Outputs: { hypothesis, zombie_instructions, expected_impact }
   │
   ├── ZOMBIE: receives instructions, edits scoped files
   │   └── Does NOT commit, does NOT run experiments
   │
   ├── COMMIT: git add <scope> && git commit
   │
   ├── RUNNER: execute with timeout
   │   └── Parse JSON metrics from stdout
   │
   ├── EVALUATE: compare metrics to champion
   │   ├── IMPROVED → keep commit (new champion)
   │   └── NOT IMPROVED → git reset --hard (discard)
   │
   └── LEDGER: record iteration (kept or discarded)

3. STOP when:
   ├── Goal met (all conditions satisfied)
   ├── Budget exhausted (max iterations)
   ├── Plateau (5 consecutive failures)
   └── Human override (Ctrl-C)
```

## Ledger (.bz/iterate/ledger.json)

```json
{
  "goal": { "ic": ">0.20", "hit_rate": ">0.65" },
  "baseline": { "ic": 0.152, "hit_rate": 0.658 },
  "champion": {
    "iteration": 3,
    "metrics": { "ic": 0.198, "hit_rate": 0.671 },
    "commit": "a1b2c3d4"
  },
  "budget": { "max": 20, "used": 5 },
  "last_good_commit": "a1b2c3d4e5f6...",
  "iterations": [
    {
      "id": 1,
      "hypothesis": "Adding RSI and MACD features will improve IC",
      "changes": "Added 2 features to compute_features()",
      "metrics": { "ic": 0.178, "hit_rate": 0.662 },
      "vs_champion": { "ic": {"old": 0.152, "new": 0.178, "delta": 0.026} },
      "verdict": "IMPROVED",
      "duration_sec": 2.3,
      "kept": true,
      "timestamp": "2026-03-24T19:30:00"
    }
  ]
}
```

## Directory Structure

```
.bz/
  iterate/
    ledger.json              ← experiment history (kept + discarded)
    plans/
      iter_001.json          ← brain's plan for each iteration
      iter_002.json
    logs/
      iter_001_zombie.log    ← zombie stdout/stderr
      iter_002_zombie.log
```

## Use Cases

Works for any project with measurable output:

| Domain | Goal | Runner | Scope |
|--------|------|--------|-------|
| Quant signal | `ic > 0.04` | `python run_cv.py` | `src/features/`, `configs/` |
| ML model | `accuracy > 0.95` | `python train.py && python eval.py` | `model.py` |
| Code perf | `latency < 100` | `./bench.sh` | `src/engine.py` |
| LLM training | `val_bpb < 1.0` | `python train.py` | `train.py` |
| Test coverage | `coverage > 90` | `pytest --cov` | `src/`, `tests/` |

## Resume

If interrupted, `bz iterate` resumes automatically:
- Detects existing `ledger.json` and loads state
- Continues from last completed iteration
- Champion and `last_good_commit` are preserved
