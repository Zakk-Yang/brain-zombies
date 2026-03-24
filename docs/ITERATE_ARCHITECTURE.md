# bz iterate — Autonomous Iterative Research

## Overview

`bz iterate` runs an autonomous experiment loop where the brain:
1. Reads the goal and baseline
2. Analyzes past results
3. Plans the next experiment
4. Assigns a zombie to implement it
5. Runs the experiment
6. Evaluates results
7. Decides: continue, pivot, or stop

## Command

```bash
bz iterate \
  --goal "top_decile_precision > 0.25, ic > 0.04" \
  --baseline outputs/baseline_xgb/metrics.json \
  --budget 20 \
  --brain opus \
  --zombie sonnet \
  --experiment-runner "python scripts/run_experiment.py --config {config}" \
  --max-cost 50.0
```

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  bz iterate loop                      │
│                                                       │
│  ┌─────────────┐    ┌──────────┐    ┌──────────────┐ │
│  │  BRAIN       │───→│  ZOMBIE  │───→│  RUNNER      │ │
│  │  (opus)      │    │  (sonnet)│    │  (python)    │ │
│  │              │    │          │    │              │ │
│  │ • analyze    │    │ • code   │    │ • execute    │ │
│  │ • plan       │    │ • commit │    │ • measure    │ │
│  │ • decide     │    │ • test   │    │ • log        │ │
│  └──────┬───────┘    └──────────┘    └──────┬───────┘ │
│         │                                    │        │
│         └────────────── LEDGER ◄─────────────┘        │
│                     (experiment log)                   │
└──────────────────────────────────────────────────────┘
```

## Experiment Ledger (.bz/iterate/ledger.json)

```json
{
  "goal": {
    "top_decile_precision": ">0.25",
    "ic": ">0.04"
  },
  "baseline": {
    "top_decile_precision": 0.215,
    "quintile_spread": 0.155,
    "ic": 0.036,
    "ic_ir": 0.162,
    "source": "quant-lab XGB v8 32-feature"
  },
  "champion": {
    "iteration": 5,
    "top_decile_precision": 0.228,
    "ic": 0.039,
    "config": "configs/iter_005_cross_sectional.yaml"
  },
  "budget": { "max_iterations": 20, "used": 7, "max_cost": 50.0, "spent": 12.30 },
  "iterations": [
    {
      "id": 1,
      "hypothesis": "Adding 28 more features will close IC gap from 0.022 to 0.036",
      "changes": "Added 28 daily features to src/features/daily.py",
      "config": "configs/iter_001_more_features.yaml",
      "results": { "ic": 0.032, "top_decile_precision": 0.206 },
      "vs_baseline": { "ic": -0.004, "top_decile_precision": -0.009 },
      "verdict": "IMPROVED over previous but still below baseline",
      "duration_min": 3.2,
      "cost": 1.50
    },
    {
      "id": 2,
      "hypothesis": "Cross-sectional features (sector-relative) will improve quintile spread",
      "changes": "Added 5 cross-sectional features",
      "config": "configs/iter_002_cross_sectional.yaml",
      "results": { "ic": 0.039, "top_decile_precision": 0.228 },
      "vs_baseline": { "ic": "+0.003", "top_decile_precision": "+0.013" },
      "verdict": "NEW CHAMPION — beats baseline on both metrics",
      "duration_min": 4.1,
      "cost": 1.80
    }
  ]
}
```

## Brain Prompt Template

Each iteration, the brain receives:

```
ROLE: You are a quantitative research lead. Your goal: {goal}

BASELINE TO BEAT:
{baseline metrics}

CURRENT CHAMPION (iteration {n}):
{champion metrics}

EXPERIMENT HISTORY:
{last 5 iterations with hypothesis → result → verdict}

AVAILABLE TOOLS:
- Modify feature set (src/features/daily.py has {n} features)
- Change target (current: {target}, options: residual/rank/vol_adjusted × 5/10/21/42d)
- Tune model params (current: {model_config})
- Change model type (XGB, LightGBM, MLP)

YOUR TASK:
1. Analyze: why hasn't the goal been met yet? What's the biggest gap?
2. Hypothesize: what specific change would most likely improve results?
3. Plan: output a JSON with:
   {
     "hypothesis": "...",
     "action": "modify_features|change_target|tune_model|change_model",
     "changes_description": "...",
     "zombie_task": "... specific instructions for the coding zombie ...",
     "config_overrides": { ... },
     "expected_impact": "IC +0.005, top_decile +0.01"
   }

RULES:
- Only ONE change per iteration (isolate variables)
- If last 3 iterations all failed, try a completely different direction
- If overfit_ratio > 2.0, add more regularization before trying new features
- Stop if: goal met, or 3 consecutive iterations with no improvement
```

## Iterate Loop (lib/iterate.sh or Python)

```python
def iterate(goal, baseline, budget, brain_model, zombie_model, runner_cmd):
    ledger = load_or_create_ledger(goal, baseline, budget)

    while ledger.budget_remaining() and not ledger.goal_met():
        # 1. Brain decides next experiment
        brain_output = call_brain(
            model=brain_model,
            prompt=build_brain_prompt(ledger),
        )
        plan = parse_brain_plan(brain_output)

        # 2. Zombie implements changes
        if plan.needs_code_changes:
            zombie_output = spawn_zombie(
                model=zombie_model,
                task=plan.zombie_task,
            )
            wait_for_zombie(zombie_output)

        # 3. Generate experiment config
        config_path = generate_config(plan.config_overrides, ledger.iteration_id)

        # 4. Run experiment
        start = time.time()
        result = run_experiment(runner_cmd.format(config=config_path))
        duration = time.time() - start

        # 5. Evaluate
        metrics = parse_results(result)
        verdict = evaluate_vs_baseline(metrics, ledger.champion, ledger.baseline)

        # 6. Update ledger
        ledger.add_iteration(
            hypothesis=plan.hypothesis,
            changes=plan.changes_description,
            config=config_path,
            results=metrics,
            verdict=verdict,
            duration=duration,
        )

        if verdict == "NEW_CHAMPION":
            ledger.update_champion(metrics, config_path)

        # 7. Brain decides: continue or stop
        if ledger.consecutive_failures >= 3:
            # Brain gets a special "pivot" prompt
            pass

        ledger.save()
        log_iteration(ledger.latest)

    print_summary(ledger)
```

## Directory Structure

```
.bz/
  iterate/
    ledger.json              ← experiment history
    plans/
      iter_001_plan.json     ← brain's plan for each iteration
      iter_002_plan.json
    configs/
      iter_001.yaml          ← generated experiment config
      iter_002.yaml
    results/
      iter_001_metrics.json  ← experiment results
      iter_002_metrics.json
    logs/
      iter_001_zombie.log    ← zombie output
      iter_001_run.log       ← experiment stdout
```

## Dashboard Integration

The dashboard shows:

```
ITERATE MODE: 7/20 iterations | Champion: IC=0.039 (iteration 2) | Goal: IC>0.04

[Progress Chart]
  iter 1: IC=0.032 ↑ features
  iter 2: IC=0.039 ★ champion (cross-sectional)
  iter 3: IC=0.035 ↓ target change
  iter 4: IC=0.038 → model tuning
  iter 5: IC=0.039 → same
  iter 6: IC=0.041 ★ NEW CHAMPION (goal: IC>0.04 ✓)
  iter 7: IC=0.040 → refinement

[Latest Brain Decision]
  "IC goal met. Quintile spread still below baseline.
   Next: add momentum persistence features to improve ranking depth."
```

## Stop Conditions

1. **Goal met** — all target metrics exceeded
2. **Budget exhausted** — max iterations or max cost reached
3. **Plateau** — 3 consecutive iterations with no improvement
4. **Overfit** — overfit_ratio > 3.0 for 2 consecutive iterations
5. **Human override** — `bz iterate --stop`
