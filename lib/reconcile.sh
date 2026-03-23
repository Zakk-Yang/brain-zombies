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
    for status_file in "${BZ_DIR}/agents"/*/STATUS.md; do
        [[ -f "$status_file" ]] || continue
        has_agents=1
        local state
        state="$(grep '^State:' "$status_file" 2>/dev/null | head -1 | sed 's/State: //')"
        [[ "$state" == "done" ]] || return 1
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
        [[ "$state" == "done" || "$state" == "blocked" || "$state" == "starting" ]] && continue

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

# ── Brain Wake (COSTS TOKENS) ───────────────────

wake_brain() {
    local reason="$1"
    local mode="$2"   # reactive | proactive | crash | stall

    local sess="bz-${PROJECT_NAME}-supervisor"
    if ! tmux has-session -t "$sess" 2>/dev/null; then
        echo "[nerve] WARNING: brain session not running!"
        return 1
    fi

    local message
    case "$mode" in
        reactive)
            message="NERVE SIGNAL: Zombie state change —${reason}. Read all .bz/agents/*/STATUS.md and coordinate." ;;
        crash)
            message="ZOMBIE DOWN:${reason} — tmux session died. Check if task was complete, restart if needed, or reassign." ;;
        stall)
            message="STALL DETECTED:${reason} — no commits for 10+ min while State=working. Investigate: read STATUS.md, check worktree, redirect if stuck." ;;
        proactive)
            message="HEARTBEAT: Routine check. Verify all zombies making progress. Read .bz/agents/*/STATUS.md. Send decisions where needed." ;;
    esac

    tmux send-keys -t "$sess" "$message" Enter
    echo "[nerve] $(date '+%H:%M:%S') WAKE brain (${mode}):${reason}"

    # Reset proactive timer after any wake
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
