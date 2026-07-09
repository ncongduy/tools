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
check "0s"    "$(human_delta 0)"    "0m"
check "3600s" "$(human_delta 3600)" "1h 0m"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[[ "$FAIL" -eq 0 ]]
