#!/usr/bin/env python3
"""claude-usage.py — analyze Claude Code token usage, cost, and prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# USD per million tokens. Matched by substring against message.model.
PRICES: list[tuple[str, dict]] = [
    ("opus-4-7",   {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50}),
    ("opus-4-6",   {"in": 15.0, "out": 75.0, "cw": 18.75, "cr": 1.50}),
    ("sonnet-4-6", {"in":  3.0, "out": 15.0, "cw":  3.75, "cr": 0.30}),
    ("sonnet-4-5", {"in":  3.0, "out": 15.0, "cw":  3.75, "cr": 0.30}),
    ("haiku-4-5",  {"in":  1.0, "out":  5.0, "cw":  1.25, "cr": 0.10}),
]

PROMPT_SNIPPET_LEN = 120

COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
TAG_BLOCK_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


# ── price helpers ─────────────────────────────────────────────────────────────

def price_for(model: str | None) -> dict | None:
    if not model:
        return None
    for key, p in PRICES:
        if key in model:
            return p
    return None


def model_family(model: str | None) -> str:
    if not model:
        return "other"
    for key, _ in PRICES:
        if key in model:
            return key
    return "other"


def compute_cost(t: "Turn") -> float | None:
    p = price_for(t.model)
    if p is None:
        return None
    return (
        t.input * p["in"] + t.output * p["out"]
        + t.cache_write * p["cw"] + t.cache_read * p["cr"]
    ) / 1_000_000


# ── turn model ────────────────────────────────────────────────────────────────

@dataclass
class Turn:
    ts: datetime
    date: str
    session: str
    project: str
    source_path: Path
    prompt: str           # snippet, ≤ PROMPT_SNIPPET_LEN
    prompt_full: str
    is_slash: bool
    model: str | None = None
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write + self.cache_read

    @property
    def id(self) -> str:
        h = hashlib.sha256(f"{self.session}|{self.ts.isoformat()}".encode())
        return h.hexdigest()[:8]


# ── JSONL parsing ─────────────────────────────────────────────────────────────

def is_real_user_prompt(entry: dict) -> bool:
    msg = entry.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "text" for c in content)
    return False


def extract_prompt_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return " ".join(parts)
    return ""


def display_prompt(text: str) -> str:
    """Snippet form: collapse whitespace, swap <command-name> for slash form."""
    m = COMMAND_NAME_RE.search(text)
    if m:
        cmd = m.group(1).strip()
        if not cmd.startswith("/"):
            cmd = "/" + cmd
        return cmd
    cleaned = TAG_BLOCK_RE.sub(" ", text)
    cleaned = WS_RE.sub(" ", cleaned).strip()
    return cleaned[:PROMPT_SNIPPET_LEN]


def strip_command_tags(text: str) -> str:
    """For `show` body: drop tag noise, leave actual user text."""
    text = re.sub(r"<command-name>([^<]+)</command-name>", r"\1", text)
    text = re.sub(r"<command-message>[^<]*</command-message>", "", text)
    text = re.sub(r"<command-args>[^<]*</command-args>", "", text)
    text = re.sub(r"<local-command-caveat>.*?</local-command-caveat>", "",
                  text, flags=re.S)
    text = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "",
                  text, flags=re.S)
    return text.strip()


def parse_ts(s: str, *, utc: bool) -> datetime:
    # JSONL timestamps end in 'Z'; fromisoformat (3.11+) handles it.
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if utc else dt.astimezone()


def iter_turns_from_file(
    path: Path, *, utc: bool, project: str,
) -> Iterator[Turn]:
    current: Turn | None = None
    with path.open() as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"warning: {path}:{lineno}: {exc}", file=sys.stderr)
                continue
            t = e.get("type")
            if t == "user" and is_real_user_prompt(e):
                if current is not None:
                    yield current
                msg = e.get("message") or {}
                full = extract_prompt_text(msg.get("content"))
                try:
                    ts = parse_ts(e["timestamp"], utc=utc)
                except (KeyError, ValueError):
                    continue
                current = Turn(
                    ts=ts,
                    date=ts.strftime("%Y-%m-%d"),
                    session=e.get("sessionId") or path.stem,
                    project=project,
                    source_path=path,
                    prompt=display_prompt(full),
                    prompt_full=full,
                    is_slash=bool(COMMAND_NAME_RE.search(full)),
                )
            elif t == "assistant" and current is not None:
                msg = e.get("message") or {}
                u = msg.get("usage") or {}
                current.input       += int(u.get("input_tokens") or 0)
                current.output      += int(u.get("output_tokens") or 0)
                current.cache_write += int(u.get("cache_creation_input_tokens") or 0)
                current.cache_read  += int(u.get("cache_read_input_tokens") or 0)
                current.model = msg.get("model") or current.model
    if current is not None:
        yield current


def collect_turns(
    *, from_date: str | None, to_date: str | None,
    utc: bool, project_filter: str | None,
) -> list[Turn]:
    if not PROJECTS_DIR.exists():
        print(f"{PROJECTS_DIR} not found — has Claude Code ever run here?",
              file=sys.stderr)
        sys.exit(1)

    if project_filter:
        if "/" in project_filter or project_filter in (".", ".."):
            print("error: --project must be a single directory name, not a path",
                  file=sys.stderr)
            sys.exit(1)
        paths: Iterable[Path] = (PROJECTS_DIR / project_filter).glob("*.jsonl")
    else:
        paths = PROJECTS_DIR.glob("*/*.jsonl")

    out: list[Turn] = []
    for p in paths:
        try:
            for turn in iter_turns_from_file(p, utc=utc, project=p.parent.name):
                if from_date is not None and turn.date < from_date:
                    continue
                if to_date is not None and turn.date > to_date:
                    continue
                out.append(turn)
        except OSError as exc:
            print(f"warning: skipping {p}: {exc}", file=sys.stderr)
    return out


# ── date window ───────────────────────────────────────────────────────────────

def resolve_window(period: str, utc: bool,
                   from_arg: str | None, to_arg: str | None) -> tuple[str, str]:
    if from_arg or to_arg:
        today_s = _today_str(utc)
        return (from_arg or "0000-01-01", to_arg or today_s)
    today = date.fromisoformat(_today_str(utc))
    if period == "all":
        return ("0000-01-01", "9999-12-31")
    if period == "today":
        s = today.isoformat()
        return (s, s)
    days = {"week": 7, "month": 30, "year": 365}[period]
    start = (today - timedelta(days=days - 1)).isoformat()
    return (start, today.isoformat())


def _today_str(utc: bool) -> str:
    from datetime import timezone
    if utc:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return datetime.now().astimezone().strftime("%Y-%m-%d")


# ── formatting ────────────────────────────────────────────────────────────────

def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,}"


def fmt_cost(c: float | None) -> str:
    if c is None:
        return "   n/a"
    return f"${c:7.2f}"


# ── aggregation ───────────────────────────────────────────────────────────────

def _zero() -> dict:
    return {"turns": 0, "input": 0, "output": 0,
            "cache_write": 0, "cache_read": 0, "total": 0, "cost": 0.0,
            "cost_known": False}


def aggregate(turns: list[Turn], keyfn) -> dict[str, dict]:
    agg: dict[str, dict] = defaultdict(_zero)
    for t in turns:
        k = keyfn(t)
        a = agg[k]
        a["turns"]       += 1
        a["input"]       += t.input
        a["output"]      += t.output
        a["cache_write"] += t.cache_write
        a["cache_read"]  += t.cache_read
        a["total"]       += t.total
        c = compute_cost(t)
        if c is not None:
            a["cost"] += c
            a["cost_known"] = True
    return dict(agg)


# ── report rendering ──────────────────────────────────────────────────────────

def render_report(turns: list[Turn], window: tuple[str, str], *,
                  utc: bool, top: int, bottom: int, by_day_flag: bool) -> None:
    from_d, to_d = window
    span_days = (date.fromisoformat(to_d) - date.fromisoformat(from_d)).days + 1
    tz_label = "utc" if utc else "local"

    print(f"Claude Code usage — {from_d} → {to_d}  "
          f"({span_days} day{'s' if span_days != 1 else ''}, {tz_label})")

    if not turns:
        print("  (no usage recorded)")
        return

    sessions = {t.session for t in turns}
    projects = {t.project for t in turns}
    total_cost_known = any(compute_cost(t) is not None for t in turns)
    print(f"  {len(turns)} turn{'s' if len(turns) != 1 else ''} across "
          f"{len(sessions)} session{'s' if len(sessions) != 1 else ''} / "
          f"{len(projects)} project{'s' if len(projects) != 1 else ''}")
    print()

    # — by model —
    by_model = aggregate(turns, lambda t: model_family(t.model))
    print("By model")
    hdr = f"  {'model':<12} {'turns':>6}  {'input':>8}  {'output':>8}  " \
          f"{'c_write':>8}  {'c_read':>8}  {'total':>8}  {'cost':>8}"
    print(hdr)
    rows = sorted(by_model.items(), key=lambda kv: -kv[1]["total"])
    for k, v in rows:
        cost = v["cost"] if v["cost_known"] else None
        print(f"  {k:<12} {v['turns']:>6}  "
              f"{fmt_tokens(v['input']):>8}  {fmt_tokens(v['output']):>8}  "
              f"{fmt_tokens(v['cache_write']):>8}  {fmt_tokens(v['cache_read']):>8}  "
              f"{fmt_tokens(v['total']):>8}  {fmt_cost(cost):>8}")
    grand = _grand(by_model)
    print(f"  {'─' * (len(hdr) - 2)}")
    g_cost = grand["cost"] if grand["cost_known"] else None
    print(f"  {'TOTAL':<12} {grand['turns']:>6}  "
          f"{fmt_tokens(grand['input']):>8}  {fmt_tokens(grand['output']):>8}  "
          f"{fmt_tokens(grand['cache_write']):>8}  {fmt_tokens(grand['cache_read']):>8}  "
          f"{fmt_tokens(grand['total']):>8}  {fmt_cost(g_cost):>8}")
    print()

    # — by day —
    if span_days > 1 or by_day_flag:
        print("By day")
        print(f"  {'date':<12} {'turns':>6}  {'tokens':>10}  {'cost':>8}")
        by_day = aggregate(turns, lambda t: t.date)
        for k in sorted(by_day.keys()):
            v = by_day[k]
            cost = v["cost"] if v["cost_known"] else None
            print(f"  {k:<12} {v['turns']:>6}  "
                  f"{fmt_tokens(v['total']):>10}  {fmt_cost(cost):>8}")
        print()

    # — top expensive —
    expensive = sorted([t for t in turns if t.total > 0],
                       key=lambda t: -t.total)[:top]
    if expensive:
        print(f"Top {len(expensive)} expensive prompts")
        print(f"  {'id':<8}  {'date':<10}  {'model':<12}  "
              f"{'tokens':>8}  {'cost':>8}   prompt")
        for t in expensive:
            cost = compute_cost(t)
            print(f"  {t.id:<8}  {t.date:<10}  {model_family(t.model):<12}  "
                  f"{fmt_tokens(t.total):>8}  {fmt_cost(cost):>8}   {t.prompt}")
        print()

    # — efficient —
    efficient = sorted([t for t in turns if t.output > 0],
                       key=lambda t: t.total)[:bottom]
    if efficient:
        print(f"{len(efficient)} most efficient prompts (smallest spend with a reply)")
        print(f"  {'id':<8}  {'date':<10}  {'model':<12}  "
              f"{'tokens':>8}  {'cost':>8}   prompt")
        for t in efficient:
            cost = compute_cost(t)
            print(f"  {t.id:<8}  {t.date:<10}  {model_family(t.model):<12}  "
                  f"{fmt_tokens(t.total):>8}  {fmt_cost(cost):>8}   {t.prompt}")
        print()

    if total_cost_known:
        print(f"  Use `claude-usage.py show <id>` to print the full prompt.")


def _grand(agg: dict[str, dict]) -> dict:
    g = _zero()
    for v in agg.values():
        for k in ("turns", "input", "output", "cache_write",
                  "cache_read", "total"):
            g[k] += v[k]
        if v["cost_known"]:
            g["cost"] += v["cost"]
            g["cost_known"] = True
    return g


# ── JSON output ───────────────────────────────────────────────────────────────

def turn_to_dict(t: Turn) -> dict:
    return {
        "id": t.id,
        "ts": t.ts.isoformat(),
        "date": t.date,
        "session": t.session,
        "project": t.project,
        "model": t.model,
        "model_family": model_family(t.model),
        "input": t.input,
        "output": t.output,
        "cache_write": t.cache_write,
        "cache_read": t.cache_read,
        "total": t.total,
        "cost": compute_cost(t),
        "prompt": t.prompt,
    }


def render_json(turns: list[Turn], window: tuple[str, str], *,
                utc: bool, top: int, bottom: int) -> None:
    by_model = aggregate(turns, lambda t: model_family(t.model))
    by_day = aggregate(turns, lambda t: t.date)
    expensive = sorted([t for t in turns if t.total > 0],
                       key=lambda t: -t.total)[:top]
    efficient = sorted([t for t in turns if t.output > 0],
                       key=lambda t: t.total)[:bottom]

    def _clean(d: dict) -> dict:
        out = {k: v for k, v in d.items() if k != "cost_known"}
        if not d["cost_known"]:
            out["cost"] = None
        return out

    payload = {
        "period": {"from": window[0], "to": window[1],
                   "tz": "utc" if utc else "local"},
        "totals": _clean(_grand(by_model)),
        "by_model": [{"model": k, **_clean(v)}
                     for k, v in sorted(by_model.items(),
                                        key=lambda kv: -kv[1]["total"])],
        "by_day":   [{"date": k, **_clean(v)} for k, v in sorted(by_day.items())],
        "expensive": [turn_to_dict(t) for t in expensive],
        "efficient": [turn_to_dict(t) for t in efficient],
    }
    json.dump(payload, sys.stdout, indent=2, default=str)
    print()


# ── show subcommand ───────────────────────────────────────────────────────────

def cmd_show(id_prefix: str, *, raw: bool, utc: bool) -> int:
    turns = collect_turns(from_date=None, to_date=None,
                          utc=utc, project_filter=None)
    matches = [t for t in turns if t.id.startswith(id_prefix)]
    if not matches:
        print(f"no turn matches id '{id_prefix}'", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"ambiguous id '{id_prefix}' — {len(matches)} matches:",
              file=sys.stderr)
        for t in matches[:8]:
            print(f"  {t.id}  {t.date}  {t.prompt}", file=sys.stderr)
        return 1
    t = matches[0]
    if raw:
        sys.stdout.write(t.prompt_full)
        if not t.prompt_full.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    cost = compute_cost(t)
    print(f"id      : {t.id}")
    print(f"when    : {t.ts.isoformat()}")
    print(f"session : {t.session}")
    print(f"project : {t.project}")
    print(f"source  : {t.source_path}")
    print(f"model   : {t.model or '(unknown)'}")
    print(f"tokens  : in={fmt_int(t.input)} out={fmt_int(t.output)} "
          f"c_write={fmt_int(t.cache_write)} c_read={fmt_int(t.cache_read)} "
          f"total={fmt_int(t.total)}")
    print(f"cost    : {fmt_cost(cost).strip()}")
    print()
    print(strip_command_tags(t.prompt_full))
    return 0


# ── report subcommand ─────────────────────────────────────────────────────────

def cmd_report(args: argparse.Namespace) -> int:
    window = resolve_window(args.period, args.utc, args.from_, args.to)
    turns = collect_turns(from_date=window[0], to_date=window[1],
                          utc=args.utc, project_filter=args.project)
    if args.json:
        render_json(turns, window, utc=args.utc,
                    top=args.top, bottom=args.bottom)
    else:
        render_report(turns, window, utc=args.utc,
                      top=args.top, bottom=args.bottom,
                      by_day_flag=args.by_day)
    return 0


# ── entry point ───────────────────────────────────────────────────────────────

def build_report_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-usage.py",
        description="Analyze Claude Code token usage, cost, and prompts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  claude-usage.py                   # today\n"
            "  claude-usage.py week\n"
            "  claude-usage.py month --top 20\n"
            "  claude-usage.py --from 2026-05-01 --to 2026-05-20\n"
            "  claude-usage.py year --json | jq .totals\n"
            "  claude-usage.py show a1b2c3d4     # full prompt for that turn\n"
            "\n"
            "Efficiency = total tokens spent on a turn that produced a reply,\n"
            "ascending. Cheapest answered turns surface as 'efficient'.\n"
        ),
    )
    p.add_argument("period", nargs="?", default="today",
                   choices=["today", "week", "month", "year", "all"])
    p.add_argument("--from", dest="from_", metavar="YYYY-MM-DD",
                   help="start date (overrides period)")
    p.add_argument("--to", dest="to", metavar="YYYY-MM-DD",
                   help="end date (overrides period)")
    p.add_argument("--top", type=int, default=10,
                   help="how many expensive prompts to list (default 10)")
    p.add_argument("--bottom", type=int, default=5,
                   help="how many efficient prompts to list (default 5)")
    p.add_argument("--by-day", action="store_true",
                   help="force per-day table (auto-on when range > 1 day)")
    p.add_argument("--utc", action="store_true",
                   help="interpret/display dates in UTC (default local)")
    p.add_argument("--project", metavar="DIR",
                   help="restrict to one project (encoded-cwd dir name)")
    p.add_argument("--json", action="store_true",
                   help="emit raw aggregates as JSON, skip pretty tables")
    return p


def build_show_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude-usage.py show",
        description="Print the full prompt of a single turn by id prefix.",
    )
    p.add_argument("id", help="turn id (full 8-char hex, or any unique prefix)")
    p.add_argument("--raw", action="store_true",
                   help="print only the prompt body, no header / no tag stripping")
    p.add_argument("--utc", action="store_true",
                   help="match timestamps in UTC (default local)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "show":
        args = build_show_parser().parse_args(argv[1:])
        return cmd_show(args.id, raw=args.raw, utc=args.utc)
    args = build_report_parser().parse_args(argv)
    return cmd_report(args)


if __name__ == "__main__":
    sys.exit(main())
