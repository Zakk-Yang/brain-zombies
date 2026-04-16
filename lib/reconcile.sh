#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════
# NERVE SYSTEM — smart gatekeeper for brain wake decisions
#
# Every 30s (FREE, bash only):
#   1. Scan STATUS.md for state changes    → wake brain if changed
#   2. Check tmux sessions alive           → wake brain if crashed
#   3. Check DuckDB state heartbeats       → wake brain if stalled
#   4. All clear?                          → sleep, save tokens
#
# Every 15m (PROACTIVE, costs tokens):
#   Force brain check for stuck zombies that didn't update STATUS.md
#
# Usage: bash reconcile.sh <project-root>
# ══════════════════════════════════════════════════════════════

PROJECT_ROOT="${1:?Usage: reconcile.sh <project-root>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BZ_DIR="${PROJECT_ROOT}/.bz"
LOG_DIR="${BZ_DIR}/logs"
SIGNATURES_FILE="${LOG_DIR}/.signatures"
LAST_PROACTIVE_FILE="${LOG_DIR}/.last-proactive"
LAST_COMMIT_FILE="${LOG_DIR}/.last-commits"
PROJECT_STATE_DIR="${BZ_DIR}/project"
MEMORY_DIR="${PROJECT_STATE_DIR}/memories"
BRAIN_MEMORY_FILE="${MEMORY_DIR}/brain_mem.md"
SHARED_MEMORY_FILE="${MEMORY_DIR}/shared_mem.md"

SCAN_INTERVAL=30
PROACTIVE_INTERVAL=900   # 15 min default
HEARTBEAT_INTERVAL=600   # 10 min without state heartbeat = stalled
BRAIN_ERROR_THRESHOLD=3          # consecutive failures before suppressing a trigger
BRAIN_ERROR_SUPPRESS_WINDOW=1800 # 30 min suppression window
BRAIN_ERRORS_DIR="${LOG_DIR}/.brain-errors"

mkdir -p "$LOG_DIR" "$MEMORY_DIR" "$BRAIN_ERRORS_DIR"

# Read config
if [[ -f "${PROJECT_ROOT}/bz.yaml" ]]; then
    PROACTIVE_INTERVAL="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(int(d.get('supervisor',{}).get('proactive_check_mins', 15)) * 60)
" 2>/dev/null || echo 900)"
    HEARTBEAT_INTERVAL="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(int(d.get('supervisor',{}).get('zombie_heartbeat_mins', 10)) * 60)
" 2>/dev/null || echo 600)"
fi

# ── Helpers ──────────────────────────────────────

log() {
    echo "[nerve] $(date '+%H:%M:%S') $*"
}

kill_tmux_session_tree() {
    local sess="$1"

    for pane_pid in $(tmux list-panes -t "$sess" -F '#{pane_pid}' 2>/dev/null); do
        pkill -TERM -P "$pane_pid" 2>/dev/null || true
        for child in $(pgrep -P "$pane_pid" 2>/dev/null); do
            pkill -TERM -P "$child" 2>/dev/null || true
        done
    done

    tmux kill-session -t "$sess" 2>/dev/null || true
}

project_name() {
    python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    print(yaml.safe_load(f).get('project',{}).get('name',''))
" 2>/dev/null || echo "unknown"
}

control_plane() {
    python3 "${SCRIPT_DIR}/control_plane.py" --project-root "${PROJECT_ROOT}" "$@"
}

PROJECT_NAME="$(project_name)"

status_field() {
    local status_file="$1"
    local key="$2"
    grep "^${key}:" "$status_file" 2>/dev/null | head -1 | sed "s/^${key}: //"
}

agent_status_file() {
    echo "${BZ_DIR}/agents/$1/STATUS.md"
}

agent_memory_file() {
    echo "${MEMORY_DIR}/$1_mem.md"
}

decision_fresh_for_status() {
    local status_file="$1"
    local decision_file="$2"
    [[ -f "$status_file" && -f "$decision_file" && "$decision_file" -nt "$status_file" ]]
}

phase_expects_live_session() {
    local phase="${1,,}"
    case "$phase" in
        starting|planning|working|executing|testing|iterating) return 0 ;;
        *) return 1 ;;
    esac
}

total_minutes_budget_reason() {
    control_plane budget-status 2>/dev/null | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(1)

total = data.get("total_minutes", {})
if total.get("exhausted"):
    used = total.get("used", 0)
    maximum = total.get("max", 0)
    if maximum:
        print(f"max_total_minutes reached: {used}/{maximum}")
    else:
        print(f"max_total_minutes reached: {used}")
' 2>/dev/null
}

shutdown_budget_exhausted_run() {
    local reason="$1"
    log "Automatic supervision paused: ${reason}"
    control_plane write-state \
        --agent supervisor \
        --phase blocked \
        --action "budget exhausted" \
        --summary "Automatic supervision paused because the total runtime budget was exhausted." \
        --depends-on "" \
        --needs-brain no \
        --files "" \
        --next-step "Human can extend the budget in bz.yaml and relaunch supervision." \
        --blocker "budget exhausted: ${reason}" \
        --updated-by system \
        --source reconcile >/dev/null 2>&1 || true
    control_plane record-event \
        --type budget_exhausted \
        --source reconcile \
        --summary "Automatic supervision paused because the total runtime budget was exhausted." \
        --details "${reason}" >/dev/null 2>&1 || true

    tmux list-sessions 2>/dev/null | grep "bz-${PROJECT_NAME}" | cut -d: -f1 | while read -r sess; do
        [[ -n "$sess" ]] || continue
        case "$sess" in
            *-dashboard|*-nerve) continue ;;
        esac
        kill_tmux_session_tree "$sess"
        log "Stopped session due to exhausted total budget: ${sess}"
    done

    rm -f "${BZ_DIR}/reconcile.pid"
    exit 0
}

# ── Brain error circuit breaker ──────────────────
# Prevents the supervisor loop from re-firing the same wake trigger after the
# brain CLI has failed repeatedly — the pattern that burned 168 consecutive
# [BRAIN ERROR] responses in the quant-lab-feature-research run.

brain_trigger_key() {
    local mode="$1"
    local reason="$2"
    printf '%s|%s' "$mode" "$reason" | head -c 400 | md5sum 2>/dev/null | awk '{print $1}'
}

brain_trigger_error_file() {
    echo "${BRAIN_ERRORS_DIR}/$1"
}

brain_trigger_suppressed() {
    local key="$1"
    local file
    file="$(brain_trigger_error_file "$key")"
    [[ -f "$file" ]] || return 1
    local now
    now="$(date +%s)"
    local kept=""
    local count=0
    while IFS= read -r epoch; do
        [[ -z "$epoch" ]] && continue
        if [[ "$((now - epoch))" -lt "$BRAIN_ERROR_SUPPRESS_WINDOW" ]]; then
            kept+="${epoch}"$'\n'
            count=$((count + 1))
        fi
    done < "$file"
    if [[ -z "$kept" ]]; then
        rm -f "$file"
    else
        printf '%s' "$kept" > "$file"
    fi
    [[ "$count" -ge "$BRAIN_ERROR_THRESHOLD" ]]
}

brain_trigger_record_failure() {
    local key="$1"
    mkdir -p "$BRAIN_ERRORS_DIR"
    date +%s >> "$(brain_trigger_error_file "$key")"
}

brain_trigger_clear() {
    rm -f "$(brain_trigger_error_file "$1")" 2>/dev/null || true
}

ensure_brain_memory() {
    if [[ ! -f "$BRAIN_MEMORY_FILE" ]]; then
        cat > "$BRAIN_MEMORY_FILE" <<'EOF'
# Brain Memory

## Coordination notes
- Track cross-agent dependencies, review outcomes, and recurring failure modes here.

## Global blockers
- none

## Open handoffs
- none
EOF
    fi
    if [[ ! -f "$SHARED_MEMORY_FILE" ]]; then
        cat > "$SHARED_MEMORY_FILE" <<'EOF'
# Shared Memory

## Summary
- none
EOF
    fi
}

write_status_file() {
    local path="$1"; shift
    local agent_id="$1"; shift
    local state="$1"; shift
    local action="$1"; shift
    local summary="$1"; shift
    local files_touched="$1"; shift
    local depends_on="$1"; shift
    local needs_brain="$1"; shift
    local next_step="$1"; shift
    local blocker="$1"; shift
    local memory_path="${1:-$(agent_memory_file "$agent_id")}"

    cat > "$path" <<EOF
# STATUS.md
State: ${state}
Action: ${action}
Summary: ${summary}
Files touched: ${files_touched}
Depends on: ${depends_on}
Needs brain: ${needs_brain}
Next step: ${next_step}
Blocker: ${blocker}
Memory: ${memory_path}
Last updated: $(date '+%Y-%m-%d %H:%M')
EOF
}

memory_excerpt() {
    local path="$1"
    local lines="${2:-12}"
    [[ -f "$path" ]] || return 0
    tail -n "$lines" "$path"
}

append_brain_memory() {
    local header="$1"
    local body="$2"
    ensure_brain_memory
    {
        echo ""
        echo "## $(date '+%Y-%m-%d %H:%M') — ${header}"
        echo "- ${body}"
    } >> "$BRAIN_MEMORY_FILE"
}

push_protocol_to_worktree() {
    local agent_id="$1"
    local wt="${BZ_DIR}/worktrees/${agent_id}"
    [[ -d "$wt" ]] || return 0

    local rel_bz="${BZ_DIR#${PROJECT_ROOT}/}"
    local rel_memory_dir="${MEMORY_DIR#${PROJECT_ROOT}/}"
    local rel_brain="${BRAIN_MEMORY_FILE#${PROJECT_ROOT}/}"
    local rel_shared="${SHARED_MEMORY_FILE#${PROJECT_ROOT}/}"
    local agent_memory_src
    agent_memory_src="$(agent_memory_file "$agent_id")"
    local rel_agent_memory="${agent_memory_src#${PROJECT_ROOT}/}"

    mkdir -p \
        "${wt}/${rel_bz}/agents/${agent_id}" \
        "${wt}/${rel_bz}/project/souls" \
        "${wt}/${rel_memory_dir}" \
        "${wt}/${rel_bz}/project/plans" \
        "${wt}/${rel_bz}/project/outputs/brain" \
        "${wt}/${rel_bz}/project/outputs/${agent_id}" \
        "${wt}/${rel_bz}/project/chatlogs" \
        "${wt}/${rel_bz}/project/scheduler"
    [[ -f "${BZ_DIR}/agents/${agent_id}/STATUS.md" ]] && cp "${BZ_DIR}/agents/${agent_id}/STATUS.md" "${wt}/${rel_bz}/agents/${agent_id}/STATUS.md"
    [[ -f "${BZ_DIR}/agents/${agent_id}/DECISION.md" ]] && cp "${BZ_DIR}/agents/${agent_id}/DECISION.md" "${wt}/${rel_bz}/agents/${agent_id}/DECISION.md"
    [[ -f "${PROJECT_STATE_DIR}/PROJECT.md" ]] && cp "${PROJECT_STATE_DIR}/PROJECT.md" "${wt}/${rel_bz}/project/PROJECT.md"
    [[ -f "${PROJECT_STATE_DIR}/TARGET.md" ]] && cp "${PROJECT_STATE_DIR}/TARGET.md" "${wt}/${rel_bz}/project/TARGET.md"
    [[ -f "${PROJECT_STATE_DIR}/souls/brain_soul.md" ]] && cp "${PROJECT_STATE_DIR}/souls/brain_soul.md" "${wt}/${rel_bz}/project/souls/brain_soul.md"
    [[ -f "${PROJECT_STATE_DIR}/souls/${agent_id}_soul.md" ]] && cp "${PROJECT_STATE_DIR}/souls/${agent_id}_soul.md" "${wt}/${rel_bz}/project/souls/${agent_id}_soul.md"
    [[ -f "$agent_memory_src" ]] && cp "$agent_memory_src" "${wt}/${rel_agent_memory}"
    [[ -f "$BRAIN_MEMORY_FILE" ]] && cp "$BRAIN_MEMORY_FILE" "${wt}/${rel_brain}"
    [[ -f "$SHARED_MEMORY_FILE" ]] && cp "$SHARED_MEMORY_FILE" "${wt}/${rel_shared}"
    [[ -f "${PROJECT_STATE_DIR}/plans/${agent_id}_plan.md" ]] && cp "${PROJECT_STATE_DIR}/plans/${agent_id}_plan.md" "${wt}/${rel_bz}/project/plans/${agent_id}_plan.md"
    [[ -f "${PROJECT_STATE_DIR}/scheduler/policy.yaml" ]] && cp "${PROJECT_STATE_DIR}/scheduler/policy.yaml" "${wt}/${rel_bz}/project/scheduler/policy.yaml"
}

dependency_targets() {
    local depends="$1"
    [[ -n "$depends" ]] || return 0
    printf '%s\n' "$depends" \
        | tr ',' '\n' \
        | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//' \
        | sed '/^$/Id; /^none$/Id'
}

dependencies_satisfied() {
    local depends="$1"
    local seen=0
    local target
    while IFS= read -r target; do
        [[ -n "$target" ]] || continue
        seen=1
        [[ "$target" == "human" || "$target" == "all-agents" ]] && return 1
        local dep_status
        dep_status="$(agent_status_file "$target")"
        [[ -f "$dep_status" ]] || return 1
        local dep_state
        dep_state="$(status_field "$dep_status" "State" | tr '[:upper:]' '[:lower:]')"
        [[ "$dep_state" == "done" || "$dep_state" == "finished" ]] || return 1
    done < <(dependency_targets "$depends")

    [[ "$seen" -eq 1 ]]
}

deliver_action_to_agent() {
    local target="$1"
    local action_summary
    action_summary="$(control_plane latest-action --agent "$target" --format summary 2>/dev/null || echo "new action")"

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
        tmux send-keys -t "$zombie_sess" "NEW ACTION queued. Read .bz/control/contexts/${target}.md and .bz/control/agents/${target}/latest-action.md now, then act." Enter
        log "Delivered queued action to running ${target}: ${action_summary}"
        return 0
    fi

    log "Restarting ${target} CLI to deliver queued action"
    tmux kill-session -t "$zombie_sess" 2>/dev/null || true

    local wt_path="${BZ_DIR}/worktrees/${target}"
    local work_dir="${PROJECT_ROOT}"
    [[ -d "$wt_path" ]] && work_dir="$wt_path"
    local abs_work_dir
    abs_work_dir="$(realpath "$work_dir")"

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

    local restart_prompt="NEW ACTION queued.

Read .bz/control/contexts/${target}.md and .bz/control/agents/${target}/latest-action.md first.
Then execute the action immediately."
    local restart_prompt_file="${BZ_DIR}/agents/${target}/RESTART_PROMPT.txt"
    echo "$restart_prompt" > "$restart_prompt_file"

    local restart_cmd
    if [[ "$z_cli" == "claude" ]]; then
        restart_cmd="cd ${abs_work_dir} && claude --dangerously-skip-permissions --model ${z_model} -p \"\$(cat ${restart_prompt_file})\""
    elif [[ "$z_cli" == "codex" ]]; then
        restart_cmd="cd ${abs_work_dir} && codex exec --full-auto --add-dir ${PROJECT_ROOT} --model ${z_model} \"\$(cat ${restart_prompt_file})\""
    else
        restart_cmd="cd ${abs_work_dir} && ${z_cli} \"\$(cat ${restart_prompt_file})\""
    fi

    tmux new-session -d -s "$zombie_sess" -x 200 -y 50
    tmux send-keys -t "$zombie_sess" "$restart_cmd" Enter
    log "Dispatched queued action to ${target}: ${action_summary}"
}

update_status_after_decision() {
    local target="$1"
    local decision_type="$2"
    local detail="$3"
    local status_file
    status_file="$(agent_status_file "$target")"
    [[ -f "$status_file" ]] || return 0

    local state files depends memory
    state="$(status_field "$status_file" "State")"
    files="$(status_field "$status_file" "Files touched")"
    depends="$(status_field "$status_file" "Depends on")"
    memory="$(status_field "$status_file" "Memory")"

    local action="$decision_type"
    local summary="$detail"
    local next_step="$detail"
    local blocker="none"
    local needs_brain="no"

    case "$decision_type" in
        accept|complete)
            state="done"
            action="brain accepted work"
            summary="Brain accepted work."
            next_step="Wait for merge or next assignment."
            ;;
        redirect|reject)
            state="working"
            action="following brain redirect"
            summary="Brain redirect: ${detail:-follow updated instructions}."
            next_step="${detail:-Follow brain redirect.}"
            ;;
        unblock)
            state="working"
            action="following brain unblock"
            summary="Brain removed blocker."
            next_step="${detail:-Continue execution.}"
            blocker="none"
            ;;
        restart)
            state="working"
            action="restarting after brain intervention"
            summary="Brain requested a restart."
            next_step="${detail:-Restart task execution.}"
            ;;
        hold)
            state="blocked"
            action="waiting on brain hold"
            summary="Brain placed this task on hold."
            next_step="Wait for new instructions."
            blocker="${detail:-brain hold}"
            ;;
        status-check)
            state="working"
            action="responding to heartbeat check"
            summary="Brain requested a status update."
            next_step="${detail:-Update state, memory, and current task progress now.}"
            ;;
        *)
            action="following brain decision"
            summary="Brain decision: ${detail:-follow latest decision}."
            next_step="${detail:-Read DECISION.md and act.}"
            ;;
    esac

    write_status_file \
        "$status_file" \
        "$target" \
        "$state" \
        "$action" \
        "$summary" \
        "${files:-none}" \
        "${depends:-none}" \
        "$needs_brain" \
        "$next_step" \
        "$blocker" \
        "${memory:-$(agent_memory_file "$target")}"
    push_protocol_to_worktree "$target"
}

capture_signatures() {
    for status_file in "${BZ_DIR}/agents"/*/STATUS.md; do
        [[ -f "$status_file" ]] || continue
        local agent_id
        agent_id="$(basename "$(dirname "$status_file")")"
        [[ "$agent_id" == "supervisor" ]] && continue
        local sig
        sig="$(grep -E '^(State|Action|Depends on|Needs brain|Blocker):' "$status_file" 2>/dev/null | tr '\n' '|')"
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
        state="$(status_field "${agent_dir}/STATUS.md" "State")"
        # Only brain-confirmed terminal states can finish the whole run.
        if [[ "$state" != "done" && "$state" != "finished" ]]; then
            return 1
        fi

        local needs_brain blocker
        needs_brain="$(status_field "${agent_dir}/STATUS.md" "Needs brain" | tr '[:upper:]' '[:lower:]')"
        blocker="$(status_field "${agent_dir}/STATUS.md" "Blocker" | tr '[:upper:]' '[:lower:]')"
        if [[ -n "$needs_brain" && "$needs_brain" != "no" && "$needs_brain" != "none" ]]; then
            return 1
        fi
        if [[ -n "$blocker" && "$blocker" != "no" && "$blocker" != "none" ]]; then
            return 1
        fi
    done
    [[ "$has_agents" -eq 1 ]] && return 0 || return 1
}

shutdown_finished_run() {
    log "All zombies finished and brain-confirmed. Stopping background agent tasks."
    control_plane write-state \
        --agent supervisor \
        --phase done \
        --action "project finished" \
        --summary "All zombies finished and brain confirmed completion." \
        --depends-on "" \
        --needs-brain no \
        --files "" \
        --next-step "none" \
        --blocker none \
        --updated-by system \
        --source reconcile >/dev/null 2>&1 || true
    control_plane record-event \
        --type project_finished \
        --source reconcile \
        --summary "All zombies finished and brain confirmed completion." \
        --details "Reconcile loop stopped zombie tmux sessions and exited." >/dev/null 2>&1 || true

    for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null | grep "^bz-${PROJECT_NAME}-" || true); do
        case "$sess" in
            *-supervisor|*-dashboard|*-nerve) ;;
            *)
                for pane_pid in $(tmux list-panes -t "$sess" -F '#{pane_pid}' 2>/dev/null); do
                    pkill -TERM -P "$pane_pid" 2>/dev/null || true
                    for child in $(pgrep -P "$pane_pid" 2>/dev/null); do
                        pkill -TERM -P "$child" 2>/dev/null || true
                    done
                done
                sleep 1
                tmux kill-session -t "$sess" 2>/dev/null || true
                log "Stopped finished session: $sess"
                ;;
        esac
    done

    rm -f "${BZ_DIR}/reconcile.pid"
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

sync_memory_from_worktrees() {
    for wt_memory in "${BZ_DIR}/worktrees"/*/".bz/project/memories"/*_mem.md; do
        [[ -f "$wt_memory" ]] || continue
        local fname
        fname="$(basename "$wt_memory")"
        local main_memory="${MEMORY_DIR}/${fname}"
        if [[ ! -f "$main_memory" || "$wt_memory" -nt "$main_memory" ]]; then
            cp "$wt_memory" "$main_memory"
        fi
    done

    for wt_shared in "${BZ_DIR}/worktrees"/*/".bz/project/memories/shared_mem.md"; do
        [[ -f "$wt_shared" ]] || continue
        if [[ ! -f "$SHARED_MEMORY_FILE" || "$wt_shared" -nt "$SHARED_MEMORY_FILE" ]]; then
            cp "$wt_shared" "$SHARED_MEMORY_FILE"
        fi
    done
}

sync_outputs_from_worktrees() {
    for wt in "${BZ_DIR}/worktrees"/*; do
        [[ -d "$wt" ]] || continue
        for wt_output in "$wt"/".bz/project/outputs"/*/*; do
            [[ -f "$wt_output" ]] || continue
            local rel_path main_output
            rel_path="${wt_output#${wt}/}"
            main_output="${PROJECT_ROOT}/${rel_path}"
            if [[ ! -f "$main_output" || "$wt_output" -nt "$main_output" ]]; then
                mkdir -p "$(dirname "$main_output")"
                cp "$wt_output" "$main_output"
            fi
        done
    done
}

sync_outputs_to_worktrees() {
    for wt in "${BZ_DIR}/worktrees"/*; do
        [[ -d "$wt" ]] || continue
        for main_output in "${PROJECT_STATE_DIR}/outputs"/*/*; do
            [[ -f "$main_output" ]] || continue
            local rel_path wt_output
            rel_path="${main_output#${PROJECT_ROOT}/}"
            wt_output="${wt}/${rel_path}"
            if [[ ! -f "$wt_output" || "$main_output" -nt "$wt_output" ]]; then
                mkdir -p "$(dirname "$wt_output")"
                cp "$main_output" "$wt_output"
            fi
        done
    done
}

is_framework_path() {
    local rel="${1#./}"
    case "$rel" in
        ""|.bz|.bz/*|.git|.git/*|.codex|.codex/*) return 0 ;;
        *) return 1 ;;
    esac
}

project_base_ref() {
    local base
    base="$(git -C "$PROJECT_ROOT" symbolic-ref --short HEAD 2>/dev/null || true)"
    [[ -n "$base" ]] || base="master"
    echo "$base"
}

worktree_changed_files() {
    local wt="$1"
    [[ -d "$wt" ]] || return 0

    local base_ref
    base_ref="$(project_base_ref)"
    {
        git -C "$wt" diff --name-only --diff-filter=ACMRT HEAD 2>/dev/null || true
        git -C "$wt" diff --name-only --diff-filter=ACMRT "${base_ref}...HEAD" 2>/dev/null || true
        git -C "$wt" ls-files --others --exclude-standard 2>/dev/null || true
    } | sort -u
}

worktree_deliverable_files() {
    local wt="$1"
    worktree_changed_files "$wt" | while IFS= read -r rel; do
        rel="${rel#./}"
        [[ -n "$rel" ]] || continue
        is_framework_path "$rel" && continue
        [[ -f "${wt}/${rel}" ]] || continue
        printf '%s\n' "$rel"
    done
}

worktree_has_changes() {
    local wt="$1"
    [[ -n "$(worktree_changed_files "$wt" | head -1)" ]]
}

promote_worktree_deliverables() {
    local agent_id="$1"
    local wt="${BZ_DIR}/worktrees/${agent_id}"
    [[ -d "$wt" ]] || return 0

    local promoted=0
    local rel
    while IFS= read -r rel; do
        local src="${wt}/${rel}"
        local dest="${PROJECT_ROOT}/${rel}"
        [[ -f "$src" ]] || continue
        if [[ -f "$dest" ]] && cmp -s "$src" "$dest"; then
            continue
        fi
        mkdir -p "$(dirname "$dest")"
        cp "$src" "$dest"
        promoted=$((promoted + 1))
        log "Promoted ${agent_id} deliverable: ${rel}"
    done < <(worktree_deliverable_files "$wt")

    if [[ "$promoted" -gt 0 ]]; then
        control_plane record-event \
            --type deliverables_promoted \
            --source reconcile \
            --summary "Promoted ${promoted} root deliverable(s) from ${agent_id}." \
            --details "Copied accepted non-.bz files from ${wt} into ${PROJECT_ROOT}." >/dev/null 2>&1 || true
    fi
}

promote_done_worktrees() {
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local state needs_brain blocker
        state="$(status_field "${agent_dir}/STATUS.md" "State")"
        needs_brain="$(status_field "${agent_dir}/STATUS.md" "Needs brain" | tr '[:upper:]' '[:lower:]')"
        blocker="$(status_field "${agent_dir}/STATUS.md" "Blocker" | tr '[:upper:]' '[:lower:]')"
        [[ "$state" == "done" || "$state" == "finished" ]] || continue
        [[ -z "$needs_brain" || "$needs_brain" == "no" || "$needs_brain" == "none" ]] || continue
        [[ -z "$blocker" || "$blocker" == "no" || "$blocker" == "none" ]] || continue

        promote_worktree_deliverables "$agent_id"
    done
}

release_satisfied_dependencies() {
    local released=""
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local status_file state depends blocker
        status_file="${agent_dir}/STATUS.md"
        [[ -f "$status_file" ]] || continue
        state="$(status_field "$status_file" "State" | tr '[:upper:]' '[:lower:]')"
        [[ "$state" == "blocked" ]] || continue

        depends="$(status_field "$status_file" "Depends on")"
        [[ -n "$depends" && "${depends,,}" != "none" ]] || continue

        blocker="$(status_field "$status_file" "Blocker" | tr '[:upper:]' '[:lower:]')"
        [[ "$blocker" == *"brain hold"* || "$blocker" == *"budget exhausted"* ]] && continue

        dependencies_satisfied "$depends" || continue

        local files_touched
        files_touched="$(status_field "$status_file" "Files touched")"
        local detail="Dependencies satisfied: ${depends}. Read .bz/control/contexts/${agent_id}.md and continue execution."

        control_plane queue-action \
            --from system \
            --to "$agent_id" \
            --kind unblock \
            --summary "Dependency satisfied; resume work." \
            --details "$detail" \
            --reason "Upstream dependency completed." >/dev/null 2>&1 || true
        control_plane write-state \
            --agent "$agent_id" \
            --phase working \
            --action "resuming after dependency" \
            --summary "Dependencies satisfied; resume work." \
            --depends-on "" \
            --needs-brain no \
            --files "${files_touched:-}" \
            --next-step "Read latest action and continue execution." \
            --blocker none \
            --updated-by system \
            --source reconcile >/dev/null 2>&1 || true

        deliver_action_to_agent "$agent_id" >&2
        released="${released} ${agent_id}"
    done

    [[ -n "$released" ]] && echo "$released" && return 0
    return 1
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
        state="$(status_field "${agent_dir}/STATUS.md" "State")"

        # Only check agents claiming completion
        [[ "$state" != "done" && "$state" != "ready-for-review" ]] && continue

        # Brain-accepted done work should not be re-opened by this heuristic.
        [[ "$state" == "done" && -f "${agent_dir}/DECISION.md" ]] && continue

        # Check git diff in worktree — should have commits beyond main
        local wt="${BZ_DIR}/worktrees/${agent_id}"
        if [[ -d "$wt" ]] && ( [[ -d "$wt/.git" ]] || [[ -f "$wt/.git" ]] ); then
            local main_head
            main_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
            local wt_head
            wt_head="$(git -C "$wt" rev-parse HEAD 2>/dev/null || echo "")"

            if [[ -n "$main_head" && "$main_head" == "$wt_head" ]] && ! worktree_has_changes "$wt"; then
                # Zero commits beyond main — agent is lying
                local files_touched
                files_touched="$(grep '^Files touched:' "${agent_dir}/STATUS.md" 2>/dev/null | head -1 | sed 's/Files touched: //')"
                if [[ "$files_touched" != "none" && -n "$files_touched" ]]; then
                    # Claims files but has no commits — hallucination
                    frauds="${frauds} ${agent_id}"
                    log "HALLUCINATION DETECTED: ${agent_id} claims '${state}' with files '${files_touched}' but has zero git changes"

                    # Auto-reject: reset status and restart
                    write_status_file \
                        "${agent_dir}/STATUS.md" \
                        "${agent_id}" \
                        "working" \
                        "recovering from rejected completion" \
                        "Previous completion was rejected because no git changes were found." \
                        "none" \
                        "$(status_field "${agent_dir}/STATUS.md" "Depends on")" \
                        "no" \
                        "Re-read BRIEF.md and do the actual work. Commit files as you go." \
                        "none" \
                        "$(status_field "${agent_dir}/STATUS.md" "Memory")"
                fi
            fi
        fi
    done

    [[ -n "$frauds" ]] && echo "$frauds" && return 0
    return 1
}

check_brain_requests() {
    local pending=""
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local status_file="${agent_dir}/STATUS.md"
        local needs_brain
        needs_brain="$(status_field "$status_file" "Needs brain" | tr '[:upper:]' '[:lower:]')"
        [[ -z "$needs_brain" || "$needs_brain" == "no" || "$needs_brain" == "none" ]] && continue

        pending="${pending} ${agent_id}(${needs_brain})"
    done

    [[ -n "$pending" ]] && echo "$pending" && return 0
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
        state="$(status_field "${agent_dir}/STATUS.md" "State" | tr '[:upper:]' '[:lower:]')"
        phase_expects_live_session "$state" || continue

        if ! tmux has-session -t "$sess" 2>/dev/null; then
            dead="${dead} ${agent_id}"
        fi
    done

    [[ -n "$dead" ]] && echo "$dead" && return 0
    return 1
}

# Check 3: active zombie missed heartbeat
check_stalled() {
    local stalled=""
    stalled="$(control_plane stale-agents --heartbeat-mins "$((HEARTBEAT_INTERVAL/60))" --format names 2>/dev/null | tr '\n' ' ' || true)"

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
        state="$(status_field "${agent_dir}/STATUS.md" "State")"
        local needs_brain
        needs_brain="$(status_field "${agent_dir}/STATUS.md" "Needs brain" | tr '[:upper:]' '[:lower:]')"

        local status_file="${agent_dir}/STATUS.md"
        local decision_file="${agent_dir}/DECISION.md"

        # ready-for-review needs a brain review unless the latest decision was
        # written after the status. Older decisions may be previous unblocks.
        if [[ "$state" == "ready-for-review" || "$needs_brain" == "review" ]]; then
            if ! decision_fresh_for_status "$status_file" "$decision_file"; then
                pending="${pending} ${agent_id}"
            fi
        elif [[ "$state" == "done" && ! -f "$decision_file" ]]; then
            pending="${pending} ${agent_id}"
        fi
    done

    [[ -n "$pending" ]] && echo "$pending" && return 0
    return 1
}

has_actionable_proactive_targets() {
    for agent_dir in "${BZ_DIR}/agents"/*/; do
        [[ -d "$agent_dir" ]] || continue
        local agent_id
        agent_id="$(basename "$agent_dir")"
        [[ "$agent_id" == "supervisor" ]] && continue

        local status_file="${agent_dir}/STATUS.md"
        local state blocker needs_brain
        state="$(status_field "$status_file" "State" | tr '[:upper:]' '[:lower:]')"
        blocker="$(status_field "$status_file" "Blocker" | tr '[:upper:]' '[:lower:]')"
        needs_brain="$(status_field "$status_file" "Needs brain" | tr '[:upper:]' '[:lower:]')"

        if [[ "$state" == "ready-for-review" ]]; then
            echo "$agent_id"
            return 0
        fi
        if [[ -n "$needs_brain" && "$needs_brain" != "no" && "$needs_brain" != "none" ]]; then
            echo "$agent_id"
            return 0
        fi
        if [[ "$blocker" == *"budget exhausted"* || "$blocker" == *"brain hold"* ]]; then
            continue
        fi
        if phase_expects_live_session "$state"; then
            echo "$agent_id"
            return 0
        fi
    done

    return 1
}

# Gather an agent's actual work for brain review
gather_agent_work() {
    local agent_id="$1"
    local work=""
    local wt="${BZ_DIR}/worktrees/${agent_id}"
    local agent_dir="${BZ_DIR}/agents/${agent_id}"
    local memory_file
    memory_file="$(agent_memory_file "$agent_id")"

    # Full STATUS.md
    if [[ -f "${agent_dir}/STATUS.md" ]]; then
        work="${work}
--- STATUS.md ---
$(cat "${agent_dir}/STATUS.md")"
    fi

    # Git log (commits by this agent)
    if [[ -d "$wt/.git" ]] || [[ -f "$wt/.git" ]]; then
        local branch_commits
        local base_ref
        base_ref="$(project_base_ref)"
        branch_commits="$(git -C "$wt" log --format='%s' "${base_ref}..HEAD" 2>/dev/null | head -20)"
        if [[ -n "$branch_commits" ]]; then
            work="${work}

--- Git commits (branch summaries vs ${base_ref}) ---
${branch_commits}"
        fi

        local deliverables
        deliverables="$(worktree_deliverable_files "$wt" | head -30)"
        if [[ -n "$deliverables" ]]; then
            work="${work}

--- Candidate root deliverables ---
${deliverables}"
            while IFS= read -r rel; do
                [[ -f "${wt}/${rel}" ]] || continue
                local byte_count
                byte_count="$(wc -c < "${wt}/${rel}" 2>/dev/null || echo 0)"
                if [[ "$byte_count" -le 20000 ]] && grep -Iq . "${wt}/${rel}" 2>/dev/null; then
                    local file_excerpt
                    file_excerpt="$(head -80 "${wt}/${rel}")"
                    work="${work}

--- ${rel} (first 80 lines) ---
${file_excerpt}"
                else
                    work="${work}

--- ${rel} ---
[${byte_count} bytes; binary or large content omitted]"
                fi
            done <<< "$deliverables"
        fi
    fi

    # Output files: reports, results (truncated to avoid prompt explosion)
    for report in "${wt}/.bz/project/outputs/${agent_id}/"* "${wt}/outputs/research/"*.md "${wt}/outputs/research/"*.json; do
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

    if [[ -f "$memory_file" ]]; then
        work="${work}

--- Agent memory (tail) ---
$(memory_excerpt "$memory_file" 14)"
    fi

    echo "$work"
}

run_brain_cli() {
    local brain_cli="$1"
    local brain_model="$2"
    local thinking_flag="$3"
    local prompt_file="$4"
    local output_file="$5"
    local error_file="$6"
    local mode="$7"
    local reason="$8"

    local attempt
    local max_attempts=2
    : > "$output_file"
    : > "$error_file"

    for attempt in $(seq 1 "$max_attempts"); do
        local output=""
        local status=0

        set +e
        if [[ "$brain_cli" == "claude" ]]; then
            output="$(cd "${PROJECT_ROOT}" && claude --dangerously-skip-permissions --model "$brain_model" $thinking_flag -p "$(cat "$prompt_file")" 2>"$error_file")"
            status=$?
        elif [[ "$brain_cli" == "codex" ]]; then
            output="$(cd "${PROJECT_ROOT}" && codex exec --full-auto --add-dir "${PROJECT_ROOT}" --model "$brain_model" "$(cat "$prompt_file")" 2>"$error_file")"
            status=$?
        else
            output="$(cd "${PROJECT_ROOT}" && ${brain_cli} "$(cat "$prompt_file")" 2>"$error_file")"
            status=$?
        fi
        set -e

        printf '%s' "$output" > "$output_file"
        [[ "$status" -eq 0 ]] && return 0

        local stderr_tail
        stderr_tail="$(tail -20 "$error_file" 2>/dev/null | tr '\n' ' ' | cut -c1-600)"
        log "Brain CLI attempt ${attempt}/${max_attempts} failed for mode=${mode} reason=${reason}: ${stderr_tail:-no stderr}"
        sleep $((attempt * 2))
    done

    echo "BRAIN ERROR" > "$output_file"
    return 1
}

# ── Brain Wake (COSTS TOKENS) ───────────────────

wake_brain() {
    local reason="$1"
    local mode="$2"   # reactive | proactive | crash | stall
    ensure_brain_memory
    control_plane sync-all --quiet >/dev/null 2>&1 || true

    local brain_context
    brain_context="$(control_plane render-context --viewer brain 2>/dev/null || true)"
    [[ -z "$brain_context" && -f "$BRAIN_MEMORY_FILE" ]] && brain_context="$(cat "$BRAIN_MEMORY_FILE")"

    local mode_brief=""
    local extra_context=""
    case "$mode" in
        reactive)
            mode_brief="A zombie state or action changed. Decide whether intervention is needed right now."
            ;;
        crash)
            mode_brief="One or more zombie tmux sessions died. Decide whether to restart, redirect, or hold affected zombies."
            ;;
        stall)
            mode_brief="One or more zombies missed the 10 minute state heartbeat. Issue a concrete status-check, unblock, redirect, restart, or hold action."
            ;;
        proactive)
            mode_brief="Run a proactive supervision pass. If no intervention is required, return an empty actions list."
            ;;
        review)
            mode_brief="Pending zombies are asking for review. Accept, redirect, or hold them based on evidence."
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
            extra_context="## Detailed Review Inputs
${review_details}"
            ;;
    esac

    local json_contract='Return exactly one JSON object with this shape:
{
  "brain_state": {
    "phase": "monitoring",
    "action": "short brain action",
    "summary": "short summary of what brain concluded",
    "depends_on": [],
    "needs_brain": "no",
    "next_step": "wait for next signal",
    "blocker": "none"
  },
  "brain_memory": [
    {
      "scope": "private or shared",
      "kind": "decision | handoff | constraint | result | observation",
      "summary": "durable memory in one line",
      "details": "compact durable detail",
      "tags": ["optional-tag"],
      "related_agents": ["optional-agent-id"]
    }
  ],
  "actions": [
    {
      "to": "agent-id",
      "kind": "accept | redirect | unblock | restart | hold | status-check",
      "summary": "one-line instruction/result",
      "details": "specific action the zombie should take next",
      "reason": "why this action is needed"
    }
  ]
}
Rules:
- use only the supplied context; do not run tools, inspect git, or execute commands
- actions must be [] if no zombie intervention is needed
- use kind=accept only when review evidence is good enough to mark the zombie done
- for root project deliverables, review Candidate root deliverables and Worktree status; reconcile promotes non-.bz files only after accept
- do not accept when STATUS has a non-none Blocker or the claimed implementation files are missing from the review evidence
- keep brain_memory compact and durable
- do not include markdown fences or any text before/after the JSON object'

    local prompt="${mode_brief}

Reason: ${reason}

## Canonical Brain Context
${brain_context}

${extra_context}

${json_contract}"

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

    local trigger_key
    trigger_key="$(brain_trigger_key "$mode" "$reason")"
    if [[ -n "$trigger_key" ]] && brain_trigger_suppressed "$trigger_key"; then
        log "Suppressing brain wake for mode=${mode} reason=${reason} — ${BRAIN_ERROR_THRESHOLD}+ consecutive BRAIN ERROR responses in last $((BRAIN_ERROR_SUPPRESS_WINDOW/60))m. Extend bz.yaml or clear ${BRAIN_ERRORS_DIR}/${trigger_key} to retry."
        return 0
    fi

    echo "[brain] $(date '+%H:%M:%S') WAKE (${mode}):${reason}"
    append_brain_memory "wake ${mode}" "Reason: ${reason}"

    # On-demand brain call — captured output
    local brain_output=""
    local brain_prompt_file="${BZ_DIR}/logs/brain-prompt-$(date +%s).txt"
    local brain_output_file="${BZ_DIR}/logs/brain-output-$(date +%s).txt"
    local brain_error_file="${BZ_DIR}/logs/brain-error-$(date +%s).txt"
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

    if ! run_brain_cli "$brain_cli" "$brain_model" "$thinking_flag" "$brain_prompt_file" "$brain_output_file" "$brain_error_file" "$mode" "$reason"; then
        log "Brain CLI failed for mode=${mode} reason=${reason}. Prompt: ${brain_prompt_file##*/} Error: ${brain_error_file##*/}"
    fi

    # Log brain output
    brain_output="$(cat "$brain_output_file" 2>/dev/null || true)"
    echo "[brain] $(date '+%H:%M:%S') RESPONSE: ${brain_output}" | head -20

    local queued_targets=""
    queued_targets="$(control_plane ingest-brain-output --output-file "$brain_output_file" --mode "$mode" --reason "$reason" 2>/dev/null || true)"

    while IFS= read -r target; do
        [[ -n "$target" ]] || continue

        local action_summary
        action_summary="$(control_plane latest-action --agent "$target" --format summary 2>/dev/null || echo "new brain action")"
        append_brain_memory "decision ${target}" "${action_summary}"
        deliver_action_to_agent "$target"
    done <<< "$queued_targets"

    if [[ "$brain_output" == "BRAIN ERROR" ]]; then
        [[ -n "$trigger_key" ]] && brain_trigger_record_failure "$trigger_key"
    else
        [[ -n "$trigger_key" ]] && brain_trigger_clear "$trigger_key"
        rm -f "$brain_prompt_file" "$brain_output_file" "$brain_error_file"
    fi

    # Reset proactive timer
    date +%s > "$LAST_PROACTIVE_FILE"
}

# ── Main Loop ────────────────────────────────────

echo "[nerve] Started — scan:${SCAN_INTERVAL}s proactive:$((PROACTIVE_INTERVAL/60))m heartbeat:$((HEARTBEAT_INTERVAL/60))m"
echo "[nerve] Watching: ${BZ_DIR}/agents/*/STATUS.md"

date +%s > "$LAST_PROACTIVE_FILE" 2>/dev/null || true
ensure_brain_memory

while true; do
    sync_status_from_worktrees 2>/dev/null || true
    sync_memory_from_worktrees 2>/dev/null || true
    sync_outputs_from_worktrees 2>/dev/null || true
    sync_outputs_to_worktrees 2>/dev/null || true
    control_plane sync-all --quiet >/dev/null 2>&1 || true
    promote_done_worktrees 2>/dev/null || true

    if budget_reason="$(total_minutes_budget_reason 2>/dev/null)"; then
        if [[ -n "$budget_reason" ]]; then
            shutdown_budget_exhausted_run "$budget_reason"
        fi
    fi
    if released="$(release_satisfied_dependencies)"; then
        log "Released satisfied dependencies:${released}"
    fi

    # Stop background work once every zombie is brain-confirmed finished.
    if all_done 2>/dev/null; then
        shutdown_finished_run
        exit 0
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

    # CHECK 1.2: explicit zombie request for brain attention
    if [[ "$wake_needed" -eq 0 ]]; then
        if brain_requests="$(check_brain_requests 2>/dev/null)"; then
            wake_needed=1
            wake_reason="$brain_requests"
            wake_mode="reactive"
        fi
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
        if proactive_targets="$(has_actionable_proactive_targets 2>/dev/null)"; then
            echo "[nerve] $(date '+%H:%M:%S') Proactive check ($((elapsed/60))m since last brain activity)"
            wake_brain "Routine $((elapsed/60))m check" "proactive" 2>/dev/null || true
        else
            log "Skipping proactive brain wake; no actionable targets remain."
            date +%s > "$LAST_PROACTIVE_FILE"
        fi
    fi

    sleep "$SCAN_INTERVAL"
done
