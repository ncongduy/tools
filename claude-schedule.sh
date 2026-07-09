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

# ── time ───────────────────────────────────────────────────────────────────────
# BSD date (macOS) first, GNU date (Linux) as fallback.

to_epoch() {
  local day="$1" hhmm="$2"
  date -j -f "%Y-%m-%d %H:%M:%S" "$day $hhmm:00" +%s 2>/dev/null \
    || date -d "$day $hhmm:00" +%s 2>/dev/null
}

fmt_day()   { date -r "$1" +%Y-%m-%d         2>/dev/null || date -d "@$1" +%Y-%m-%d;        }
fmt_stamp() { date -r "$1" +%Y-%m-%d-%H%M    2>/dev/null || date -d "@$1" +%Y-%m-%d-%H%M;   }
fmt_when()  { date -r "$1" "+%Y-%m-%d %H:%M" 2>/dev/null || date -d "@$1" "+%Y-%m-%d %H:%M"; }

# Next occurrence of HH:MM. Re-resolves against tomorrow's calendar date rather
# than adding 86400, so the wall-clock time survives a DST boundary.
resolve_target() {
  local hhmm="$1" now="${2:-$(date +%s)}" target
  target=$(to_epoch "$(fmt_day "$now")" "$hhmm")
  if (( target <= now )); then
    target=$(to_epoch "$(fmt_day $(( now + 86400 )))" "$hhmm")
  fi
  printf '%s\n' "$target"
}

human_delta() {
  local s="$1" h m
  h=$(( s / 3600 ))
  m=$(( (s % 3600) / 60 ))
  if (( h > 0 )); then printf '%dh %dm' "$h" "$m"; else printf '%dm' "$m"; fi
}

# Sourced by tests: define functions, run nothing.
if [[ -n "${CLAUDE_SCHEDULE_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
