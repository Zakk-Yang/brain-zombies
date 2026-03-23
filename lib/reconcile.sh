#!/usr/bin/env bash
set -euo pipefail

# Reconcile loop: watches STATUS.md changes and wakes supervisor
# Usage: bash reconcile.sh <project-root>

PROJECT_ROOT="${1:?Usage: reconcile.sh <project-root>}"
BZ_DIR="${PROJECT_ROOT}/.bz"
SIGNATURES_FILE="${BZ_DIR}/logs/.signatures"
INTERVAL=30

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

wake_supervisor() {
    local reason="$1"
    local project_name
    project_name="$(python3 -c "
import yaml
with open('${PROJECT_ROOT}/bz.yaml') as f:
    print(yaml.safe_load(f).get('project',{}).get('name',''))
" 2>/dev/null || true)"

    local sess="bz-${project_name}-supervisor"
    if tmux has-session -t "$sess" 2>/dev/null; then
        # Send message to supervisor's CLI session
        tmux send-keys -t "$sess" "Agent state change: ${reason}. Check all STATUS.md files and coordinate." Enter
    fi
}

# Also sync worktree STATUS.md back to main
sync_status_from_worktrees() {
    for wt_status in "${BZ_DIR}/worktrees"/*/".bz/agents"/*/STATUS.md; do
        [[ -f "$wt_status" ]] || continue
        # Extract agent id from path
        local agent_id
        agent_id="$(basename "$(dirname "$wt_status")")"
        local main_status="${BZ_DIR}/agents/${agent_id}/STATUS.md"
        if [[ -f "$main_status" ]]; then
            # Only copy if worktree version is newer
            if [[ "$wt_status" -nt "$main_status" ]]; then
                cp "$wt_status" "$main_status"
            fi
        fi
    done
}

echo "[reconcile] Started watching ${BZ_DIR}/agents/*/STATUS.md (${INTERVAL}s interval)"

while true; do
    sync_status_from_worktrees 2>/dev/null || true

    current="$(capture_signatures)"
    previous=""
    [[ -f "$SIGNATURES_FILE" ]] && previous="$(cat "$SIGNATURES_FILE")"

    echo "$current" > "$SIGNATURES_FILE"

    if [[ -n "$previous" && "$current" != "$previous" ]]; then
        # Find what changed
        local changed=""
        while IFS= read -r line; do
            agent="${line%%=*}"
            if ! grep -qF "$line" <<< "$previous" 2>/dev/null; then
                changed="${changed} ${agent}"
            fi
        done <<< "$current"
        echo "[reconcile] $(date '+%H:%M:%S') State change detected:${changed}"
        wake_supervisor "${changed}" 2>/dev/null || true
    fi

    sleep "$INTERVAL"
done
