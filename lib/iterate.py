#!/usr/bin/env python3
"""bz iterate — Generalized autonomous improvement loop.

Inspired by Karpathy's autoresearch, generalized for any measurable system.
Brain plans, zombie codes, runner evaluates, git keeps or discards.

Usage:
    python3 lib/iterate.py \
        --goal "ic > 0.10, hit_rate > 0.55" \
        --runner "./run.sh" \
        --scope "train.py" \
        --budget 20 \
        --time-limit 60 \
        --brain opus \
        --zombie sonnet
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Data Structures ──────────────────────────────────────────


class GoalCondition:
    __slots__ = ("metric", "op", "threshold")

    def __init__(self, metric: str, op: str, threshold: float):
        self.metric = metric
        self.op = op  # ">" or "<"
        self.threshold = threshold

    def met(self, value: float) -> bool:
        return (value > self.threshold) if self.op == ">" else (value < self.threshold)

    def __repr__(self):
        return f"{self.metric} {self.op} {self.threshold}"


class Ledger:
    """Persistent experiment ledger (.bz/iterate/ledger.json)."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}

    def create(self, goals: list[GoalCondition], baseline: dict, budget: int):
        self.data = {
            "goal": {g.metric: f"{g.op}{g.threshold}" for g in goals},
            "baseline": baseline,
            "champion": {"iteration": 0, "metrics": baseline, "commit": ""},
            "budget": {"max": budget, "used": 0},
            "last_good_commit": "",
            "iterations": [],
        }

    def load(self) -> bool:
        if self.path.exists():
            with open(self.path) as f:
                self.data = json.load(f)
            return True
        return False

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        tmp.rename(self.path)

    @property
    def champion(self) -> dict:
        return self.data.get("champion", {})

    @property
    def champion_metrics(self) -> dict:
        return self.champion.get("metrics", {})

    @property
    def iterations(self) -> list:
        return self.data.get("iterations", [])

    @property
    def used(self) -> int:
        return self.data["budget"]["used"]

    def consecutive_failures(self) -> int:
        n = 0
        for it in reversed(self.iterations):
            if it.get("kept"):
                break
            n += 1
        return n

    def add_iteration(self, record: dict):
        self.data["iterations"].append(record)
        self.data["budget"]["used"] = len(self.iterations)
        self.save()


# ── Main Loop ────────────────────────────────────────────────


class IterateLoop:
    def __init__(self, args):
        self.goals = self._parse_goals(args.goal)
        self.runner = args.runner
        self.scope = [s.strip() for s in args.scope.split(",")]
        self.budget = args.budget
        self.time_limit = args.time_limit
        self.brain_model = args.brain
        self.zombie_model = args.zombie
        self.project_dir = Path(args.project_dir or os.getcwd()).resolve()
        self.iterate_dir = self.project_dir / ".bz" / "iterate"
        self.ledger = Ledger(self.iterate_dir / "ledger.json")
        self.last_good_commit: str = ""
        self.verbose = args.verbose

    # ── Public ───────────────────────────────────────

    def run(self):
        self._print_header()
        self._ensure_clean_worktree()
        self._setup_dirs()

        # Resume or fresh start
        if self.ledger.load():
            self.last_good_commit = self.ledger.data.get("last_good_commit", "")
            self._log(f"Resuming from iteration {self.ledger.used} "
                      f"(champion: iter {self.ledger.champion.get('iteration', 0)})")
        else:
            self.last_good_commit = self._git_head()
            self._log("Running baseline...")
            baseline = self._run_experiment()
            if baseline is None:
                self._err("Baseline failed. Fix your runner and retry.")
                sys.exit(1)

            self._log(f"Baseline: {self._fmt_metrics(baseline)}")
            self.ledger.create(self.goals, baseline, self.budget)
            self.ledger.data["last_good_commit"] = self.last_good_commit
            self.ledger.save()

            if self._goal_met(baseline):
                self._log("Goal already met at baseline!")
                self._print_summary()
                return

        # ── Main loop ────────────────────────────────
        while self._should_continue():
            iter_id = self.ledger.used + 1
            self._log(f"\n{'='*60}")
            self._log(f"ITERATION {iter_id}/{self.budget}")
            self._log(f"{'='*60}")

            # 1 ── Brain plans
            self._log("Brain planning...")
            plan = self._brain_plan(iter_id)
            if plan is None:
                self._err("Brain produced no plan. Skipping iteration.")
                self._record(iter_id, {"hypothesis": "brain_failure"}, {}, "BRAIN_FAIL", 0, False)
                continue

            self._log(f"Hypothesis: {plan.get('hypothesis', '?')}")
            self._save_plan(iter_id, plan)

            # 2 ── Zombie implements
            self._log("Zombie implementing...")
            ok = self._zombie_execute(plan, iter_id)
            if not ok:
                self._log("Zombie failed. Discarding.")
                self._git_reset()
                self._record(iter_id, plan, {}, "ZOMBIE_FAIL", 0, False)
                continue

            # 3 ── Check for actual changes
            changed = self._git_has_changes()
            if not changed:
                self._log("No file changes detected. Skipping.")
                self._record(iter_id, plan, {}, "NO_CHANGE", 0, False)
                continue

            # 4 ── Commit scoped changes
            self._git_add_scope()
            msg = f"[iterate-{iter_id}] {plan.get('hypothesis', 'experiment')[:60]}"
            self._git("commit", "-m", msg, "--allow-empty")

            # 5 ── Run experiment
            self._log("Running experiment...")
            t0 = time.time()
            metrics = self._run_experiment()
            dt = time.time() - t0

            if metrics is None:
                self._log(f"Experiment failed ({dt:.0f}s). Discarding.")
                self._git_reset()
                self._record(iter_id, plan, {}, "RUN_FAIL", dt, False)
                continue

            self._log(f"Result: {self._fmt_metrics(metrics)}  ({dt:.1f}s)")

            # 6 ── Evaluate: improved?
            champ = self.ledger.champion_metrics
            improved, vs = self._compare(metrics, champ)

            # 7 ── Keep or discard
            if improved:
                self.last_good_commit = self._git_head()
                self.ledger.data["champion"] = {
                    "iteration": iter_id,
                    "metrics": metrics,
                    "commit": self.last_good_commit[:8],
                }
                self.ledger.data["last_good_commit"] = self.last_good_commit
                self._log(f">>> NEW CHAMPION (iteration {iter_id})")
            else:
                self._git_reset()
                self._log("Discarded (no improvement)")

            self._record(iter_id, plan, metrics, "IMPROVED" if improved else "NO_IMPROVEMENT", dt, improved)

            # 8 ── Goal check
            check_m = metrics if improved else champ
            if self._goal_met(check_m):
                self._log("\n*** GOAL MET ***")
                break

        self._print_summary()

    # ── Goal logic ───────────────────────────────────

    @staticmethod
    def _parse_goals(s: str) -> list[GoalCondition]:
        goals = []
        for part in s.split(","):
            m = re.match(r"\s*(\w+)\s*([><])\s*([\d.]+)\s*", part)
            if m:
                goals.append(GoalCondition(m.group(1), m.group(2), float(m.group(3))))
        if not goals:
            raise ValueError(f"Cannot parse goal: {s}")
        return goals

    def _goal_met(self, metrics: dict) -> bool:
        return all(g.met(metrics.get(g.metric, float("-inf" if g.op == ">" else "inf")))
                   for g in self.goals)

    def _compare(self, new: dict, old: dict) -> tuple[bool, dict]:
        """Compare new metrics to champion. Improved if ANY goal-metric improves
        and NONE of the goal-metrics regress significantly (>5%)."""
        vs = {}
        any_better = False
        any_worse = False
        for g in self.goals:
            o = old.get(g.metric, 0)
            n = new.get(g.metric, 0)
            delta = n - o
            vs[g.metric] = {"old": round(o, 6), "new": round(n, 6), "delta": round(delta, 6)}
            if g.op == ">":
                if n > o:
                    any_better = True
                if o > 0 and n < o * 0.95:
                    any_worse = True
            else:
                if n < o:
                    any_better = True
                if o > 0 and n > o * 1.05:
                    any_worse = True
        return (any_better and not any_worse), vs

    def _should_continue(self) -> bool:
        if self.ledger.used >= self.budget:
            self._log("Budget exhausted.")
            return False
        cf = self.ledger.consecutive_failures()
        if cf >= 5:
            self._log(f"{cf} consecutive failures. Stopping (plateau).")
            return False
        return True

    # ── Brain ────────────────────────────────────────

    def _brain_plan(self, iter_id: int) -> Optional[dict]:
        scoped = self._read_scope()
        history = self._format_history(last_n=10)
        goal_desc = ", ".join(str(g) for g in self.goals)
        champ = self.ledger.champion
        cf = self.ledger.consecutive_failures()

        prompt = f"""You are an autonomous research agent improving a system iteratively.

GOAL: {goal_desc}

CURRENT CHAMPION (iteration {champ.get('iteration', 0)}):
{json.dumps(champ.get('metrics', {}), indent=2)}

BASELINE:
{json.dumps(self.ledger.data.get('baseline', {}), indent=2)}

EXPERIMENT HISTORY (last 10):{history}

FILES IN SCOPE (you may instruct changes to ONLY these):
"""
        for fname, content in scoped.items():
            prompt += f"\n--- {fname} ---\n{content}\n"

        pivot = ""
        if cf >= 3:
            pivot = f"""
IMPORTANT: The last {cf} iterations ALL FAILED. You MUST try a completely
different approach. Do not tweak the same thing again. Think from first
principles about what would fundamentally change the result.
"""

        prompt += f"""
{pivot}
OUTPUT exactly one JSON object (no markdown fences, no explanation before or after):

{{
  "hypothesis": "one-line description of what you expect to improve",
  "changes_description": "brief summary of code changes",
  "zombie_instructions": "PRECISE instructions: which file, which function, what to add/remove/modify. Enough detail that a junior developer needs zero clarification.",
  "expected_impact": "which metric improves and by roughly how much"
}}

RULES:
- ONE change per iteration (isolate variables)
- Prefer simplicity — removing code that maintains performance is valuable
- Be SPECIFIC in zombie_instructions — vague = bad results
"""

        try:
            result = subprocess.run(
                ["claude", "--model", self.brain_model, "-p", prompt],
                cwd=str(self.project_dir),
                capture_output=True, text=True, timeout=300,
            )
            output = (result.stdout or "") + (result.stderr or "")
            plan = self._extract_json(output)
            if plan and "hypothesis" in plan:
                return plan
            self._err(f"Brain output not parseable:\n{output[-500:]}")
            return None
        except subprocess.TimeoutExpired:
            self._err("Brain timed out (300s)")
            return None
        except Exception as e:
            self._err(f"Brain error: {e}")
            return None

    # ── Zombie ───────────────────────────────────────

    def _zombie_execute(self, plan: dict, iter_id: int) -> bool:
        instructions = plan.get("zombie_instructions", plan.get("changes_description", ""))
        scope_str = ", ".join(self.scope)

        prompt = f"""You are a coding agent. Make exactly ONE focused change and stop.

TASK:
{instructions}

FILES YOU MAY EDIT: {scope_str}
DO NOT modify any other files. DO NOT create new files unless explicitly told to.
DO NOT run experiments, tests, or training scripts.
DO NOT commit — just edit the files.

RULES:
- Make the change described above and NOTHING else
- Do not refactor, add comments, or improve unrelated code
- Verify your edits are syntactically valid
- If instructions are unclear, make your best interpretation

START NOW."""

        log_path = self.iterate_dir / "logs" / f"iter_{iter_id:03d}_zombie.log"
        try:
            result = subprocess.run(
                ["claude", "--dangerously-skip-permissions",
                 "--model", self.zombie_model, "-p", prompt],
                cwd=str(self.project_dir),
                capture_output=True, text=True, timeout=600,
            )
            log_path.write_text(
                f"EXIT: {result.returncode}\n---STDOUT---\n{result.stdout}\n"
                f"---STDERR---\n{result.stderr}"
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            self._err("Zombie timed out (600s)")
            log_path.write_text("TIMEOUT after 600s")
            return False
        except Exception as e:
            self._err(f"Zombie error: {e}")
            return False

    # ── Runner ───────────────────────────────────────

    def _run_experiment(self) -> Optional[dict]:
        try:
            result = subprocess.run(
                ["bash", "-c", self.runner],
                cwd=str(self.project_dir),
                capture_output=True, text=True,
                timeout=self.time_limit,
            )
            if result.returncode != 0:
                self._err(f"Runner exit {result.returncode}")
                if self.verbose:
                    self._err(result.stderr[-500:])
                return None

            # Parse metrics: try stdout first, then metrics file
            metrics = self._extract_json(result.stdout)
            if metrics:
                return metrics

            for candidate in ["outputs/metrics.json", "metrics.json"]:
                p = self.project_dir / candidate
                if p.exists():
                    with open(p) as f:
                        return json.load(f)

            self._err("No JSON metrics in runner stdout or outputs/metrics.json")
            if self.verbose:
                self._err(f"Runner stdout:\n{result.stdout[-500:]}")
            return None

        except subprocess.TimeoutExpired:
            self._err(f"Runner timed out ({self.time_limit}s)")
            return None

    # ── Git ──────────────────────────────────────────

    def _git(self, *args) -> str:
        r = subprocess.run(
            ["git"] + list(args),
            cwd=str(self.project_dir),
            capture_output=True, text=True,
        )
        return r.stdout

    def _git_head(self) -> str:
        return self._git("rev-parse", "HEAD").strip()

    def _git_has_changes(self) -> bool:
        diff = self._git("status", "--porcelain")
        return bool(diff.strip())

    def _git_add_scope(self):
        for s in self.scope:
            p = self.project_dir / s
            if p.exists():
                self._git("add", s)

    def _git_reset(self):
        if self.last_good_commit:
            self._git("reset", "--hard", self.last_good_commit)

    def _ensure_clean_worktree(self):
        if self._git_has_changes() and not self.ledger.path.exists():
            self._err("Working tree has uncommitted changes. Commit or stash first.")
            sys.exit(1)

    # ── Helpers ──────────────────────────────────────

    def _read_scope(self) -> dict[str, str]:
        contents = {}
        for s in self.scope:
            p = self.project_dir / s
            if p.is_file():
                contents[s] = p.read_text()
            elif p.is_dir():
                for f in sorted(p.rglob("*")):
                    if f.is_file() and not f.name.startswith("."):
                        rel = str(f.relative_to(self.project_dir))
                        contents[rel] = f.read_text()
        return contents

    def _format_history(self, last_n: int = 10) -> str:
        iters = self.ledger.iterations[-last_n:]
        if not iters:
            return "\n  (no previous iterations)"
        lines = []
        for it in iters:
            mark = "KEPT" if it.get("kept") else "discarded"
            m = it.get("metrics", {})
            ms = ", ".join(f"{k}={v:.4f}" for k, v in m.items() if isinstance(v, (int, float)))
            lines.append(f"  iter {it['id']}: [{mark}] {it.get('hypothesis', '?')}")
            lines.append(f"           metrics: {ms}")
        return "\n" + "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Robustly extract first JSON object from text."""
        # Try code-fenced JSON first
        m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", text)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Walk through text finding { } balanced pairs
        depth = 0
        start = -1
        for i, c in enumerate(text):
            if c == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = -1
        return None

    def _setup_dirs(self):
        for d in ["plans", "logs"]:
            (self.iterate_dir / d).mkdir(parents=True, exist_ok=True)

    def _save_plan(self, iter_id: int, plan: dict):
        p = self.iterate_dir / "plans" / f"iter_{iter_id:03d}.json"
        with open(p, "w") as f:
            json.dump(plan, f, indent=2)

    def _record(self, iter_id: int, plan: dict, metrics: dict,
                verdict: str, duration: float, kept: bool):
        vs = {}
        champ = self.ledger.champion_metrics
        for g in self.goals:
            o = champ.get(g.metric, 0)
            n = metrics.get(g.metric, 0)
            vs[g.metric] = {"old": round(o, 6), "new": round(n, 6), "delta": round(n - o, 6)}

        self.ledger.add_iteration({
            "id": iter_id,
            "hypothesis": plan.get("hypothesis", ""),
            "changes": plan.get("changes_description", ""),
            "metrics": metrics,
            "vs_champion": vs,
            "verdict": verdict,
            "duration_sec": round(duration, 1),
            "kept": kept,
            "timestamp": datetime.now().isoformat(),
        })

    @staticmethod
    def _fmt_metrics(m: dict) -> str:
        return ", ".join(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}"
                         for k, v in m.items())

    # ── Output ───────────────────────────────────────

    def _print_header(self):
        print(f"\n{'='*60}")
        print(f"  bz iterate — Autonomous Improvement Loop")
        print(f"{'='*60}")
        print(f"  Goal:    {', '.join(str(g) for g in self.goals)}")
        print(f"  Runner:  {self.runner}")
        print(f"  Scope:   {', '.join(self.scope)}")
        print(f"  Budget:  {self.budget} iterations")
        print(f"  Timeout: {self.time_limit}s per run")
        print(f"  Brain:   {self.brain_model}")
        print(f"  Zombie:  {self.zombie_model}")
        print(f"{'='*60}\n")

    def _print_summary(self):
        champ = self.ledger.champion
        baseline = self.ledger.data.get("baseline", {})
        iters = self.ledger.iterations
        kept = sum(1 for it in iters if it.get("kept"))

        print(f"\n{'='*60}")
        print(f"  SUMMARY — {len(iters)}/{self.budget} iterations, {kept} kept")
        print(f"{'='*60}")
        print(f"  Champion: iteration {champ.get('iteration', 0)}")
        print()
        print(f"  {'Metric':<20} {'Baseline':>10} {'Champion':>10} {'Goal':>10} {'Met?':>6}")
        print(f"  {'-'*56}")
        for g in self.goals:
            b = baseline.get(g.metric, 0)
            c = champ.get("metrics", {}).get(g.metric, 0)
            met = "Y" if g.met(c) else ""
            print(f"  {g.metric:<20} {b:>10.4f} {c:>10.4f} {g.op}{g.threshold:>9} {met:>6}")

        print(f"\n  Timeline:")
        for it in iters:
            mark = ">>>" if it.get("kept") else "   "
            m = it.get("metrics", {})
            ms = ", ".join(f"{k}={v:.4f}" for k, v in m.items() if isinstance(v, (int, float)))
            hyp = it.get("hypothesis", "?")[:50]
            print(f"  {mark} iter {it['id']:>2}: {ms}  | {hyp}")
        print(f"{'='*60}\n")

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def _err(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] ERROR: {msg}", file=sys.stderr, flush=True)


# ── CLI ──────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser(
        description="bz iterate — Autonomous improvement loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  bz iterate --goal "ic > 0.10" --runner "./run.sh" --scope "train.py"
  bz iterate --goal "accuracy > 0.95, loss < 0.05" --runner "python eval.py" \\
             --scope "model.py,config.yaml" --budget 30 --brain opus --zombie sonnet
        """,
    )
    ap.add_argument("--goal", required=True,
                    help="Comma-separated conditions: 'metric > value, ...'")
    ap.add_argument("--runner", required=True,
                    help="Command that outputs JSON metrics to stdout")
    ap.add_argument("--scope", required=True,
                    help="Comma-separated files/dirs the zombie can edit")
    ap.add_argument("--budget", type=int, default=20,
                    help="Max iterations (default: 20)")
    ap.add_argument("--time-limit", type=int, default=300,
                    help="Runner timeout in seconds (default: 300)")
    ap.add_argument("--brain", default="opus",
                    help="Brain model (default: opus)")
    ap.add_argument("--zombie", default="sonnet",
                    help="Zombie model (default: sonnet)")
    ap.add_argument("--project-dir",
                    help="Project directory (default: cwd)")
    ap.add_argument("--verbose", action="store_true",
                    help="Show runner stderr on failure")

    args = ap.parse_args()
    IterateLoop(args).run()


if __name__ == "__main__":
    main()
