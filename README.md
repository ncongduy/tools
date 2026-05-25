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

## `habit.py`

Terminal daily habit tracker with an append-only event log. Tracks
boolean (done/not-done) and quantitative habits with optional targets,
and reports streaks, heatmaps, and completion rates.

```
./habit.py add meditate
./habit.py add pushups --type num --target 20 --unit reps
./habit.py list                             # active habits
./habit.py list --all                       # include archived
./habit.py checkin meditate
./habit.py checkin pushups 25 --note "morning set"
./habit.py uncheckin meditate               # undo today's check-in
./habit.py today                            # [ ]/[x] progress view
./habit.py status                           # streak table across all habits
./habit.py status meditate                  # per-habit detail
./habit.py report week                      # week | month | year | all
./habit.py report --from 2026-05-01 --to 2026-05-25
./habit.py heatmap --weeks 12               # ASCII calendar, all habits
./habit.py heatmap pushups --weeks 4
./habit.py edit pushups --target 25         # update definition in place
./habit.py edit meditate --rename meditation
./habit.py archive meditation               # hide without deleting history
./habit.py unarchive meditation
./habit.py rm meditation --yes              # hard-delete definition + events
./habit.py log -n 20
```

Habits are identified by name (or any unambiguous prefix). Data lives in
`~/.habit/`: `habits.csv` for current definitions and `events.log` for the
append-only event history — both plain CSV, readable in any spreadsheet.

## workflow

- check todo high priority => timer => action
- check habit => timer => action
- check todo medium, low priority => timer => action
