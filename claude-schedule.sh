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

# ── logging ────────────────────────────────────────────────────────────────────

# $RANDOM, not `tr </dev/urandom | head -c 4`: that pipeline takes SIGPIPE and
# trips `set -o pipefail` with exit 141.
rand_id() { printf '%04x' $(( RANDOM % 65536 )); }

log_path() {
  local target="$1" fmt="$2"
  printf '%s/%s-%s.%s' "$LOG_DIR" "$(fmt_stamp "$target")" "$(rand_id)" "$fmt"
}

# render_md <prompt> <model> <mode> <session> <target> <rc> <raw_json> <stderr>
render_md() {
  python3 - "$@" <<'PY'
import json, pathlib, sys
from datetime import datetime

prompt, model, mode, session, target, rc, rawf, errf = sys.argv[1:9]
raw = pathlib.Path(rawf).read_text(errors="replace").strip()
err = pathlib.Path(errf).read_text(errors="replace").strip()

try:
    data = json.loads(raw) if raw else {}
    if not isinstance(data, dict):
        data = {}
except json.JSONDecodeError:
    data = {}

when = datetime.fromtimestamp(int(target)).strftime("%Y-%m-%d %H:%M")
body = data.get("result") or raw or "(no output)"

lines = [
    "# claude-schedule run",
    "",
    f"- when    : {when}",
    f"- model   : {model}",
    f"- mode    : {mode}",
    f"- session : {data.get('session_id') or session or '(new)'}",
    f"- exit    : {rc}",
]
if data.get("total_cost_usd") is not None:
    lines.append(f"- cost    : ${float(data['total_cost_usd']):.4f}")
if data.get("duration_ms") is not None:
    lines.append(f"- duration: {int(data['duration_ms']) / 1000:.1f}s")
if data.get("num_turns") is not None:
    lines.append(f"- turns   : {data['num_turns']}")

lines += ["", "## Prompt", "", prompt, "", "## Output", "", body]
if err:
    lines += ["", "## Stderr", "", "```", err, "```"]

sys.stdout.write("\n".join(lines) + "\n")
PY
}

# render_error_json <rc> <stderr_file>
render_error_json() {
  python3 - "$@" <<'PY'
import json, pathlib, sys
rc = int(sys.argv[1])
err = pathlib.Path(sys.argv[2]).read_text(errors="replace").strip()
json.dump(
    {"type": "result", "is_error": True, "exit_code": rc, "error": err},
    sys.stdout,
    indent=2,
)
sys.stdout.write("\n")
PY
}

# Sourced by tests: define functions, run nothing.
if [[ -n "${CLAUDE_SCHEDULE_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
