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
./todo.py done <id>            # id, or any unambiguous prefix
./todo.py undone <id>
./todo.py edit <id> "new text"
./todo.py rm <id>
./todo.py clear-done
./todo.py log -n 20
./todo.py report week          # week | month | year | all
./todo.py report --from 2026-05-01 --to 2026-05-20
```

IDs are 14-digit creation timestamps (`YYYYMMDDHHMMSS`) — sortable and
self-describing. Both data files are CSV so they open cleanly in any
spreadsheet or pandas for ad-hoc analysis.
