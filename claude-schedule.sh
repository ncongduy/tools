#!/usr/bin/env bash
# claude-schedule.sh — run `claude -p` at a chosen time, log the result.
#
# Usage:
#   ./claude-schedule.sh   # interactive: asks time, prompt, model,
#                          # permission mode, session id, log format
#
# Schedules a background job (nohup) and returns your shell immediately.
# Logs land in ~/.claude/schedule/. Job does not survive a reboot.

set -euo pipefail

LOG_DIR="$HOME/.claude/schedule"
VALID_MODES="acceptEdits auto bypassPermissions manual dontAsk plan"
DEFAULT_MODEL="opus"
DEFAULT_MODE="acceptEdits"
DEFAULT_FMT="md"

# ── validators ─────────────────────────────────────────────────────────────────

validate_hhmm() {
  [[ "${1:-}" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]
}

validate_mode() {
  local candidate="${1:-}" m
  [[ -n "$candidate" ]] || return 1
  for m in $VALID_MODES; do
    [[ "$m" == "$candidate" ]] && return 0
  done
  return 1
}

# Sourced by tests: define functions, run nothing.
if [[ -n "${CLAUDE_SCHEDULE_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
