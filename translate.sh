#!/usr/bin/env bash
# ─────────────────────────────────────────────
#  Interactive Translator using translate-shell
# ─────────────────────────────────────────────

# --- Check dependency ---
if ! command -v trans &>/dev/null; then
  echo "❌  translate-shell is not installed."
  echo "    Install it with:"
  echo "      sudo apt install translate-shell   # Debian/Ubuntu"
  echo "      brew install translate-shell       # macOS"
  exit 1
fi

# --- Default languages (can be overridden as args) ---
SOURCE_LANG="${1:-auto}"
TARGET_LANG="${2:-en}"

# --- Header ---
clear
echo "╔══════════════════════════════════════╗"
echo "║        🌍  Terminal Translator        ║"
echo "╠══════════════════════════════════════╣"
echo "║  Source : ${SOURCE_LANG}  →  Target : ${TARGET_LANG}"
echo "║  Type 'quit' or 'q' to exit          ║"
echo "║  Type 'lang' to change languages     ║"
echo "╚══════════════════════════════════════╝"
echo ""

# --- Main loop ---
while true; do
  # Prompt user for input
  echo -n "📝  Enter text: "
  read -r user_input

  # --- Exit condition ---
  if [[ "$user_input" == "quit" || "$user_input" == "q" ]]; then
    echo ""
    echo "👋  Goodbye!"
    break
  fi

  # --- Change language pair ---
  if [[ "$user_input" == "lang" ]]; then
    echo -n "    Source language (e.g. en, fi, auto): "
    read -r SOURCE_LANG
    echo -n "    Target language (e.g. fi, en, es):   "
    read -r TARGET_LANG
    echo "    ✅  Languages set: ${SOURCE_LANG} → ${TARGET_LANG}"
    echo ""
    continue
  fi

  # --- Skip empty input ---
  if [[ -z "$user_input" ]]; then
    echo "    ⚠️  Please enter some text."
    echo ""
    continue
  fi

  # --- Translate ---
  echo ""
  echo "🔤  Translation (${SOURCE_LANG} → ${TARGET_LANG}):"
  echo "────────────────────────────────────────"
  trans -b "${SOURCE_LANG}:${TARGET_LANG}" "$user_input"
  echo "────────────────────────────────────────"
  echo ""

done
