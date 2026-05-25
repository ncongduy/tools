#!/usr/bin/env python3
"""habit.py — terminal daily habit tracker with append-only event log."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

HABIT_DIR = Path.home() / ".habit"
HABITS_CSV = HABIT_DIR / "habits.csv"
EVENTS_LOG = HABIT_DIR / "events.log"

FIELDS = ["id", "name", "type", "target", "unit", "created_at", "archived_at"]
EVENT_FIELDS = ["timestamp", "event", "habit_id", "name", "value", "note"]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


# ── storage ───────────────────────────────────────────────────────────────────

def ensure_storage() -> None:
    HABIT_DIR.mkdir(parents=True, exist_ok=True)
    if not HABITS_CSV.exists():
        with HABITS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(FIELDS)
    if not EVENTS_LOG.exists():
        with EVENTS_LOG.open("w", newline="") as f:
            csv.writer(f).writerow(EVENT_FIELDS)


def read_habits() -> list[dict]:
    with HABITS_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def write_habits(rows: list[dict]) -> None:
    tmp = HABITS_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, HABITS_CSV)


def read_events() -> list[dict]:
    with EVENTS_LOG.open(newline="") as f:
        return list(csv.DictReader(f))


def write_events(rows: list[dict]) -> None:
    tmp = EVENTS_LOG.parent / (EVENTS_LOG.name + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in EVENT_FIELDS})
    os.replace(tmp, EVENTS_LOG)


def log_event(event: str, habit: dict, value: str = "", note: str = "") -> None:
    row = {
        "timestamp": now_iso(),
        "event": event,
        "habit_id": habit.get("id", ""),
        "name": habit.get("name", ""),
        "value": value,
        "note": note,
    }
    with EVENTS_LOG.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=EVENT_FIELDS).writerow(row)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_id(existing: set[str]) -> str:
    while True:
        candidate = datetime.now().strftime("%Y%m%d%H%M%S")
        if candidate not in existing:
            return candidate
        time.sleep(1)


# ── helpers ───────────────────────────────────────────────────────────────────

def die(msg: str) -> None:
    print(f"habit: {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_name(arg: str, rows: list[dict], active_only: bool = True) -> dict:
    pool = [r for r in rows if not r.get("archived_at")] if active_only else rows
    exact = [r for r in pool if r["name"] == arg]
    if exact:
        return exact[0]
    matches = [r for r in pool if r["name"].startswith(arg)]
    if not matches:
        die(f"no habit matching '{arg}'")
    if len(matches) > 1:
        names = ", ".join(r["name"] for r in matches)
        die(f"name prefix '{arg}' is ambiguous; matches: {names}")
    return matches[0]


def sanitize_text(text: str, field: str = "text") -> str:
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if not text:
        die(f"{field} is empty")
    if text[0] in ("=", "+", "-", "@"):
        text = "'" + text
    return text


def fmt_target(v: float) -> str:
    return str(int(v)) if v == int(v) else str(v)


# ── formatting ────────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "yellow": "\033[33m",
    "green": "\033[32m",
    "cyan": "\033[36m",
}


def c(text: str, *codes: str) -> str:
    if not USE_COLOR:
        return text
    return "".join(COLORS[k] for k in codes) + text + COLORS["reset"]


def pad(text: str, width: int) -> str:
    return text.ljust(width)


# ── checkin logic ─────────────────────────────────────────────────────────────

def get_effective_checkin(events: list[dict], habit_id: str, day: date) -> dict | None:
    day_str = day.isoformat()
    relevant = [
        e for e in events
        if e["event"] in ("checkin", "uncheckin")
        and e["habit_id"] == habit_id
        and e["timestamp"][:10] == day_str
    ]
    last: dict | None = None
    for e in relevant:
        last = e if e["event"] == "checkin" else None
    return last


def is_hit(habit: dict, checkin: dict | None) -> bool:
    if checkin is None:
        return False
    if habit["type"] == "bool":
        return True
    try:
        val = float(checkin.get("value") or 0)
    except ValueError:
        return False
    if habit.get("target"):
        try:
            return val >= float(habit["target"])
        except ValueError:
            return False
    return val > 0


def compute_streak(habit: dict, events: list[dict], today: date) -> tuple[int, int]:
    day_last: dict[date, dict | None] = {}
    for e in events:
        if e["habit_id"] != habit["id"] or e["event"] not in ("checkin", "uncheckin"):
            continue
        try:
            d = date.fromisoformat(e["timestamp"][:10])
        except ValueError:
            continue
        day_last[d] = e if e["event"] == "checkin" else None

    hit_dates: set[date] = {d for d, ev in day_last.items() if is_hit(habit, ev)}

    streak_end = today if today in hit_dates else today - timedelta(days=1)
    current = 0
    d = streak_end
    while d in hit_dates:
        current += 1
        d -= timedelta(days=1)

    if not hit_dates:
        return 0, 0
    sorted_dates = sorted(hit_dates)
    longest = run = 1
    for i in range(1, len(sorted_dates)):
        if (sorted_dates[i] - sorted_dates[i - 1]).days == 1:
            run += 1
            if run > longest:
                longest = run
        else:
            run = 1
    return current, longest


def sparkline(habit: dict, events: list[dict], today: date, days: int = 7) -> str:
    chars = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        checkin = get_effective_checkin(events, habit["id"], d)
        if checkin is None:
            chars.append("·")
        elif is_hit(habit, checkin):
            chars.append(c("■", "green"))
        else:
            chars.append("▫")
    return "".join(chars)


def net_checkins(habit: dict, events: list[dict]) -> int:
    day_strs = {
        e["timestamp"][:10] for e in events
        if e["habit_id"] == habit["id"] and e["event"] in ("checkin", "uncheckin")
    }
    return sum(
        1 for ds in day_strs
        if get_effective_checkin(events, habit["id"], date.fromisoformat(ds)) is not None
    )


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> None:
    name = args.name
    if not NAME_RE.match(name):
        die(f"name must match ^[a-z0-9_-]{{1,32}}$, got '{name}'")

    rows = read_habits()
    if any(r["name"] == name and not r.get("archived_at") for r in rows):
        die(f"habit '{name}' already exists")

    htype = args.type
    target = unit = ""

    if htype == "bool":
        if args.target is not None:
            die("--target is only valid for --type num")
        if args.unit:
            die("--unit is only valid for --type num")
    else:
        if args.target is not None:
            if args.target < 0:
                die("--target must be non-negative")
            target = fmt_target(args.target)
        if args.unit:
            unit = sanitize_text(args.unit, "unit")

    hid = new_id({r["id"] for r in rows})
    habit = {
        "id": hid,
        "name": name,
        "type": htype,
        "target": target,
        "unit": unit,
        "created_at": now_iso(),
        "archived_at": "",
    }
    rows.append(habit)
    write_habits(rows)
    log_event("created", habit)

    desc = htype
    if target:
        desc += f", target={target}"
        if unit:
            desc += f" {unit}"
    print(f"added {c(name, 'cyan')}  ({desc})")


def cmd_list(args: argparse.Namespace) -> None:
    rows = read_habits()
    if not args.all:
        rows = [r for r in rows if not r.get("archived_at")]
    if not rows:
        print(c("(no habits)", "dim"))
        return

    print(
        f"  {c(pad('NAME', 20), 'bold')}  "
        f"{c(pad('TYPE', 5), 'bold')}  "
        f"{c(pad('TARGET', 8), 'bold')}  "
        f"{c(pad('UNIT', 10), 'bold')}  "
        f"{c('STATUS', 'bold')}"
    )
    for r in rows:
        status = c("archived", "dim") if r.get("archived_at") else "active"
        print(
            f"  {pad(r['name'], 20)}  "
            f"{pad(r['type'], 5)}  "
            f"{pad(r.get('target') or '-', 8)}  "
            f"{pad(r.get('unit') or '-', 10)}  "
            f"{status}"
        )


def cmd_checkin(args: argparse.Namespace) -> None:
    rows = read_habits()
    habit = resolve_name(args.name, rows)

    if habit["type"] == "bool" and args.value is not None:
        die("value is not accepted for bool habits")
    if habit["type"] == "num":
        if args.value is None:
            die("value is required for num habits")
        try:
            val_f = float(args.value)
        except ValueError:
            die(f"value must be a number, got '{args.value}'")
        if val_f < 0:
            die("value must be non-negative")
        value_str = args.value
    else:
        value_str = "1"

    if args.date:
        if not DATE_RE.match(args.date):
            die(f"--date must be YYYY-MM-DD, got '{args.date}'")
        checkin_date = date.fromisoformat(args.date)
        if checkin_date > date.today():
            die("--date cannot be in the future")
    else:
        checkin_date = date.today()

    note = sanitize_text(args.note, "note") if args.note else ""

    events = read_events()
    existing = get_effective_checkin(events, habit["id"], checkin_date)
    log_event("checkin", habit, value=value_str, note=note)

    verb = "updated" if existing else "added"
    if habit["type"] == "num":
        if habit.get("target"):
            suffix = f"  {value_str}/{habit['target']}"
        else:
            suffix = f"  {value_str}"
        if habit.get("unit"):
            suffix += f" {habit['unit']}"
        hit = is_hit(habit, {"value": value_str})
        name_part = c(habit["name"], "green") if hit else habit["name"]
        print(f"{verb}: {name_part}{suffix}")
    else:
        print(f"{verb}: {c(habit['name'], 'green')}")


def cmd_uncheckin(args: argparse.Namespace) -> None:
    rows = read_habits()
    habit = resolve_name(args.name, rows)

    if args.date:
        if not DATE_RE.match(args.date):
            die(f"--date must be YYYY-MM-DD, got '{args.date}'")
        checkin_date = date.fromisoformat(args.date)
        if checkin_date > date.today():
            die("--date cannot be in the future")
    else:
        checkin_date = date.today()

    events = read_events()
    if get_effective_checkin(events, habit["id"], checkin_date) is None:
        print(c(f"(nothing to remove for '{habit['name']}' on {checkin_date})", "dim"))
        return

    log_event("uncheckin", habit)
    print(f"removed check-in: {habit['name']}  {checkin_date}")


def cmd_today(args: argparse.Namespace) -> None:
    rows = read_habits()
    active = [r for r in rows if not r.get("archived_at")]
    if not active:
        print(c("(no habits)", "dim"))
        return

    events = read_events()
    today = date.today()

    for habit in active:
        checkin = get_effective_checkin(events, habit["id"], today)
        done = is_hit(habit, checkin)
        box = c("[x]", "green") if done else "[ ]"

        if habit["type"] == "num":
            val = float(checkin["value"]) if checkin and checkin.get("value") else 0.0
            val_str = str(int(val)) if val == int(val) else str(val)
            suffix = f"  {val_str}/{habit['target']}" if habit.get("target") else f"  {val_str}"
            if habit.get("unit"):
                suffix += f" {habit['unit']}"
            name_part = c(habit["name"], "green") if done else habit["name"]
            print(f"{box} {name_part}{suffix}")
        else:
            name_part = c(habit["name"], "green") if done else habit["name"]
            print(f"{box} {name_part}")


def cmd_status(args: argparse.Namespace) -> None:
    rows = read_habits()
    active = [r for r in rows if not r.get("archived_at")]
    events = read_events()
    today = date.today()

    if args.name:
        habit = resolve_name(args.name, rows)
        current, longest = compute_streak(habit, events, today)
        spark = sparkline(habit, events, today)
        total = net_checkins(habit, events)
        type_info = habit["type"] + (f", target={habit['target']}" if habit.get("target") else "")
        print(f"{c(habit['name'], 'bold')}  ({type_info})")
        print(f"  current streak:  {current}")
        print(f"  longest streak:  {longest}")
        print(f"  last 7 days:     {spark}")
        print(f"  total check-ins: {total}")
    else:
        if not active:
            print(c("(no habits)", "dim"))
            return
        print(
            f"  {c(pad('HABIT', 20), 'bold')}  "
            f"{c(pad('STREAK', 6), 'bold')}  "
            f"{c(pad('BEST', 6), 'bold')}  "
            f"{c(pad('LAST 7', 7), 'bold')}  "
            f"{c('TOTAL', 'bold')}"
        )
        for habit in active:
            current, longest = compute_streak(habit, events, today)
            spark = sparkline(habit, events, today)
            total = net_checkins(habit, events)
            print(
                f"  {pad(habit['name'], 20)}  "
                f"{pad(str(current), 6)}  "
                f"{pad(str(longest), 6)}  "
                f"{spark}  "
                f"{total}"
            )


def parse_window(args: argparse.Namespace) -> tuple[datetime | None, datetime]:
    now = datetime.now()
    end = now
    start: datetime | None = None

    period = getattr(args, "period", "week")
    if period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    elif period == "year":
        start = now - timedelta(days=365)

    from_ = getattr(args, "from_", None)
    to = getattr(args, "to", None)
    if from_:
        if not DATE_RE.match(from_):
            die(f"--from must be YYYY-MM-DD, got '{from_}'")
        start = datetime.fromisoformat(from_)
    if to:
        if not DATE_RE.match(to):
            die(f"--to must be YYYY-MM-DD, got '{to}'")
        end = datetime.fromisoformat(to) + timedelta(days=1) - timedelta(seconds=1)

    return start, end


def cmd_report(args: argparse.Namespace) -> None:
    start, end = parse_window(args)

    rows = read_habits()
    active = [r for r in rows if not r.get("archived_at")]
    events = read_events()

    if not active:
        print(c("(no habits)", "dim"))
        return

    today = date.today()
    end_date = end.date()
    if start is None:
        min_created = min(
            (r["created_at"][:10] for r in active if r.get("created_at")),
            default=today.isoformat(),
        )
        start_date = date.fromisoformat(min_created)
    else:
        start_date = start.date()

    window_days = (end_date - start_date).days + 1
    effective_days = max(1, min(window_days, (today - start_date).days + 1))
    period_label = "all time" if start is None else f"{start_date} .. {end_date} ({window_days} days)"

    print(f"Period: {c(period_label, 'bold')}")
    print()
    print(
        f"  {c(pad('HABIT', 20), 'bold')}  "
        f"{c(pad('HITS', 5), 'bold')}  "
        f"{c(pad('DAYS', 5), 'bold')}  "
        f"{c('COMPLETION', 'bold')}"
    )

    for habit in active:
        hit_count = sum(
            1 for i in range(effective_days)
            if is_hit(habit, get_effective_checkin(
                events, habit["id"], start_date + timedelta(days=i)
            ))
        )
        pct = hit_count / effective_days * 100
        if pct >= 80:
            pct_str = c(f"{pct:.0f}%", "green")
        elif pct < 50:
            pct_str = c(f"{pct:.0f}%", "dim")
        else:
            pct_str = f"{pct:.0f}%"
        print(
            f"  {pad(habit['name'], 20)}  "
            f"{pad(str(hit_count), 5)}  "
            f"{pad(str(effective_days), 5)}  "
            f"{pct_str}"
        )


def cmd_heatmap(args: argparse.Namespace) -> None:
    rows = read_habits()
    events = read_events()
    today = date.today()
    weeks = args.weeks

    start_date = today - timedelta(days=weeks * 7 - 1)
    start_date -= timedelta(days=start_date.weekday())

    all_days: list[date] = []
    d = start_date
    while d <= today:
        all_days.append(d)
        d += timedelta(days=1)

    if args.name:
        habits_to_show = [resolve_name(args.name, rows)]
    else:
        habits_to_show = [r for r in rows if not r.get("archived_at")]

    if not habits_to_show:
        print(c("(no habits)", "dim"))
        return

    weeks_list: list[list[date]] = []
    week: list[date] = []
    for d in all_days:
        if d.weekday() == 0 and week:
            weeks_list.append(week)
            week = []
        week.append(d)
    if week:
        weeks_list.append(week)

    name_width = max(len(h["name"]) for h in habits_to_show)

    for habit in habits_to_show:
        cells: list[str] = []
        for wk in weeks_list:
            for day in wk:
                if day > today:
                    cells.append(" ")
                else:
                    checkin = get_effective_checkin(events, habit["id"], day)
                    if checkin is None:
                        cells.append("·")
                    elif is_hit(habit, checkin):
                        cells.append(c("■", "green"))
                    elif habit["type"] == "num":
                        cells.append("▫")
                    else:
                        cells.append("·")
            cells.append(" ")
        print(f"{habit['name']:<{name_width}}  {''.join(cells)}")


def cmd_edit(args: argparse.Namespace) -> None:
    if args.rename is None and args.target is None and args.unit is None:
        die("nothing to edit; pass --rename, --target, or --unit")

    rows = read_habits()
    habit = resolve_name(args.name, rows)

    changes = []
    if args.rename is not None:
        new_name = args.rename
        if not NAME_RE.match(new_name):
            die(f"name must match ^[a-z0-9_-]{{1,32}}$, got '{new_name}'")
        active_names = {r["name"] for r in rows if not r.get("archived_at") and r["id"] != habit["id"]}
        if new_name in active_names:
            die(f"habit '{new_name}' already exists")
        habit["name"] = new_name
        changes.append(f"name={new_name!r}")

    if args.target is not None:
        if habit["type"] == "bool":
            die("--target is only valid for num habits")
        if args.target < 0:
            die("--target must be non-negative")
        habit["target"] = fmt_target(args.target)
        changes.append(f"target={habit['target']}")

    if args.unit is not None:
        if habit["type"] == "bool":
            die("--unit is only valid for num habits")
        habit["unit"] = sanitize_text(args.unit, "unit") if args.unit else ""
        changes.append(f"unit={habit['unit']!r}")

    for r in rows:
        if r["id"] == habit["id"]:
            r.update(habit)
            break

    write_habits(rows)
    log_event("edited", habit)
    print(f"edited {habit['name']}  " + "  ".join(changes))


def cmd_archive(args: argparse.Namespace) -> None:
    rows = read_habits()
    habit = resolve_name(args.name, rows, active_only=False)

    if habit.get("archived_at"):
        print(c(f"'{habit['name']}' is already archived", "dim"))
        return

    for r in rows:
        if r["id"] == habit["id"]:
            r["archived_at"] = now_iso()
            habit = dict(r)
            break

    write_habits(rows)
    log_event("archived", habit)
    print(f"archived {habit['name']}")


def cmd_unarchive(args: argparse.Namespace) -> None:
    rows = read_habits()
    habit = resolve_name(args.name, rows, active_only=False)

    if not habit.get("archived_at"):
        print(c(f"'{habit['name']}' is not archived", "dim"))
        return

    for r in rows:
        if r["id"] == habit["id"]:
            r["archived_at"] = ""
            habit = dict(r)
            break

    write_habits(rows)
    log_event("unarchived", habit)
    print(f"unarchived {habit['name']}")


def cmd_rm(args: argparse.Namespace) -> None:
    rows = read_habits()
    habit = resolve_name(args.name, rows, active_only=False)

    events = read_events()
    event_count = sum(1 for e in events if e["habit_id"] == habit["id"])

    if not args.yes:
        resp = input(f"delete '{habit['name']}' and {event_count} events? [y/N] ").strip().lower()
        if resp != "y":
            print("cancelled")
            return

    write_habits([r for r in rows if r["id"] != habit["id"]])
    write_events([e for e in events if e["habit_id"] != habit["id"]])
    log_event("removed", habit)
    print(f"removed {habit['name']}")


def cmd_log(args: argparse.Namespace) -> None:
    with EVENTS_LOG.open(newline="") as f:
        rows = list(csv.DictReader(f))
    if args.n < 0:
        die("-n must be non-negative")
    tail = rows[-args.n:] if args.n else rows
    for r in tail:
        print(
            f"{r['timestamp']}  {r['event']:<10}  {r['habit_id']}  "
            f"{r['name']:<20}  {r.get('value', ''):<8}  {r.get('note', '')}"
        )


# ── main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="habit",
        description="terminal daily habit tracker with append-only event log",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="create a habit")
    a.add_argument("name")
    a.add_argument("--type", choices=["bool", "num"], default="bool")
    a.add_argument("--target", type=float, help="target value (num only)")
    a.add_argument("--unit", help="unit label (num only)")
    a.set_defaults(func=cmd_add)

    ls = sub.add_parser("list", help="list habits")
    ls.add_argument("--all", action="store_true", help="include archived habits")
    ls.set_defaults(func=cmd_list)

    ci = sub.add_parser("checkin", help="record a check-in")
    ci.add_argument("name")
    ci.add_argument("value", nargs="?", help="value (required for num habits)")
    ci.add_argument("--date", help="date YYYY-MM-DD (default: today)")
    ci.add_argument("--note", help="optional annotation")
    ci.set_defaults(func=cmd_checkin)

    uc = sub.add_parser("uncheckin", help="remove a check-in")
    uc.add_argument("name")
    uc.add_argument("--date", help="date YYYY-MM-DD (default: today)")
    uc.set_defaults(func=cmd_uncheckin)

    td = sub.add_parser("today", help="today's progress")
    td.set_defaults(func=cmd_today)

    st = sub.add_parser("status", help="streaks and stats")
    st.add_argument("name", nargs="?", help="habit name (omit for summary table)")
    st.set_defaults(func=cmd_status)

    rep = sub.add_parser("report", help="completion rate per habit over a time window")
    rep.add_argument("period", nargs="?", default="week",
                     choices=["week", "month", "year", "all"])
    rep.add_argument("--from", dest="from_", help="window start YYYY-MM-DD")
    rep.add_argument("--to", help="window end YYYY-MM-DD")
    rep.set_defaults(func=cmd_report)

    hm = sub.add_parser("heatmap", help="ASCII calendar heatmap")
    hm.add_argument("name", nargs="?", help="habit name (omit for all active)")
    hm.add_argument("--weeks", type=int, default=12, help="weeks to show (default 12)")
    hm.set_defaults(func=cmd_heatmap)

    ed = sub.add_parser("edit", help="edit habit definition")
    ed.add_argument("name")
    ed.add_argument("--rename", help="new name")
    ed.add_argument("--target", type=float, help="new target value")
    ed.add_argument("--unit", help="new unit label (empty string to clear)")
    ed.set_defaults(func=cmd_edit)

    ar = sub.add_parser("archive", help="hide habit without deleting history")
    ar.add_argument("name")
    ar.set_defaults(func=cmd_archive)

    ua = sub.add_parser("unarchive", help="restore archived habit")
    ua.add_argument("name")
    ua.set_defaults(func=cmd_unarchive)

    rm = sub.add_parser("rm", help="hard-delete habit and all its events")
    rm.add_argument("name")
    rm.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    rm.set_defaults(func=cmd_rm)

    lg = sub.add_parser("log", help="tail the events log")
    lg.add_argument("-n", type=int, default=20, help="show last N events (0 = all)")
    lg.set_defaults(func=cmd_log)

    return p


def main() -> None:
    ensure_storage()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
