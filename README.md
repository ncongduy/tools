# tools

Small terminal utilities.

## `timer.sh`

Countdown or stopwatch timer.

```
./timer.sh 90        # countdown 90 seconds
./timer.sh 1:30      # countdown 1 min 30 sec
./timer.sh 01:30:00  # countdown 1 hour 30 min
./timer.sh           # stopwatch
```

## `todo.py`

Terminal to-do list with an append-only event log for weekly / monthly /
yearly performance reports. Stores data in `~/.todo/` (`tasks.csv` for
current state, `events.log` for history).

```
./todo.py add "buy milk"
./todo.py add "finish report" -p H -d 2026-05-22
./todo.py list
./todo.py start <id>           # mark in progress (open → active → done)
./todo.py done <id>            # id, or any unambiguous prefix
./todo.py undone <id>          # revert to open from any state
./todo.py edit <id> -t "new text"           # any combo of -t/-d/-p
./todo.py edit <id> -d 2026-07-15 -p H
./todo.py edit <id> -d ""                   # clear due date
./todo.py rm <id>
./todo.py clear-done
./todo.py log -n 20
./todo.py report week          # week | month | year | all
./todo.py report --from 2026-05-01 --to 2026-05-20
```

IDs are 14-digit creation timestamps (`YYYYMMDDHHMMSS`) — sortable and
self-describing. Both data files are CSV so they open cleanly in any
spreadsheet or pandas for ad-hoc analysis.

## `claude-usage.py`

Analyze Claude Code token usage, cost, and which prompts are expensive
vs. efficient. Reads session logs from `~/.claude/projects/`.

```
./claude-usage.py                          # today
./claude-usage.py week                     # last 7 days
./claude-usage.py month --top 20           # 30-day rollup, top-20 expensive
./claude-usage.py year
./claude-usage.py all
./claude-usage.py --from 2026-05-01 --to 2026-05-20
./claude-usage.py --utc                    # interpret dates in UTC
./claude-usage.py --project -home-ncd-tools  # one project only
./claude-usage.py --json | jq .totals
./claude-usage.py show <id>                # full text of a listed prompt
./claude-usage.py show <id> --raw          # body only, pipe-friendly
```

Each listed prompt gets a stable 8-char id (sha1 of session+timestamp).
`show` accepts any unique prefix and prints the full prompt with token /
cost metadata. Efficiency = total tokens for a turn that produced a
reply, ascending — the smallest spends that still got something useful
back.

Costs use a hardcoded per-million-tokens price table for the Opus,
Sonnet, and Haiku 4.x families; unknown models contribute tokens but
show `n/a` for cost.
