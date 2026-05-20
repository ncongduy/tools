#!/usr/bin/env bash
# timer.sh — countdown or stopwatch timer
# Usage:
#   ./timer.sh 90        # countdown from 90 seconds
#   ./timer.sh 1:30      # countdown from 1 min 30 sec
#   ./timer.sh 01:30:00  # countdown from 1 hour 30 min
#   ./timer.sh           # stopwatch (counts up)

set -euo pipefail

# ── helpers ────────────────────────────────────────────────────────────────────

parse_duration() {
  local input="$1"
  local total=0

  if [[ "$input" =~ ^[0-9]+$ ]]; then
    total="$input"
  elif [[ "$input" =~ ^([0-9]+):([0-9]{2})$ ]]; then
    total=$(( 10#${BASH_REMATCH[1]} * 60 + 10#${BASH_REMATCH[2]} ))
  elif [[ "$input" =~ ^([0-9]+):([0-9]{2}):([0-9]{2})$ ]]; then
    total=$(( 10#${BASH_REMATCH[1]} * 3600 + 10#${BASH_REMATCH[2]} * 60 + 10#${BASH_REMATCH[3]} ))
  else
    echo "Error: unrecognised duration '$input'" >&2
    echo "       Use seconds (90), MM:SS (1:30), or HH:MM:SS (01:30:00)" >&2
    exit 1
  fi

  echo "$total"
}

format_time() {
  local secs="$1"
  local h=$(( secs / 3600 ))
  local m=$(( (secs % 3600) / 60 ))
  local s=$(( secs % 60 ))

  if (( h > 0 )); then
    printf "%02d:%02d:%02d" "$h" "$m" "$s"
  else
    printf "%02d:%02d" "$m" "$s"
  fi
}

bell() {
  if command -v canberra-gtk-play &>/dev/null; then
    canberra-gtk-play -i complete >/dev/null 2>&1 || true
  else
    printf '\a' 2>/dev/null || true  # fallback to terminal bell
  fi
}

notify() {
  local title="$1"
  local body="$2"
  # notify-send is available on Ubuntu via libnotify-bin
  if command -v notify-send &>/dev/null; then
    notify-send --urgency=normal --icon=alarm-clock "$title" "$body"
  else
    echo "  (install libnotify-bin for desktop notifications: sudo apt install libnotify-bin)" >&2
  fi
}

cleanup() {
  tput cnorm 2>/dev/null || true   # restore cursor
  echo
  exit 0
}

# ── main ───────────────────────────────────────────────────────────────────────

trap cleanup INT TERM

tput civis 2>/dev/null || true     # hide cursor

if [[ $# -eq 0 ]]; then
  # ── stopwatch mode ──────────────────────────────────────────────────────────
  echo "Stopwatch started — press Ctrl+C to stop."
  elapsed=0
  start=$(date +%s)
  while true; do
    elapsed=$(( $(date +%s) - start ))
    printf "\r  ⏱  %s  " "$(format_time "$elapsed")"
    sleep 0.25
  done

else
  # ── countdown mode ──────────────────────────────────────────────────────────
  total=$(parse_duration "$1")

  if (( total <= 0 )); then
    echo "Error: duration must be greater than zero." >&2
    exit 1
  fi

  label="${2:-Timer}"
  echo "$label — press Ctrl+C to cancel."

  remaining="$total"
  while (( remaining >= 0 )); do
    pct=$(( (total - remaining) * 100 / total ))
    bar_len=30
    filled=$(( pct * bar_len / 100 ))
    empty=$(( bar_len - filled ))
    bar="$(printf '%0.s█' $(seq 1 $filled))$(printf '%0.s░' $(seq 1 $empty))"

    printf "\r  ⏳  %s  [%s] %3d%%  " \
      "$(format_time "$remaining")" "$bar" "$pct"

    (( remaining == 0 )) && break
    sleep 1
    (( remaining-- ))
  done

  printf "\r  ✅  %s — done!%*s\n" "$label" 20 ""
  notify "⏰ $label" "Your timer has finished!"
  bell
fi
