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
  if   (( h > 0 )); then printf '%dh %dm' "$h" "$m"
  elif (( m > 0 )); then printf '%dm' "$m"
  else                   printf '%ds' "$s"
  fi
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

# ── worker ─────────────────────────────────────────────────────────────────────

# Poll the wall clock rather than one long sleep: nanosleep does not advance
# across a macOS suspend, so `sleep 28800` fires hours late after an overnight
# lid-close. 30s slices bound the drift.
wait_until() {
  local target="$1" remain
  while (( $(date +%s) < target )); do
    remain=$(( target - $(date +%s) ))
    if (( remain > 30 )); then sleep 30; else sleep "$remain"; fi
  done
}

notify() {
  local title="$1" body="$2"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${body//\"/}\" with title \"${title//\"/}\"" \
      >/dev/null 2>&1 || true
  elif command -v notify-send >/dev/null 2>&1; then
    notify-send "$title" "$body" >/dev/null 2>&1 || true
  fi
}

# run_job <prompt> <model> <mode> <session> <fmt> <target> <logfile> <workdir>
run_job() {
  local prompt="$1" model="$2" mode="$3" session="$4" fmt="$5" \
        target="$6" logfile="$7" workdir="$8"
  local raw err rc=0 prev_pwd="$PWD"

  wait_until "$target"
  cd "$workdir" || return 1

  raw="$(mktemp)"; err="$(mktemp)"
  # Restore cwd as well as clean up: the tests call run_job in-process, so a
  # stray `cd` would leak into the caller's shell.
  trap 'rm -f "$raw" "$err"; cd "$prev_pwd" || true' RETURN

  local args=( -p "$prompt" --model "$model" --permission-mode "$mode"
               --output-format json )
  [[ -n "$session" ]] && args+=( --resume "$session" )

  claude "${args[@]}" </dev/null >"$raw" 2>"$err" || rc=$?

  mkdir -p "$(dirname "$logfile")"
  if [[ "$fmt" == "json" ]]; then
    if (( rc == 0 )) && [[ -s "$raw" ]]; then
      cat "$raw" >"$logfile"
    else
      render_error_json "$rc" "$err" >"$logfile"
    fi
  else
    render_md "$prompt" "$model" "$mode" "$session" "$target" "$rc" "$raw" "$err" >"$logfile"
  fi

  notify "claude-schedule" "Run finished (exit $rc) → $(basename "$logfile")"
  return "$rc"
}

# ── input ──────────────────────────────────────────────────────────────────────

# Prompt on stderr so `answer=$(ask ...)` captures only the answer.
ask() {
  local label="$1" default="${2:-}" reply
  if [[ -n "$default" ]]; then
    printf '  %-24s [%s]: ' "$label" "$default" >&2
  else
    printf '  %-24s: ' "$label" >&2
  fi
  read -r reply || true
  printf '%s\n' "${reply:-$default}"
}

# Sourced by tests: define functions, run nothing.
if [[ -n "${CLAUDE_SCHEDULE_LIB:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi

# ── worker entrypoint ──────────────────────────────────────────────────────────
# Re-exec'd by the scheduler under nohup. Not a public interface.

if [[ "${1:-}" == "--worker" ]]; then
  shift
  run_job "$@"
  exit $?
fi

# ── main ───────────────────────────────────────────────────────────────────────

if ! command -v claude >/dev/null 2>&1; then
  echo "❌  claude is not on PATH." >&2
  exit 1
fi

SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

echo "╔══════════════════════════════════════╗"
echo "║      ⏰  Schedule a Claude run        ║"
echo "╚══════════════════════════════════════╝"
echo

while true; do
  TIME_INPUT="$(ask 'Time (HH:MM)')"
  validate_hhmm "$TIME_INPUT" && break
  echo "     ⚠️  Expected HH:MM in 24-hour form, e.g. 14:30." >&2
done

while true; do
  PROMPT="$(ask 'Prompt')"
  [[ -n "$PROMPT" ]] && break
  echo "     ⚠️  Prompt cannot be empty." >&2
done

MODEL="$(ask 'Model' "$DEFAULT_MODEL")"

while true; do
  MODE="$(ask 'Permission mode' "$DEFAULT_MODE")"
  validate_mode "$MODE" && break
  echo "     ⚠️  One of: $VALID_MODES" >&2
done

SESSION="$(ask 'Session id (blank = new)')"

while true; do
  FMT="$(ask 'Log format (md/json)' "$DEFAULT_FMT" | tr '[:upper:]' '[:lower:]')"
  [[ "$FMT" == "md" || "$FMT" == "json" ]] && break
  echo "     ⚠️  Expected 'md' or 'json'." >&2
done

NOW="$(date +%s)"
TARGET="$(resolve_target "$TIME_INPUT" "$NOW")"
mkdir -p "$LOG_DIR" && chmod 700 "$LOG_DIR"
LOGFILE="$(log_path "$TARGET" "$FMT")"

if [[ "$MODE" == "bypassPermissions" ]]; then
  echo
  echo "  ⚠️  bypassPermissions runs unattended with no confirmation prompts."
fi

nohup "$SCRIPT_PATH" --worker \
  "$PROMPT" "$MODEL" "$MODE" "$SESSION" "$FMT" "$TARGET" "$LOGFILE" "$PWD" \
  >/dev/null 2>&1 &
JOB_PID=$!
disown "$JOB_PID" 2>/dev/null || true

echo
echo "  ✅  Scheduled for $(fmt_when "$TARGET") (in $(human_delta $(( TARGET - NOW ))))"
echo "      job pid : $JOB_PID"
echo "      workdir : $PWD"
echo "      log     : $LOGFILE"
echo
echo "      cancel with: kill $JOB_PID"
