#!/usr/bin/env python3
"""redact-log.py — mask secrets and personal data in a text log, write a new file."""

from __future__ import annotations

import argparse
import base64
import binascii
import contextlib
import ipaddress
import io
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, Iterable, NoReturn, TextIO


def tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str | None]   # None → keep the match as is
    example: str
    # Cheap pre-check: a line it rejects cannot match the pattern, so the regex
    # is skipped. Must never reject a line the pattern would match.
    hint: Callable[[str], bool] | None = None


def whole(kind: str) -> Callable[[re.Match[str]], str]:
    placeholder = tag(kind)
    return lambda m: placeholder


def contains(*needles: str) -> Callable[[str], bool]:
    return lambda line: any(n in line for n in needles)


def contains_ci(*needles: str) -> Callable[[str], bool]:
    def check(line: str) -> bool:
        low = line.lower()
        return any(n in low for n in needles)
    return check


# ── private keys ──────────────────────────────────────────────────────────────

_KEY_LABEL = r"[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?"
KEY_BEGIN_RE = re.compile(rf"-----BEGIN {_KEY_LABEL}-----")
KEY_END_RE = re.compile(rf"-----END {_KEY_LABEL}-----")
# Whole key on one line (e.g. JSON with escaped \n). Multi-line blocks are
# handled by redact_stream().
PRIVATE_KEY_RE = re.compile(rf"{KEY_BEGIN_RE.pattern}.*?{KEY_END_RE.pattern}")

# ── secrets ───────────────────────────────────────────────────────────────────

URL_CREDS_RE = re.compile(r"\b([a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")

JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")

BEARER_RE = re.compile(
    r"\b(?P<scheme>(?i:bearer)|Basic)(?P<sp>[ \t]+)(?P<tok>[A-Za-z0-9._~+/-]+=*)")


def _basic_ok(token: str) -> bool:
    try:
        return b":" in base64.b64decode(token, validate=True)
    except (binascii.Error, ValueError):
        return False


def _bearer(m: re.Match[str]) -> str | None:
    tok = m["tok"]
    if m["scheme"] == "Basic":
        ok = _basic_ok(tok)
    else:
        ok = len(tok.rstrip("=")) >= 16          # "Bearer token expired" stays
    return f"{m['scheme']}{m['sp']}{tag('TOKEN')}" if ok else None


API_KEY_RE = re.compile(r"""\b(?:
    sk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}     # Anthropic sk-ant-…, OpenAI sk-…
  | (?:AKIA|ASIA)[0-9A-Z]{16}                      # AWS access key id
  | gh[pousr]_[A-Za-z0-9]{36,}                     # GitHub tokens
  | github_pat_[A-Za-z0-9_]{22,}                   # GitHub fine-grained PAT
  | glpat-[A-Za-z0-9_-]{20,}                       # GitLab PAT
  | xox[abprs]-[A-Za-z0-9-]{10,}                   # Slack
  | AIza[0-9A-Za-z_-]{35}                          # Google API key
  | (?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}       # Stripe secret / restricted
)(?![A-Za-z0-9_-])""", re.X)

# key=value / key: value / "key": "value" where the key ENDS in a sensitive
# word — so db_password and GITHUB_TOKEN match, max_tokens and token_count don't.
_SECRET_KEYS = (r"password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key"
                r"|private[_-]?key|client[_-]?secret|credentials?|authorization|auth")
_AUTH_SCHEMES = r"bearer|basic|digest|token"
SECRET_RE = re.compile(
    r"(?P<key>(?<![\w-])[\w.-]*?(?:" + _SECRET_KEYS + r"))"
    r"(?P<sep>[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<scheme>[\"']?(?:" + _AUTH_SCHEMES + r")[ \t]+)?"
    r"(?![\"']?\[REDACTED)"                       # already masked by an earlier rule
    r"(?![\"']?(?:" + _AUTH_SCHEMES + r")[ \t])"
    r"(?P<val>\"(?:[^\"\\]|\\.)+\"|'[^']+'|[\"']?[^\s,;&\"'})\]]+)",
    re.I)


def _secret(m: re.Match[str]) -> str:
    val = m["val"]
    q_open = val[0] if val[0] in "\"'" else ""
    q_close = val[-1] if q_open and len(val) > 1 and val[-1] == q_open else ""
    return f"{m['key']}{m['sep']}{m['scheme'] or ''}{q_open}{tag('SECRET')}{q_close}"


# ── contact PII / financial ───────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")

CARD_RE = re.compile(r"(?<![\w+-])\d(?:[ -]?\d){12,18}(?![\w-])")
CARD_IIN_RE = re.compile(r"""
    4\d{12}(?:\d{3}){0,2}                                         # Visa 13/16/19
  | (?:5[1-5]\d\d|222[1-9]|22[3-9]\d|2[3-6]\d\d|27[01]\d|2720)\d{12}  # Mastercard
  | 3[47]\d{13}                                                   # Amex
  | 3(?:0[0-5]|[68]\d)\d{11}                                      # Diners
  | (?:6011|65\d\d|64[4-9]\d)\d{12,15}                            # Discover
  | 35(?:2[89]|[3-8]\d)\d{12,15}                                  # JCB
""", re.X)


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match[str]) -> str | None:
    digits = re.sub(r"[ -]", "", m[0])
    if CARD_IIN_RE.fullmatch(digits) and luhn_ok(digits):
        return tag("CREDIT_CARD")
    return None


PHONE_RE = re.compile(r"""
    (?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?![\w:]|[.-]\d)        # +CC …
  | (?<![\w+-])(?:\(\d{3}\)[ ]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-]|[.:]\d)  # 3-3-4
""", re.X)


def _phone(m: re.Match[str]) -> str | None:
    digits = sum(ch.isdigit() for ch in m[0])
    return tag("PHONE") if 7 <= digits <= 15 else None


# ── network ───────────────────────────────────────────────────────────────────

MAC_RE = re.compile(
    r"(?<![\w:-])[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}(?![\w:-])")

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4_RE = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\w|\.\d)")

IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")


def _ipv6(m: re.Match[str]) -> str | None:
    text = m[0]
    if not any(ch.isdigit() for ch in text):     # cafe::beef, a::b are code, not IPs
        return None
    try:
        ipaddress.IPv6Address(text)
    except ValueError:                           # 14:30:22 etc.
        return None
    return tag("IPV6")


# ── paths ─────────────────────────────────────────────────────────────────────

PATH_RE = re.compile(
    r"(?<![\w.-])(/home/|/Users/|[A-Za-z]:\\+Users\\+)(?!\[REDACTED)([^/\\\s\"']+)")

# ── rule table (order matters: specific before generic) ───────────────────────

RULES: list[Rule] = [
    Rule("private_key", PRIVATE_KEY_RE, whole("PRIVATE_KEY"),
         "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBg-----END PRIVATE KEY-----",
         contains("PRIVATE KEY")),
    Rule("url_creds", URL_CREDS_RE, lambda m: f"{m[1]}{tag('CREDENTIALS')}@",
         "postgres://admin:s3cret@db:5432/app", contains("://")),
    Rule("jwt", JWT_RE, whole("JWT"),
         "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", contains("eyJ")),
    Rule("bearer", BEARER_RE, _bearer,
         "Authorization: Bearer 9f8e7d6c5b4a39281706f5e4", contains_ci("bearer", "basic")),
    Rule("api_key", API_KEY_RE, whole("API_KEY"), "AKIAIOSFODNN7EXAMPLE",
         re.compile(r"sk-|AKIA|ASIA|gh[pousr]_|github_pat_|glpat-|xox[abprs]-|AIza"
                    r"|_live_|_test_").search),
    Rule("secret", SECRET_RE, _secret, "password=hunter2",
         contains_ci("pass", "secret", "token", "key", "cred", "auth")),
    Rule("email", EMAIL_RE, whole("EMAIL"), "jane@example.com", contains("@")),
    Rule("credit_card", CARD_RE, _card, "4111 1111 1111 1111"),
    Rule("phone", PHONE_RE, _phone, "+1 555 123 4567"),
    Rule("mac", MAC_RE, whole("MAC"), "00:1a:2b:3c:4d:5e",
         lambda line: line.count(":") >= 5 or line.count("-") >= 5),
    Rule("ipv4", IPV4_RE, whole("IPV4"), "192.168.1.20"),
    Rule("ipv6", IPV6_RE, _ipv6, "2001:db8::8a2e:370:7334",
         lambda line: "::" in line or line.count(":") >= 7),   # 8 groups or ::
    Rule("path", PATH_RE, lambda m: f"{m[1]}{tag('USER')}", "/home/jane/.ssh/id_rsa",
         contains("/home/", "/Users/", "Users\\")),
]
RULE_NAMES = [rule.name for rule in RULES]


def select_rules(only: Iterable[str] | None = None,
                 skip: Iterable[str] | None = None) -> list[Rule]:
    only, skip = list(only or []), list(skip or [])
    unknown = sorted(set(only + skip) - set(RULE_NAMES))
    if unknown:
        raise ValueError(f"unknown rule(s): {', '.join(unknown)} (see --list)")
    if only:
        return [rule for rule in RULES if rule.name in only]
    return [rule for rule in RULES if rule.name not in skip]


# ── redaction ─────────────────────────────────────────────────────────────────

Sub = Callable[[str], str]


def _bind(rules: list[Rule], counts: Counter) -> list[Sub]:
    def bind(rule: Rule) -> Sub:
        def repl(m: re.Match[str]) -> str:
            out = rule.replace(m)
            if out is None:
                return m[0]
            counts[rule.name] += 1
            return out
        sub = partial(rule.pattern.sub, repl)
        if rule.hint is None:
            return sub
        hint = rule.hint
        return lambda line: sub(line) if hint(line) else line
    return [bind(rule) for rule in rules]


def _apply(subs: list[Sub], line: str) -> str:
    for sub in subs:
        line = sub(line)
    return line


def redact_stream(src: TextIO, dst: TextIO, rules: list[Rule]) -> Counter:
    """Copy src to dst line by line with every rule applied. Returns hits per rule."""
    counts: Counter = Counter()
    subs = _bind(rules, counts)
    multiline_keys = any(rule.name == "private_key" for rule in rules)
    in_key, eol = False, ""
    for line in src:
        if in_key:                               # drop key body until END
            end = KEY_END_RE.search(line)
            if end is None:
                continue
            in_key = False
            line = line[end.end():]
        elif multiline_keys:
            begin = KEY_BEGIN_RE.search(line)
            if begin and not KEY_END_RE.search(line, begin.end()):
                dst.write(_apply(subs, line[:begin.start()]) + tag("PRIVATE_KEY"))
                counts["private_key"] += 1
                eol = line[len(line.rstrip("\r\n")):]
                in_key = True
                continue
        dst.write(_apply(subs, line))
    if in_key:                                   # truncated key: still hidden
        dst.write(eol)
    return counts


def redact_text(text: str, rules: list[Rule] = RULES) -> str:
    dst = io.StringIO(newline="")
    redact_stream(io.StringIO(text, newline=""), dst, rules)
    return dst.getvalue()


# ── files ─────────────────────────────────────────────────────────────────────

def default_output(path: Path) -> Path:
    return path.with_name(f"{path.stem}.redacted{path.suffix}")


def write_redacted(src: Path, out: Path, rules: list[Rule]) -> Counter:
    """Redact src into out atomically (temp file + rename); src is only read."""
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        # surrogateescape + newline="" → odd bytes and CRLF round-trip untouched
        with open(src, encoding="utf-8", errors="surrogateescape", newline="") as fin, \
             os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape", newline="") as fout:
            counts = redact_stream(fin, fout, rules)
        if src.is_file():
            shutil.copymode(src, tmp)
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return counts


# ── output ────────────────────────────────────────────────────────────────────

def die(msg: str) -> NoReturn:
    print(f"❌  redact-log: {msg}", file=sys.stderr)
    sys.exit(1)


def print_summary(counts: Counter, out: Path) -> None:
    total = sum(counts.values())
    noun = "value" if total == 1 else "values"
    print(f"✅  {total} {noun} redacted → {out}", file=sys.stderr)
    for name in RULE_NAMES:
        if counts[name]:
            print(f"    {name:<12} {counts[name]}", file=sys.stderr)


def print_rules() -> None:
    for rule in RULES:
        print(f"{rule.name:<12} {rule.example}")
        print(f"{'':<12} → {redact_text(rule.example)}")


# ── main ──────────────────────────────────────────────────────────────────────

def csv_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="redact-log",
        description="mask secrets and personal data in a text log; "
                    "writes a new file and never modifies the input",
    )
    p.add_argument("input", nargs="?", type=Path, help="log file to redact")
    p.add_argument("-o", "--output", type=Path,
                   help="output file (default: <name>.redacted<ext> next to the input)")
    p.add_argument("-f", "--force", action="store_true",
                   help="overwrite the output file if it already exists")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--only", type=csv_list, metavar="RULES",
                   help="comma-separated rules to run (see --list)")
    g.add_argument("--skip", type=csv_list, metavar="RULES",
                   help="comma-separated rules to leave out")
    p.add_argument("-q", "--quiet", action="store_true", help="no summary on stderr")
    p.add_argument("--list", action="store_true", help="show the rules with an example and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        print_rules()
        return 0
    if args.input is None:
        parser.error("the following arguments are required: input")

    try:
        rules = select_rules(args.only, args.skip)
    except ValueError as e:
        die(str(e))

    src: Path = args.input
    if not src.exists():
        die(f"{src}: no such file")
    if src.is_dir():
        die(f"{src}: is a directory")
    out: Path = args.output or default_output(src)
    if out.is_dir():
        die(f"{out}: is a directory")
    if out.exists():
        if os.path.samefile(src, out):
            die(f"{out}: output would overwrite the input")
        if not args.force:
            die(f"{out} already exists (use --force to overwrite)")

    try:
        counts = write_redacted(src, out, rules)
    except OSError as e:
        die(f"{e.filename or src}: {e.strerror}")
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130

    if not args.quiet:
        print_summary(counts, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
