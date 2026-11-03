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

## `claude-schedule.sh`

Run a Claude Code prompt at a chosen time, unattended, and log the result.
Asks for the time, prompt, model, permission mode, session id, and log
format; then detaches with `nohup` and returns your shell.

```
./claude-schedule.sh

  Time (HH:MM)            : 14:30
  Prompt                  : review the auth module for timing leaks
  Model                    [opus]:
  Permission mode          [acceptEdits]:
  Session id (blank = new):
  Log format (md/json)     [md]:

  ✅  Scheduled for 2026-07-09 14:30 (in 2h 14m)
      job pid : 48213
      log     : /Users/ncd/.claude/schedule/2026-07-09-1430-a3f1.md
```

The job runs `claude -p` in the directory you scheduled it from. Logs land in
`~/.claude/schedule/`. `md` renders a report with cost, duration, and the
resulting session id; `json` keeps the raw `--output-format json` result.
Give a session id to `--resume` an existing conversation, or leave it blank
for a new one.

Permission mode is one of `acceptEdits`, `auto`, `bypassPermissions`,
`manual`, `dontAsk`, `plan`. The job survives closing the terminal but **not
a reboot** — it is a background process, not a cron entry. Cancel it with
`kill <pid>`.

Tests: `bash tests/test_claude_schedule.sh` (uses a fake `claude`, spends
nothing).

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

## `redact-log.py`

Mask secrets and personal data in a log before sharing it. Reads any
line-based text log and writes a redacted copy; the original is never
modified. Python 3 standard library only.

```
./redact-log.py app.log                     # → app.redacted.log next to the input
./redact-log.py app.log -o clean.log        # choose the output file
./redact-log.py app.log --force             # overwrite an existing output file
./redact-log.py app.log --skip ipv4,path    # leave some rules out
./redact-log.py app.log --only email,phone  # run just these rules
./redact-log.py app.log -q                  # no summary
./redact-log.py --list                      # every rule with a before → after example
some-cmd | ./redact-log.py /dev/stdin -o out.log   # redact a pipe (needs -o)
```

A summary of what was masked goes to stderr:

```
✅  17 values redacted → app.redacted.log
    private_key  1
    secret       3
    email        1
    ipv4         3
    ...
```

Each value becomes a typed placeholder, so you can still tell what was
there. Rules run in this order:

| Rule          | Catches                                                                 | Becomes                                   |
|---------------|-------------------------------------------------------------------------|-------------------------------------------|
| `private_key` | `-----BEGIN … PRIVATE KEY-----` blocks, multi-line or `\n`-escaped      | `[REDACTED:PRIVATE_KEY]`                  |
| `url_creds`   | `scheme://user:pass@host`                                               | `scheme://[REDACTED:CREDENTIALS]@host`    |
| `jwt`         | `eyJ….eyJ….…` tokens                                                    | `[REDACTED:JWT]`                          |
| `bearer`      | `Bearer <token>`, `Basic <base64 user:pass>`                            | `Bearer [REDACTED:TOKEN]`                 |
| `api_key`     | Anthropic/OpenAI `sk-…`, AWS `AKIA…`, GitHub `ghp_…`/`github_pat_…`, GitLab `glpat-…`, Slack `xox?-…`, Google `AIza…`, Stripe `sk_live_…` | `[REDACTED:API_KEY]` |
| `secret`      | value after a key ending in `password`, `secret`, `token`, `api_key`, `access_key`, `private_key`, `credentials`, `auth`, `authorization` — `k=v`, `k: v`, `"k": "v"` | `password=[REDACTED:SECRET]` |
| `email`       | email addresses                                                         | `[REDACTED:EMAIL]`                        |
| `credit_card` | 13–19 digit card numbers, spaces/dashes allowed, card prefix + Luhn checked | `[REDACTED:CREDIT_CARD]`              |
| `phone`       | `+CC …` international numbers, `(555) 123-4567` / `040 123 4567` style  | `[REDACTED:PHONE]`                        |
| `mac`         | `aa:bb:cc:dd:ee:ff`, `AA-BB-…`                                          | `[REDACTED:MAC]`                          |
| `ipv4`        | IPv4 addresses (port kept: `[REDACTED:IPV4]:8080`)                      | `[REDACTED:IPV4]`                         |
| `ipv6`        | IPv6 addresses                                                          | `[REDACTED:IPV6]`                         |
| `path`        | the user name in `/home/<user>`, `/Users/<user>`, `C:\Users\<user>`     | `/home/[REDACTED:USER]/…`                 |

What it keeps:

- Context around a secret: key names, the `Bearer` scheme, URL scheme and
  host, the rest of a path.
- Lookalikes: timestamps, UUIDs, version numbers, epoch-ms values, order
  ids, and counters like `max_tokens=4096` or `input_tokens=1234`.
- Line endings (CRLF), non-UTF-8 bytes, and the input file's permissions.

Safety: the output is written to a temp file and renamed into place, so a
failed run never leaves half a file. It refuses to write over the input
(even with `--force`) and over an existing output unless `--force` is
given. A private key block with no `END` line is hidden to the end of the
file. Exit codes: `0` ok, `1` error, `2` bad usage.

Limits: it matches patterns, so it is best effort. It does not catch
people's names, street addresses, or secrets in formats it doesn't know.
It does mask harmless IPs like `127.0.0.1` and four-part versions like
`1.2.3.4`; use `--skip ipv4` if that gets in the way. Check the result
before sharing: `diff app.log app.redacted.log`.

To add a rule, append a `Rule(...)` to `RULES` in `redact-log.py`;
order matters, so put specific patterns before generic ones.

Tests: `python3 -m unittest tests/test_redact_log.py` (stdlib only).

## workflow

- check todo high priority => timer => action
- check habit => timer => action
- check todo medium, low priority => timer => action
