#!/usr/bin/env bash
set -euo pipefail

# Nerve system: two mechanisms to wake the brain
#   1. REACTIVE  — scan STATUS.md every 30s, wake brain on state change (free)
#   2. PROACTIVE — force brain check every N minutes to catch stuck zombies (costs tokens)
#
# Usage: bash reconcile.sh <project-root>

PROJECT_ROOT="${1:?Usage: reconcile.sh <project-root>}"
BZ_DIR="${PROJECT_ROOT}/.bz"
SIGNATURES_FILE="${BZ_DIR}/logs/.signatures"
LAST_PROACTIVE_FILE="${BZ_DIR}/logs/.last-proactive"

SCAN_INTERVAL=30          # seconds between STATUS.md scans (reactive)
PROACTIVE_INTERVAL=900    # seconds between forced brain checks (15 min default)

# Read proactive interval from config if set
if [[ -f "${PROJECT_ROOT}/bz.yaml" ]]; then
    configured="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    d = yaml.safe_load(f)
print(d.get('supervisor',{}).get('proactive_check_mins', 15))
" 2>/dev/null || echo 15)"
    PROACTIVE_INTERVAL=$((configured * 60))
fi

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
    for status_file in "${BZ_DIR}/agents"/*/STATUS.md; do
        [[ -f "$status_file" ]] || continue
        local state
        state="$(grep '^State:' "$status_file" 2>/dev/null | head -1 | sed 's/State: //')"
        [[ "$state" == "done" ]] || return 1
    done
    return 0
}

wake_brain() {
    local reason="$1"
    local mode="$2"   # reactive or proactive
    local project_name
    project_name="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    print(yaml.safe_load(f).get('project',{}).get('name',''))
" 2>/dev/null || true)"

    local sess="bz-${project_name}-supervisor"
    if tmux has-session -t "$sess" 2>/dev/null; then
        if [[ "$mode" == "reactive" ]]; then
            tmux send-keys -t "$sess" "NERVE SIGNAL: Zombie state change detected — ${reason}. Read all .bz/agents/*/STATUS.md and coordinate the next move." Enter
        else
            tmux send-keys -t "$sess" "HEARTBEAT CHECK: ${PROACTIVE_INTERVAL}s since last check. Verify all zombies are making progress. Read .bz/agents/*/STATUS.md. If any zombie has not updated in 10+ minutes, investigate and redirect." Enter
        fi
        echo "[brain] $(date '+%H:%M:%S') Woke brain (${mode}): ${reason}"
    fi
}

sync_status_from_worktrees() {
    for wt_status in "${BZ_DIR}/worktrees"/*/".bz/agents"/*/STATUS.md; do
        [[ -f "$wt_status" ]] || continue
        local agent_id
        agent_id="$(basename "$(dirname "$wt_status")")"
        local main_status="${BZ_DIR}/agents/${agent_id}/STATUS.md"
        if [[ -f "$main_status" ]]; then
            if [[ "$wt_status" -nt "$main_status" ]]; then
                cp "$wt_status" "$main_status"
            fi
        fi
    done
}

echo "[nerve] Started — scan every ${SCAN_INTERVAL}s, proactive check every $((PROACTIVE_INTERVAL / 60))m"
echo "[nerve] Watching: ${BZ_DIR}/agents/*/STATUS.md"

# Initialize proactive timer
date +%s > "$LAST_PROACTIVE_FILE" 2>/dev/null || true

while true; do
    sync_status_from_worktrees 2>/dev/null || true

    # Skip if all zombies are done
    if all_done 2>/dev/null; then
        sleep "$SCAN_INTERVAL"
        continue
    fi

    # ── REACTIVE: detect state changes ──
    current="$(capture_signatures)"
    previous=""
    [[ -f "$SIGNATURES_FILE" ]] && previous="$(cat "$SIGNATURES_FILE")"
    echo "$current" > "$SIGNATURES_FILE"

    if [[ -n "$previous" && "$current" != "$previous" ]]; then
        changed=""
        while IFS= read -r line; do
            agent="${line%%=*}"
            if ! grep -qF "$line" <<< "$previous" 2>/dev/null; then
                changed="${changed} ${agent}"
            fi
        done <<< "$current"
        echo "[nerve] $(date '+%H:%M:%S') State change:${changed}"
        wake_brain "${changed}" "reactive" 2>/dev/null || true
        # Reset proactive timer after reactive wake
        date +%s > "$LAST_PROACTIVE_FILE"
    fi

    # ── PROACTIVE: periodic forced check ──
    last_proactive=0
    [[ -f "$LAST_PROACTIVE_FILE" ]] && last_proactive="$(cat "$LAST_PROACTIVE_FILE")"
    now="$(date +%s)"
    elapsed=$((now - last_proactive))

    if [[ "$elapsed" -ge "$PROACTIVE_INTERVAL" ]]; then
        echo "[nerve] $(date '+%H:%M:%S') Proactive check (${elapsed}s since last brain activity)"
        wake_brain "No state changes in $((elapsed / 60))m — checking for stuck zombies" "proactive" 2>/dev/null || true
        date +%s > "$LAST_PROACTIVE_FILE"
    fi

    sleep "$SCAN_INTERVAL"
done
