#!/usr/bin/env bash
# Test harness for claude-schedule.sh — no dependencies.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# shellcheck disable=SC1090
CLAUDE_SCHEDULE_LIB=1 source "$ROOT/claude-schedule.sh"
set +euo pipefail   # the script sets -e; tests must survive expected failures

PASS=0
FAIL=0

check() {
  local desc="$1" got="$2" want="$3"
  if [[ "$got" == "$want" ]]; then
    printf '  ✅ %s\n' "$desc"
    PASS=$(( PASS + 1 ))
  else
    printf '  ❌ %s\n     want: %s\n     got : %s\n' "$desc" "$want" "$got"
    FAIL=$(( FAIL + 1 ))
  fi
}

# Runs a command, echoes "ok" on exit 0 and "err" otherwise.
status() {
  if "$@" >/dev/null 2>&1; then echo ok; else echo err; fi
}

echo "validate_hhmm"
check "accepts 14:30"         "$(status validate_hhmm 14:30)" ok
check "accepts 00:00"         "$(status validate_hhmm 00:00)" ok
check "accepts 23:59"         "$(status validate_hhmm 23:59)" ok
check "rejects 24:00"         "$(status validate_hhmm 24:00)" err
check "rejects 12:60"         "$(status validate_hhmm 12:60)" err
check "rejects 9:5"           "$(status validate_hhmm 9:5)"   err
check "rejects abc"           "$(status validate_hhmm abc)"   err
check "rejects empty"         "$(status validate_hhmm '')"    err

echo "validate_mode"
check "accepts acceptEdits"       "$(status validate_mode acceptEdits)"       ok
check "accepts bypassPermissions" "$(status validate_mode bypassPermissions)" ok
check "accepts plan"              "$(status validate_mode plan)"              ok
check "rejects yolo"              "$(status validate_mode yolo)"              err
check "rejects empty"             "$(status validate_mode '')"                err
check "rejects planX"             "$(status validate_mode planX)"             err

echo "time math"
NOW=$(to_epoch 2026-07-09 10:00)
check "to_epoch is a 10-digit epoch" "$(printf '%s' "$NOW" | wc -c | tr -d ' ')" 10
check "fmt_day round-trips"          "$(fmt_day "$NOW")"     2026-07-09
check "fmt_stamp round-trips"        "$(fmt_stamp "$NOW")"   2026-07-09-1000
check "fmt_when round-trips"         "$(fmt_when "$NOW")"    "2026-07-09 10:00"

# 14:30 is still ahead of 10:00 → same day.
check "future time stays today" \
  "$(fmt_when "$(resolve_target 14:30 "$NOW")")" "2026-07-09 14:30"

# 09:00 already passed → tomorrow.
check "past time rolls to tomorrow" \
  "$(fmt_when "$(resolve_target 09:00 "$NOW")")" "2026-07-10 09:00"

# Exactly now counts as passed.
check "now rolls to tomorrow" \
  "$(fmt_when "$(resolve_target 10:00 "$NOW")")" "2026-07-10 10:00"

echo "human_delta"
check "8040s" "$(human_delta 8040)" "2h 14m"
check "600s"  "$(human_delta 600)"  "10m"
check "45s"   "$(human_delta 45)"   "45s"
check "0s"    "$(human_delta 0)"    "0s"
check "3600s" "$(human_delta 3600)" "1h 0m"

echo "logging"
LOG_DIR="$(mktemp -d)"   # override the real ~/.claude/schedule
TARGET=$(to_epoch 2026-07-09 14:30)

check "rand_id is 4 hex chars" "$(rand_id | grep -cE '^[0-9a-f]{4}$')" 1

P="$(log_path "$TARGET" md)"
JP="$(log_path "$TARGET" json)"
check "log_path dir"      "$(dirname "$P")"                                   "$LOG_DIR"
check "log_path shape"    "$(basename "$P" | sed -E 's/-[0-9a-f]{4}\.md$/-ID.md/')" "2026-07-09-1430-ID.md"
check "log_path json ext" "${JP##*.}"                                         json

# A canned successful claude result.
RAW="$(mktemp)"; ERRF="$(mktemp)"
cat >"$RAW" <<'JSON'
{"type":"result","subtype":"success","is_error":false,"duration_ms":4210,
 "num_turns":3,"result":"Reviewed auth module. Found 2 issues.",
 "session_id":"abc-123","total_cost_usd":0.0231}
JSON
: >"$ERRF"

MD="$(render_md "review the auth module" opus acceptEdits "" "$TARGET" 0 "$RAW" "$ERRF")"
check "md has when"     "$(printf '%s' "$MD" | grep -c '^- when    : 2026-07-09 14:30$')" 1
check "md has model"    "$(printf '%s' "$MD" | grep -c '^- model   : opus$')"             1
check "md has session"  "$(printf '%s' "$MD" | grep -c '^- session : abc-123$')"          1
check "md has cost"     "$(printf '%s' "$MD" | grep -c '^- cost    : \$0.0231$')"         1
check "md has duration" "$(printf '%s' "$MD" | grep -c '^- duration: 4.2s$')"             1
check "md has prompt"   "$(printf '%s' "$MD" | grep -c '^review the auth module$')"       1
check "md has output"   "$(printf '%s' "$MD" | grep -c 'Found 2 issues')"                 1
check "md no stderr section when clean" "$(printf '%s' "$MD" | grep -c '^## Stderr$')"    0

# Garbage stdout + stderr: must still render, falling back to raw text.
printf 'not json at all' >"$RAW"
printf 'boom\n' >"$ERRF"
MD2="$(render_md "p" opus plan "" "$TARGET" 1 "$RAW" "$ERRF")"
check "md survives bad json"  "$(printf '%s' "$MD2" | grep -c 'not json at all')"    1
check "md shows exit code"    "$(printf '%s' "$MD2" | grep -c '^- exit    : 1$')"    1
check "md shows stderr"       "$(printf '%s' "$MD2" | grep -c '^## Stderr$')"        1
check "md session falls back" "$(printf '%s' "$MD2" | grep -c '^- session : (new)$')" 1

EJ="$(render_error_json 127 "$ERRF")"
check "error json is valid" \
  "$(printf '%s' "$EJ" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["exit_code"], d["is_error"])')" \
  "127 True"

echo "worker"
notify() { :; }   # override: don't fire real desktop notifications during tests

FAKE_BIN="$(mktemp -d)"
cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
# Fake claude: echoes its own argv into the json result so we can assert on it.
if [[ "${FAKE_CLAUDE_FAIL:-0}" == "1" ]]; then
  echo "fake failure" >&2
  exit 3
fi
printf '{"type":"result","is_error":false,"duration_ms":1000,"num_turns":1,'
printf '"result":"ARGV=%s","session_id":"sess-9","total_cost_usd":0.01}\n' "$*"
SH
chmod +x "$FAKE_BIN/claude"
PATH="$FAKE_BIN:$PATH"

WORKDIR="$(mktemp -d)"
PAST=$(( $(date +%s) - 5 ))   # already elapsed → wait_until returns instantly

LOG="$LOG_DIR/worker-md.md"
run_job "do a thing" opus acceptEdits "" md "$PAST" "$LOG" "$WORKDIR"
check "md log written"    "$( [[ -s "$LOG" ]] && echo yes || echo no )" yes
check "md log has output" "$(grep -c 'ARGV=' "$LOG")"                    1
check "passes -p"         "$(grep -c -- '-p do a thing' "$LOG")"         1
check "passes --model"    "$(grep -c -- '--model opus' "$LOG")"          1
check "passes --permission-mode"    "$(grep -c -- '--permission-mode acceptEdits' "$LOG")" 1
check "passes --output-format json" "$(grep -c -- '--output-format json' "$LOG")"          1
check "no --resume when session empty" "$(grep -c -- '--resume' "$LOG")" 0
check "run_job restores cwd" "$PWD" "$ROOT"

LOG2="$LOG_DIR/worker-resume.md"
run_job "again" sonnet plan "sess-abc" md "$PAST" "$LOG2" "$WORKDIR"
check "passes --resume when session given" "$(grep -c -- '--resume sess-abc' "$LOG2")" 1

LOG3="$LOG_DIR/worker.json"
run_job "raw" opus acceptEdits "" json "$PAST" "$LOG3" "$WORKDIR"
check "json log is the raw result" \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["session_id"])' "$LOG3")" \
  sess-9

export FAKE_CLAUDE_FAIL=1

LOG4="$LOG_DIR/worker-fail.json"
run_job "boom" opus acceptEdits "" json "$PAST" "$LOG4" "$WORKDIR"
RC=$?
check "run_job propagates exit code" "$RC" 3
check "failed json log records exit_code" \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["exit_code"])' "$LOG4")" 3

LOG5="$LOG_DIR/worker-fail.md"
run_job "boom" opus acceptEdits "" md "$PAST" "$LOG5" "$WORKDIR"
check "failed md log has stderr" "$(grep -c 'fake failure' "$LOG5")" 1

unset FAKE_CLAUDE_FAIL

# claude can exit non-zero *and* still print a valid JSON diagnostic on stdout
# (e.g. an unknown --model). That diagnostic is the most useful thing we have,
# so the json log must keep it rather than synthesise an empty error object.
cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
echo '{"type":"result","is_error":true,"result":"unknown model","session_id":"sess-diag"}'
exit 1
SH
chmod +x "$FAKE_BIN/claude"

LOG6="$LOG_DIR/worker-diag.json"
run_job "boom" nope acceptEdits "" json "$PAST" "$LOG6" "$WORKDIR"
check "nonzero exit still propagates" "$?" 1
check "json log keeps claude's own diagnostic" \
  "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["result"])' "$LOG6")" \
  "unknown model"

# Unparseable stdout → fall back to the synthetic error object.
cat >"$FAKE_BIN/claude" <<'SH'
#!/usr/bin/env bash
echo 'segfault lol'
echo 'crashed' >&2
exit 9
SH
chmod +x "$FAKE_BIN/claude"

LOG7="$LOG_DIR/worker-garbage.json"
run_job "boom" opus acceptEdits "" json "$PAST" "$LOG7" "$WORKDIR"
check "garbage stdout falls back to synthetic error json" \
  "$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["exit_code"], d["error"])' "$LOG7")" \
  "9 crashed"

echo "ask"
check "ask returns typed value"           "$(echo 'sonnet' | ask 'Model' opus 2>/dev/null)" sonnet
check "ask returns default on empty"      "$(echo '' | ask 'Model' opus 2>/dev/null)"       opus
check "ask with no default returns empty" "$(echo '' | ask 'Prompt' 2>/dev/null)"           ""
check "ask prompt goes to stderr" \
  "$(echo 'x' | ask 'Model' opus 2>&1 >/dev/null | grep -c 'Model')" 1

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
