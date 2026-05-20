#!/usr/bin/env python3
"""todo.py — terminal to-do list with lifecycle event log for self-analytics."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

TODO_DIR = Path.home() / ".todo"
TASKS_CSV = TODO_DIR / "tasks.csv"
EVENTS_LOG = TODO_DIR / "events.log"

FIELDS = ["id", "status", "priority", "due", "created_at", "completed_at", "text"]
EVENT_FIELDS = ["timestamp", "event", "id", "priority", "due", "text"]

PRIORITY_RANK = {"H": 0, "M": 1, "L": 2, "-": 3}
VALID_PRIORITIES = {"H", "M", "L", "-"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^\d{14}$")


# ── storage helpers ───────────────────────────────────────────────────────────

def ensure_storage() -> None:
    TODO_DIR.mkdir(parents=True, exist_ok=True)
    if not TASKS_CSV.exists():
        with TASKS_CSV.open("w", newline="") as f:
            csv.writer(f).writerow(FIELDS)
    if not EVENTS_LOG.exists():
        with EVENTS_LOG.open("w", newline="") as f:
            csv.writer(f).writerow(EVENT_FIELDS)


def read_tasks() -> list[dict]:
    with TASKS_CSV.open(newline="") as f:
        return list(csv.DictReader(f))


def write_tasks(rows: list[dict]) -> None:
    tmp = TASKS_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    os.replace(tmp, TASKS_CSV)


def log_event(event: str, task: dict) -> None:
    row = {
        "timestamp": now_iso(),
        "event": event,
        "id": task.get("id", ""),
        "priority": task.get("priority", ""),
        "due": task.get("due", ""),
        "text": task.get("text", ""),
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


def resolve_id(arg: str, rows: list[dict]) -> str:
    ids = [r["id"] for r in rows]
    if arg in ids:
        return arg
    matches = [i for i in ids if i.startswith(arg)]
    if not matches:
        die(f"no task with id matching '{arg}'")
    if len(matches) > 1:
        die(f"id prefix '{arg}' is ambiguous; matches: {', '.join(matches)}")
    return matches[0]


def die(msg: str) -> None:
    print(f"todo: {msg}", file=sys.stderr)
    sys.exit(1)


def sanitize_text(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if not text:
        die("task text is empty")
    return text


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


def priority_color(p: str) -> tuple[str, ...]:
    return {"H": ("red", "bold"), "M": ("yellow",), "L": ("dim",)}.get(p, ("dim",))


def pad(text: str, width: int) -> str:
    return text.ljust(width)


PRIORITY_LABEL = {"H": "High", "M": "Medium", "L": "Low", "-": "-"}
STATUS_LABEL = {"open": "undone", "done": "done"}


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_add(args: argparse.Namespace) -> None:
    text = sanitize_text(args.text)
    priority = args.priority or "-"
    due = args.due or ""
    if due and not DATE_RE.match(due):
        die(f"due date must be YYYY-MM-DD, got '{due}'")

    rows = read_tasks()
    tid = new_id({r["id"] for r in rows})
    task = {
        "id": tid,
        "status": "open",
        "priority": priority,
        "due": due,
        "created_at": now_iso(),
        "completed_at": "",
        "text": text,
    }
    rows.append(task)
    write_tasks(rows)
    log_event("created", task)
    print(f"added {c(tid, 'cyan')}  {text}")


def cmd_list(args: argparse.Namespace) -> None:
    rows = read_tasks()
    if not args.all:
        rows = [r for r in rows if r["status"] == "open"]
    if not rows:
        print(c("(no tasks)", "dim"))
        return

    rows.sort(key=lambda r: (
        PRIORITY_RANK.get(r["priority"], 9),
        r["due"] or "9999-99-99",
        r["id"],
    ))

    print(
        f"  {c(pad('ID', 14), 'bold')}  "
        f"{c(pad('STATUS', 6), 'bold')}  "
        f"{c(pad('PRIORITY', 8), 'bold')}  "
        f"{c(pad('DUE', 10), 'bold')}  "
        f"{c('TASK', 'bold')}"
    )
    for r in rows:
        status_label = STATUS_LABEL.get(r["status"], r["status"])
        status_cell = c(pad(status_label, 6), "green") if r["status"] == "done" else pad(status_label, 6)
        prio_label = PRIORITY_LABEL.get(r["priority"], r["priority"])
        prio_cell = c(pad(prio_label, 8), *priority_color(r["priority"]))
        due_cell = pad(r["due"] or "-", 10)
        text = c(r["text"], "dim") if r["status"] == "done" else r["text"]
        print(f"  {r['id']:<14}  {status_cell}  {prio_cell}  {due_cell}  {text}")


def _set_status(args: argparse.Namespace, target: str) -> None:
    rows = read_tasks()
    for arg in args.ids:
        tid = resolve_id(arg, rows)
        for r in rows:
            if r["id"] != tid:
                continue
            if r["status"] == target:
                print(c(f"{tid} already {target}", "dim"))
                break
            r["status"] = target
            r["completed_at"] = now_iso() if target == "done" else ""
            event = "completed" if target == "done" else "reopened"
            log_event(event, r)
            verb = "done" if target == "done" else "reopened"
            print(f"{verb}: {tid}  {r['text']}")
            break
    write_tasks(rows)


def cmd_done(args: argparse.Namespace) -> None:
    _set_status(args, "done")


def cmd_undone(args: argparse.Namespace) -> None:
    _set_status(args, "open")


def cmd_edit(args: argparse.Namespace) -> None:
    if args.text is None and args.due is None and args.priority is None:
        die("nothing to edit; pass --text, --due, or --priority")

    if args.due:
        if not DATE_RE.match(args.due):
            die(f"due date must be YYYY-MM-DD, got '{args.due}'")

    rows = read_tasks()
    tid = resolve_id(args.id, rows)
    for r in rows:
        if r["id"] != tid:
            continue
        changes = []
        if args.text is not None:
            r["text"] = sanitize_text(args.text)
            changes.append(f"text={r['text']!r}")
        if args.due is not None:
            r["due"] = args.due
            changes.append(f"due={r['due'] or '-'}")
        if args.priority is not None:
            r["priority"] = args.priority
            changes.append(f"priority={r['priority']}")
        log_event("edited", r)
        print(f"edited {tid}  " + "  ".join(changes))
        break
    write_tasks(rows)


def cmd_rm(args: argparse.Namespace) -> None:
    rows = read_tasks()
    keep = []
    removed_ids = set()
    targets = {resolve_id(a, rows) for a in args.ids}
    for r in rows:
        if r["id"] in targets:
            log_event("removed", r)
            removed_ids.add(r["id"])
        else:
            keep.append(r)
    write_tasks(keep)
    for tid in removed_ids:
        print(f"removed {tid}")


def cmd_clear_done(args: argparse.Namespace) -> None:
    rows = read_tasks()
    keep = []
    cleared = 0
    for r in rows:
        if r["status"] == "done":
            log_event("archived", r)
            cleared += 1
        else:
            keep.append(r)
    write_tasks(keep)
    print(f"cleared {cleared} done task(s)")


def cmd_log(args: argparse.Namespace) -> None:
    with EVENTS_LOG.open(newline="") as f:
        rows = list(csv.DictReader(f))
    tail = rows[-args.n:] if args.n else rows
    for r in tail:
        print(f"{r['timestamp']}  {r['event']:<9}  {r['id']}  "
              f"{r['priority']}  {r['due'] or '-':<10}  {r['text']}")


# ── report ────────────────────────────────────────────────────────────────────

def parse_window(args: argparse.Namespace) -> tuple[datetime | None, datetime | None]:
    now = datetime.now()
    end = now
    start: datetime | None = None

    if args.period == "week":
        start = now - timedelta(days=7)
    elif args.period == "month":
        start = now - timedelta(days=30)
    elif args.period == "year":
        start = now - timedelta(days=365)
    elif args.period == "all":
        start = None

    if args.from_:
        if not DATE_RE.match(args.from_):
            die(f"--from must be YYYY-MM-DD, got '{args.from_}'")
        start = datetime.fromisoformat(args.from_)
    if args.to:
        if not DATE_RE.match(args.to):
            die(f"--to must be YYYY-MM-DD, got '{args.to}'")
        end = datetime.fromisoformat(args.to) + timedelta(days=1) - timedelta(seconds=1)

    return start, end


def fmt_duration(seconds: float) -> str:
    if seconds <= 0:
        return "0s"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and not days:
        parts.append(f"{minutes}m")
    return " ".join(parts) or f"{int(seconds)}s"


def cmd_report(args: argparse.Namespace) -> None:
    start, end = parse_window(args)

    with EVENTS_LOG.open(newline="") as f:
        events = list(csv.DictReader(f))

    def in_window(ts: str) -> bool:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return False
        if start and dt < start:
            return False
        if end and dt > end:
            return False
        return True

    filtered = [e for e in events if in_window(e["timestamp"])]

    counts = Counter(e["event"] for e in filtered)
    completed_by_prio = Counter(
        e["priority"] for e in filtered if e["event"] == "completed"
    )
    completed_by_day = Counter(
        e["timestamp"][:10] for e in filtered if e["event"] == "completed"
    )

    # Avg time to completion: pair created (any time) ↔ completed (in window)
    created_at: dict[str, datetime] = {}
    for e in events:
        if e["event"] == "created":
            try:
                created_at[e["id"]] = datetime.fromisoformat(e["timestamp"])
            except ValueError:
                pass

    durations: list[float] = []
    for e in filtered:
        if e["event"] != "completed":
            continue
        if e["id"] not in created_at:
            continue
        try:
            done_dt = datetime.fromisoformat(e["timestamp"])
        except ValueError:
            continue
        durations.append((done_dt - created_at[e["id"]]).total_seconds())

    rows = read_tasks()
    still_open = sum(1 for r in rows if r["status"] == "open")

    created_n = counts.get("created", 0)
    completed_n = counts.get("completed", 0)
    removed_n = counts.get("removed", 0)
    completion_rate = (completed_n / created_n * 100) if created_n else 0.0

    period_label = "all time"
    if start and end:
        days = max(1, (end - start).days)
        period_label = f"{start.date()} .. {end.date()} ({days} days)"
    elif start:
        period_label = f"since {start.date()}"

    print(f"Period: {c(period_label, 'bold')}")
    print()
    print(f"  Created     {created_n:>3}")
    print(f"  Completed   {completed_n:>3}   ({completion_rate:.0f}% completion rate)")
    print(f"  Removed     {removed_n:>3}")
    print(f"  Still open  {still_open:>3}")
    print()
    if completed_by_prio:
        print("  By priority (completed):")
        for p in ("H", "M", "L", "-"):
            if completed_by_prio.get(p):
                print(f"    {p}  {completed_by_prio[p]}")
        print()
    if durations:
        avg = sum(durations) / len(durations)
        print(f"  Avg time to completion: {fmt_duration(avg)}")
    if completed_by_day:
        best_day, best_n = completed_by_day.most_common(1)[0]
        print(f"  Best day: {best_day} ({best_n} completed)")


# ── main ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="todo",
        description="terminal to-do list with lifecycle event log",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add a new task")
    a.add_argument("text")
    a.add_argument("-p", "--priority", choices=["H", "M", "L"])
    a.add_argument("-d", "--due", help="due date YYYY-MM-DD")
    a.set_defaults(func=cmd_add)

    l = sub.add_parser("list", help="list tasks")
    l.add_argument("--all", action="store_true", help="include done tasks")
    l.set_defaults(func=cmd_list)

    d = sub.add_parser("done", help="mark task(s) done")
    d.add_argument("ids", nargs="+")
    d.set_defaults(func=cmd_done)

    u = sub.add_parser("undone", help="reopen task(s)")
    u.add_argument("ids", nargs="+")
    u.set_defaults(func=cmd_undone)

    e = sub.add_parser("edit", help="edit task text, due, or priority")
    e.add_argument("id")
    e.add_argument("-t", "--text")
    e.add_argument("-d", "--due", help="due date YYYY-MM-DD, or empty string to clear")
    e.add_argument("-p", "--priority", choices=["H", "M", "L", "-"])
    e.set_defaults(func=cmd_edit)

    r = sub.add_parser("rm", help="remove task(s)")
    r.add_argument("ids", nargs="+")
    r.set_defaults(func=cmd_rm)

    sub.add_parser("clear-done", help="drop all done tasks from active list").set_defaults(func=cmd_clear_done)

    rep = sub.add_parser("report", help="performance stats")
    rep.add_argument("period", nargs="?", default="week",
                     choices=["week", "month", "year", "all"])
    rep.add_argument("--from", dest="from_", help="window start YYYY-MM-DD")
    rep.add_argument("--to", help="window end YYYY-MM-DD")
    rep.set_defaults(func=cmd_report)

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
