#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
# NERVE SYSTEM — smart gatekeeper for brain wake decisions
#
# Every 30s (FREE, bash only):
#   1. Scan STATUS.md for state changes    → wake brain if changed
#   2. Check tmux sessions alive           → wake brain if crashed
#   3. Check git commit timestamps         → wake brain if stalled
#   4. All clear?                          → sleep, save tokens
#
# Every 15m (PROACTIVE, costs tokens):
#   Force brain check for stuck zombies that didn't update STATUS.md
#
# Usage: bash reconcile.sh <project-root>
# ══════════════════════════════════════════════════════════════

PROJECT_ROOT="${1:?Usage: reconcile.sh <project-root>}"
BZ_DIR="${PROJECT_ROOT}/.bz"
LOG_DIR="${BZ_DIR}/logs"
SIGNATURES_FILE="${LOG_DIR}/.signatures"
LAST_PROACTIVE_FILE="${LOG_DIR}/.last-proactive"
LAST_COMMIT_FILE="${LOG_DIR}/.last-commits"

SCAN_INTERVAL=30
PROACTIVE_INTERVAL=900   # 15 min default
STALL_THRESHOLD=600      # 10 min without commits = stalled

mkdir -p "$LOG_DIR"

# Read config
if [[ -f "${PROJECT_ROOT}/bz.yaml" ]]; then
    PROACTIVE_INTERVAL="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(int(d.get('supervisor',{}).get('proactive_check_mins', 15)) * 60)
" 2>/dev/null || echo 900)"
fi

# ── Helpers ──────────────────────────────────────

project_name() {
    python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    print(yaml.safe_load(f).get('project',{}).get('name',''))
" 2>/dev/null || echo "unknown"
}

PROJECT_NAME="$(project_name)"

capture_signatures() {
    for status_file in "${BZ_DIR}/agents"/*/STATUS.md; do
        [[ -f "$status_file" ]] || continue
        local agent_id
        agent_id="$(basename "$(dirname "$status_file")")"
        local sig
        sig="$(grep -E '^(State|Blocker):' "$status_file" 2>/dev/null | tr '\n' '|')"
        echo "${agent_id}=${sig}"
    done | sort
}

all_done() {
    local has_agents=0
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local aid
        aid="$(basename "$agent_dir")"
        [[ "$aid" == "supervisor" ]] && continue
        has_agents=1

        local state
        state="$(grep '^State:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/State: //')"
        # Must be done AND have brain confirmation
        if [[ "$state" != "done" && "$state" != "ready-for-review" ]]; then
            return 1
        fi
        if [[ ! -f "${agent_dir}/DECISION.md" ]]; then
            return 1  # brain hasn't confirmed
        fi
    done
    [[ "$has_agents" -eq 1 ]] && return 0 || return 1
}

sync_status_from_worktrees() {
    for wt_status in "${BZ_DIR}/worktrees"/*/".bz/agents"/*/STATUS.md; do
        [[ -f "$wt_status" ]] || continue
        local agent_id
        agent_id="$(basename "$(dirname "$wt_status")")"
        local main_status="${BZ_DIR}/agents/${agent_id}/STATUS.md"
        if [[ -f "$main_status" && "$wt_status" -nt "$main_status" ]]; then
            cp "$wt_status" "$main_status"
        fi
    done
}

# ── Wake Triggers (FREE — just bash checks) ─────

# Check 1: STATUS.md content changed
check_state_change() {
    local current="$1"
    local previous=""
    [[ -f "$SIGNATURES_FILE" ]] && previous="$(cat "$SIGNATURES_FILE")"
    echo "$current" > "$SIGNATURES_FILE"

    [[ -z "$previous" ]] && return 1  # first run, skip
    [[ "$current" == "$previous" ]] && return 1  # no change

    # Find which agents changed
    local changed=""
    while IFS= read -r line; do
        local agent="${line%%=*}"
        if ! grep -qF "$line" <<< "$previous" 2>/dev/null; then
            changed="${changed} ${agent}"
        fi
    done <<< "$current"

    echo "$changed"
    return 0
}

# Check 1.5: hallucination — agent claims done/ready but has zero git changes
check_hallucination() {
    local frauds=""
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local state
        state="$(grep '^State:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/State: //')"

        # Only check agents claiming completion
        [[ "$state" != "done" && "$state" != "ready-for-review" ]] && continue

        # Already verified by brain (has DECISION.md)
        [[ -f "${agent_dir}/DECISION.md" ]] && continue

        # Check git diff in worktree — should have commits beyond main
        local wt="${BZ_DIR}/worktrees/${agent_id}"
        if [[ -d "$wt" ]] && ( [[ -d "$wt/.git" ]] || [[ -f "$wt/.git" ]] ); then
            local main_head
            main_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
            local wt_head
            wt_head="$(git -C "$wt" rev-parse HEAD 2>/dev/null || echo "")"

            if [[ -n "$main_head" && "$main_head" == "$wt_head" ]]; then
                # Zero commits beyond main — agent is lying
                local files_touched
                files_touched="$(grep '^Files touched:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/Files touched: //')"
                if [[ "$files_touched" != "none" && -n "$files_touched" ]]; then
                    # Claims files but has no commits — hallucination
                    frauds="${frauds} ${agent_id}"
                    log "HALLUCINATION DETECTED: ${agent_id} claims '${state}' with files '${files_touched}' but has zero git changes"

                    # Auto-reject: reset status and restart
                    cat > "${agent_dir}/STATUS.md" << FRAUD_EOF
# STATUS.md
State: executing
Summary: RESTARTED — previous completion was rejected (zero git changes detected). You must actually write code, run experiments, and git commit your work.
Files touched: none
Next step: Re-read BRIEF.md and do the actual work. Commit files as you go.
Blocker: none
Last updated: $(date '+%Y-%m-%d %H:%M')
FRAUD_EOF
                fi
            fi
        fi
    done

    [[ -n "$frauds" ]] && echo "$frauds" && return 0
    return 1
}

# Check 2: tmux session died
check_zombie_alive() {
    local dead=""
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        local sess="bz-${PROJECT_NAME}-${agent_id}"

        # Skip supervisor and already-done zombies
        [[ "$agent_id" == "supervisor" ]] && continue
        local state
        state="$(grep '^State:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/State: //')"
        [[ "$state" == "done" ]] && continue

        if ! tmux has-session -t "$sess" 2>/dev/null; then
            dead="${dead} ${agent_id}"
        fi
    done

    [[ -n "$dead" ]] && echo "$dead" && return 0
    return 1
}

# Check 3: no new commits in worktree (stalled)
check_stalled() {
    local stalled=""
    local now
    now="$(date +%s)"

    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local state
        state="$(grep '^State:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/State: //')"
        [[ "$state" == "done" || "$state" == "blocked" || "$state" == "starting" || "$state" == "executing" || "$state" == "running" || "$state" == "ready-for-review" ]] && continue

        # Also skip stall check if summary mentions running/executing
        local summary
        summary="$(grep '^Summary:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/Summary: //' | tr '[:upper:]' '[:lower:]')"
        if [[ "$summary" == *"running"* || "$summary" == *"experiment"* || "$summary" == *"executing"* || "$summary" == *"training"* ]]; then
            continue
        fi

        # Check last commit time in worktree
        local wt="${BZ_DIR}/worktrees/${agent_id}"
        local last_commit=0
        if [[ -d "$wt/.git" || -f "$wt/.git" ]]; then
            last_commit="$(git -C "$wt" log -1 --format='%ct' 2>/dev/null || echo 0)"
        fi

        if [[ "$last_commit" -gt 0 ]]; then
            local elapsed=$((now - last_commit))
            if [[ "$elapsed" -ge "$STALL_THRESHOLD" ]]; then
                stalled="${stalled} ${agent_id}(${elapsed}s)"
            fi
        fi
    done

    [[ -n "$stalled" ]] && echo "$stalled" && return 0
    return 1
}

# Check 4: zombies finished but brain hasn't confirmed
check_pending_review() {
    local pending=""
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local state
        state="$(grep '^State:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/State: //')"

        # Zombie says done or ready-for-review but no DECISION.md from brain
        if [[ "$state" == "done" || "$state" == "ready-for-review" ]]; then
            if [[ ! -f "${agent_dir}/DECISION.md" ]]; then
                pending="${pending} ${agent_id}"
            fi
        fi
    done

    [[ -n "$pending" ]] && echo "$pending" && return 0
    return 1
}

# Gather an agent's actual work for brain review
gather_agent_work() {
    local agent_id="$1"
    local work=""
    local wt="${BZ_DIR}/worktrees/${agent_id}"
    local agent_dir="${BZ_DIR}/agents/${agent_id}"

    # Full STATUS.md
    if [[ -f "${agent_dir}/STATUS.md" ]]; then
        work="${work}
--- STATUS.md ---
$(cat "${agent_dir}/STATUS.md")"
    fi

    # Git log (commits by this agent)
    if [[ -d "$wt/.git" ]] || [[ -f "$wt/.git" ]]; then
        local branch_commits
        branch_commits="$(git -C "$wt" log --oneline "master..HEAD" 2>/dev/null | head -20)"
        if [[ -n "$branch_commits" ]]; then
            work="${work}

--- Git commits (branch vs master) ---
${branch_commits}"
        fi

        # Git diff stat
        local diff_stat
        diff_stat="$(git -C "$wt" diff --stat master 2>/dev/null | tail -5)"
        if [[ -n "$diff_stat" ]]; then
            work="${work}

--- Files changed ---
${diff_stat}"
        fi
    fi

    # Output files: reports, results (truncated to avoid prompt explosion)
    for report in "${wt}/outputs/research/"*.md "${wt}/outputs/research/"*.json; do
        [[ -f "$report" ]] || continue
        local fname
        fname="$(basename "$report")"
        local content
        content="$(head -80 "$report")"
        work="${work}

--- ${fname} (first 80 lines) ---
${content}"
    done

    # Check for experiment results (metrics output)
    for metrics_file in "${wt}/outputs/experiments/"*/metrics.json; do
        [[ -f "$metrics_file" ]] || continue
        local exp_name
        exp_name="$(basename "$(dirname "$metrics_file")")"
        local content
        content="$(head -30 "$metrics_file")"
        work="${work}

--- Experiment: ${exp_name} (metrics) ---
${content}"
    done

    echo "$work"
}

# ── Brain Wake (COSTS TOKENS) ───────────────────

wake_brain() {
    local reason="$1"
    local mode="$2"   # reactive | proactive | crash | stall

    # Build context: all zombie statuses
    local status_summary=""
    for sf in "${BZ_DIR}/agents"/*/STATUS.md; do
        [[ -f "$sf" ]] || continue
        local aid
        aid="$(basename "$(dirname "$sf")")"
        [[ "$aid" == "supervisor" ]] && continue
        local state summary blocker
        state="$(grep '^State:' "$sf" 2>/dev/null | head -1 | sed 's/State: //')"
        summary="$(grep '^Summary:' "$sf" 2>/dev/null | head -1 | sed 's/Summary: //')"
        blocker="$(grep '^Blocker:' "$sf" 2>/dev/null | head -1 | sed 's/Blocker: //')"
        status_summary="${status_summary}
- ${aid}: state=${state} | ${summary} | blocker=${blocker}"
    done

    local prompt
    case "$mode" in
        reactive)
            prompt="NERVE SIGNAL: Zombie state change detected —${reason}.

Current zombie states:${status_summary}

YOUR JOB: Read the states above. If any zombie just finished (state=done) and another is blocked waiting for it, write a DECISION file to unblock it. If any zombie is stuck, write a redirect. Write your decisions to .bz/agents/<zombie-id>/DECISION.md with clear instructions.

For each decision, output a line: DECISION: <zombie-id> — <action> — <reason>" ;;
        crash)
            prompt="ZOMBIE DOWN:${reason} — tmux session died.

Current zombie states:${status_summary}

YOUR JOB: Assess if the crashed zombie's work was committed. Write a DECISION file for it (restart or reassign). Output: DECISION: <zombie-id> — <action> — <reason>" ;;
        stall)
            prompt="STALL DETECTED:${reason} — no commits for 10+ minutes while State=working.

Current zombie states:${status_summary}

YOUR JOB: Investigate why the zombie stalled. Write a DECISION file with specific instructions to get it moving. Output: DECISION: <zombie-id> — <action> — <reason>" ;;
        proactive)
            prompt="HEARTBEAT CHECK: Routine progress check.

Current zombie states:${status_summary}

YOUR JOB: Verify all zombies are making progress. If any need intervention, write DECISION files. If all are fine, just output: ALL CLEAR. Output: DECISION: <zombie-id> — <action> — <reason>" ;;
        review)
            # Gather actual work from each pending agent
            local review_details=""
            for rid in ${reason}; do
                local agent_work
                agent_work="$(gather_agent_work "$rid" 2>/dev/null)"
                if [[ -n "$agent_work" ]]; then
                    review_details="${review_details}

========== AGENT: ${rid} ==========${agent_work}
"
                fi
            done

            prompt="REVIEW REQUEST: Zombies pending review —${reason}.

Current zombie states:${status_summary}

=== DETAILED WORK FOR REVIEW ===${review_details}

YOUR JOB: Review the work above for each pending zombie. Check:
1. Did commits actually land? (check git commits section)
2. Do the results meet the success criteria in PROJECT_BRIEF.md?
3. Are the reported metrics plausible (not hallucinated)?

You MUST output one DECISION line per pending zombie. No exceptions. No skipping.
Use EXACTLY this format (plain text, no markdown, no bold):
DECISION: zombie-id — accept — reason
DECISION: zombie-id — reject — reason
DECISION: zombie-id — redirect — new instructions

Pending zombies that MUST each get a DECISION line: ${reason}" ;;
    esac

    # Read brain config
    local brain_runtime brain_model brain_cli
    brain_runtime="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(d.get('supervisor',{}).get('runtime','claude'))
" 2>/dev/null || echo "claude")"
    brain_model="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(d.get('supervisor',{}).get('model','sonnet'))
" 2>/dev/null || echo "sonnet")"
    local brain_thinking
    brain_thinking="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(d.get('supervisor',{}).get('thinking',''))
" 2>/dev/null || echo "")"

    case "$brain_runtime" in
        claude|claude-code) brain_cli="claude" ;;
        codex) brain_cli="codex" ;;
        *) brain_cli="$brain_runtime" ;;
    esac

    echo "[brain] $(date '+%H:%M:%S') WAKE (${mode}):${reason}"

    # On-demand brain call — captured output
    local brain_output=""
    local brain_prompt_file="${BZ_DIR}/logs/brain-prompt-$(date +%s).txt"
    echo "$prompt" > "$brain_prompt_file"

    # Claude Code CLI uses --effort (low/medium/high/max), not --thinking
    local thinking_flag=""
    if [[ -n "$brain_thinking" && "$brain_thinking" != "None" && "$brain_thinking" != "" && "$brain_thinking" != "off" ]]; then
        if [[ "$brain_thinking" == "disabled" || "$brain_thinking" == "off" ]]; then
            thinking_flag="--effort low"
        elif [[ "$brain_thinking" == "max" || "$brain_thinking" == "xhigh" ]]; then
            thinking_flag="--effort max"
        elif [[ "$brain_thinking" == "high" || "$brain_thinking" == "enabled" ]]; then
            thinking_flag="--effort high"
        elif [[ "$brain_thinking" == "medium" ]]; then
            thinking_flag="--effort medium"
        fi
    fi

    if [[ "$brain_cli" == "claude" ]]; then
        brain_output="$(cd "${PROJECT_ROOT}" && claude --dangerously-skip-permissions --model "$brain_model" $thinking_flag -p "$(cat "$brain_prompt_file")" 2>/dev/null || echo "BRAIN ERROR")"
    elif [[ "$brain_cli" == "codex" ]]; then
        brain_output="$(cd "${PROJECT_ROOT}" && codex exec --full-auto --model "$brain_model" "$(cat "$brain_prompt_file")" 2>/dev/null || echo "BRAIN ERROR")"
    fi

    # Log brain output
    echo "[brain] $(date '+%H:%M:%S') RESPONSE: ${brain_output}" | head -20

    # Parse decisions and relay to zombies
    # Strip markdown bold/italic before parsing
    echo "$brain_output" | sed 's/\*\*//g; s/\*//g; s/__//g; s/`//g' | grep "^DECISION:" | while IFS= read -r decision_line; do
        local target action
        target="$(echo "$decision_line" | sed 's/DECISION: //' | cut -d'—' -f1 | xargs)"
        action="$(echo "$decision_line" | cut -d'—' -f2- | xargs)"

        if [[ -n "$target" && -n "$action" ]]; then
            # Write decision file
            local decision_file="${BZ_DIR}/agents/${target}/DECISION.md"
            echo "# Brain Decision ($(date '+%H:%M:%S'))
${action}" > "$decision_file"

            # Send to zombie — restart CLI if it exited
            local zombie_sess="bz-${PROJECT_NAME}-${target}"
            local cli_alive=0
            if tmux has-session -t "$zombie_sess" 2>/dev/null; then
                local pane_pid
                pane_pid="$(tmux list-panes -t "$zombie_sess" -F '#{pane_pid}' 2>/dev/null | head -1)"
                if [[ -n "$pane_pid" ]] && pgrep -P "$pane_pid" -f "claude|codex|aider" >/dev/null 2>&1; then
                    cli_alive=1
                fi
            fi

            if [[ "$cli_alive" -eq 1 ]]; then
                tmux send-keys -t "$zombie_sess" "BRAIN DECISION: ${action}" Enter
            else
                # CLI exited — restart with decision as prompt
                echo "[brain] $(date '+%H:%M:%S') Restarting ${target} CLI to deliver decision"
                tmux kill-session -t "$zombie_sess" 2>/dev/null || true

                local wt_path="${BZ_DIR}/worktrees/${target}"
                local work_dir="${PROJECT_ROOT}"
                [[ -d "$wt_path" ]] && work_dir="$wt_path"
                local abs_work_dir
                abs_work_dir="$(realpath "$work_dir")"

                # Read zombie's runtime/model from config
                local z_cli z_model
                z_cli="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
for a in d.get('agents',[]):
    if a.get('id') == '${target}':
        r = a.get('runtime','claude')
        print('claude' if r in ('claude','claude-code') else r)
        break
" 2>/dev/null || echo "claude")"
                z_model="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
for a in d.get('agents',[]):
    if a.get('id') == '${target}':
        print(a.get('model','sonnet'))
        break
" 2>/dev/null || echo "sonnet")"

                local restart_prompt="BRAIN DECISION: ${action}

Read ${BZ_DIR}/agents/${target}/DECISION.md for full instructions. Execute NOW."
                local restart_prompt_file="${BZ_DIR}/agents/${target}/RESTART_PROMPT.txt"
                echo "$restart_prompt" > "$restart_prompt_file"

                local restart_cmd
                if [[ "$z_cli" == "claude" ]]; then
                    restart_cmd="cd ${abs_work_dir} && claude --dangerously-skip-permissions --model ${z_model} -p \"\$(cat ${restart_prompt_file})\""
                elif [[ "$z_cli" == "codex" ]]; then
                    restart_cmd="cd ${abs_work_dir} && codex exec --full-auto --model ${z_model} \"\$(cat ${restart_prompt_file})\""
                else
                    restart_cmd="cd ${abs_work_dir} && ${z_cli} \"\$(cat ${restart_prompt_file})\""
                fi

                tmux new-session -d -s "$zombie_sess" -x 200 -y 50
                tmux send-keys -t "$zombie_sess" "$restart_cmd" Enter
            fi

            echo "[brain] $(date '+%H:%M:%S') → 🧟 ${target}: ${action}"
        fi
    done

    rm -f "$brain_prompt_file"

    # Reset proactive timer
    date +%s > "$LAST_PROACTIVE_FILE"
}

# ── Main Loop ────────────────────────────────────

echo "[nerve] Started — scan:${SCAN_INTERVAL}s proactive:$((PROACTIVE_INTERVAL/60))m stall:$((STALL_THRESHOLD/60))m"
echo "[nerve] Watching: ${BZ_DIR}/agents/*/STATUS.md"

date +%s > "$LAST_PROACTIVE_FILE" 2>/dev/null || true

while true; do
    sync_status_from_worktrees 2>/dev/null || true

    # Skip everything if all done
    if all_done 2>/dev/null; then
        sleep "$SCAN_INTERVAL"
        continue
    fi

    wake_needed=0
    wake_reason=""
    wake_mode=""

    # CHECK 1: State change (most common)
    current_sigs="$(capture_signatures)"
    if changed="$(check_state_change "$current_sigs" 2>/dev/null)"; then
        wake_needed=1
        wake_reason="$changed"
        wake_mode="reactive"
    fi

    # CHECK 2: Zombie crashed (tmux died)
    if [[ "$wake_needed" -eq 0 ]]; then
        if dead="$(check_zombie_alive 2>/dev/null)"; then
            wake_needed=1
            wake_reason="$dead"
            wake_mode="crash"
        fi
    fi

    # CHECK 3: Zombie stalled (working but no commits)
    if [[ "$wake_needed" -eq 0 ]]; then
        if stalled="$(check_stalled 2>/dev/null)"; then
            wake_needed=1
            wake_reason="$stalled"
            wake_mode="stall"
        fi
    fi

    # CHECK 3.5: Hallucination — agent claims done but has zero git changes
    if frauds="$(check_hallucination 2>/dev/null)"; then
        log "Hallucination auto-rejected:${frauds}"
        # Don't wake brain — we already reset the agent
    fi

    # CHECK 4: Zombies done but brain hasn't confirmed
    # ALWAYS check — override reactive wake with review wake if pending
    if pending="$(check_pending_review 2>/dev/null)"; then
        wake_needed=1
        wake_reason="$pending"
        wake_mode="review"  # review mode overrides reactive — tells brain to verify + write DECISION
    fi

    # FIRE if any check triggered
    if [[ "$wake_needed" -eq 1 ]]; then
        wake_brain "$wake_reason" "$wake_mode" 2>/dev/null || true
    fi

    # PROACTIVE: periodic forced check
    last_proactive=0
    [[ -f "$LAST_PROACTIVE_FILE" ]] && last_proactive="$(cat "$LAST_PROACTIVE_FILE")"
    now="$(date +%s)"
    elapsed=$((now - last_proactive))

    if [[ "$elapsed" -ge "$PROACTIVE_INTERVAL" ]]; then
        echo "[nerve] $(date '+%H:%M:%S') Proactive check ($((elapsed/60))m since last brain activity)"
        wake_brain "Routine $((elapsed/60))m check" "proactive" 2>/dev/null || true
    fi

    sleep "$SCAN_INTERVAL"
done
