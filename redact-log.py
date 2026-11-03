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


# ── placeholders ──────────────────────────────────────────────────────────────
# While a line is processed, each redaction is one private-use character from
# Unicode plane 16 (unused in real logs). Later rules can't match inside it or
# split it; it becomes "[REDACTED:<KIND>]" once the whole line is done.

KINDS = ("PRIVATE_KEY", "CREDENTIALS", "JWT", "TOKEN", "API_KEY", "SECRET", "EMAIL",
         "CREDIT_CARD", "PHONE", "MAC", "IPV4", "IPV6", "USER")
_SENTINEL = {kind: chr(0x10FF00 + i) for i, kind in enumerate(KINDS)}
_UNSENTINEL = {ord(ch): f"[REDACTED:{kind}]" for kind, ch in _SENTINEL.items()}
S = r"\U0010ff00-\U0010ffff"                     # sentinel range, for character classes
_SENTINEL_RE = re.compile(f"[{S}]")
# Already redacted, in this run or by an earlier one (so re-running is a no-op).
DONE = rf"(?:[{S}]|\[REDACTED:)"


def tag(kind: str) -> str:
    return _SENTINEL[kind]


def finish(text: str) -> str:
    return text.translate(_UNSENTINEL)


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    replace: Callable[[re.Match[str]], str | None]   # None → keep the match as is
    example: str
    # Cheap pre-check: a line it rejects cannot match the pattern, so the regex
    # is skipped. Must never reject a line the pattern would match.
    hint: Callable[[str], bool] | None = None
    # Spans lines; handled by redact_stream() instead of a per-line substitution.
    block: bool = False


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
# Key material right after BEGIN on the same line: base64 and escaped newlines.
KEY_INLINE_RE = re.compile(r"(?:\\[rn]|[A-Za-z0-9+/=])+")
# Lines that can sit inside a PEM block. Any other line before END means the
# key was cut off and ordinary log lines have resumed.
PEM_LINE_RE = re.compile(
    r"\s*(?:[A-Za-z0-9+/=]*|(?:Proc-Type|DEK-Info|Comment|Version|Hash|Charset):[^\r\n]*)\s*")

# ── secrets ───────────────────────────────────────────────────────────────────

# user:password@ — the password may hold '@' or '/', so it runs to the last '@'.
URL_CREDS_RE = re.compile(
    r"(?<![\w+.-])([a-zA-Z][a-zA-Z0-9+.-]*://)(?!" + DONE + r")"
    r"([^\s:/@\"'<>]*):([^\s\"'<>]*)@")


def _url_creds(m: re.Match[str]) -> str | None:
    password = m[3]
    if "/" in password and password.split("/", 1)[0].isdigit():
        return None                              # host:8080/path/@x, not user:pass
    return f"{m[1]}{tag('CREDENTIALS')}@"


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

# A key counts when it ENDS in one of these, so db_password and GITHUB_TOKEN
# match but max_tokens and token_count don't. "pass"/"sid" must start a word
# segment (DB_PASS yes, bypass no).
_SECRET_KEYS = (
    r"password|passwd|passphrase|secret|token|credentials?|authorization|auth"
    r"|session(?:[_-]?id)?|apikey"
    r"|(?:secret|private|api|access|encryption|signing|master|session|client|auth"
    r"|license|ssh|gpg|pgp|crypto|hmac|aes|rsa|jwt|account|storage|service|sas"
    r"|subscription|app|consumer|webhook|deploy)[_.-]?key"
    r"|(?<![a-z])(?:pass|sid)")
_AUTH_SCHEMES = r"bearer|basic|digest|token"
_Q = r"(?:\\*[\"'])"                              # a quote, maybe escaped (JSON in JSON)


def _quoted(group: str) -> str:
    return (r'"(?:[^"\\]|\\.)+"'                                   # "string"
            r"|'[^'\r\n]+'"                                        # 'string'
            rf"|(?P<{group}>\\+[\"'])(?:(?!(?P={group})).)+(?P={group})")  # \"nested\"


def _value(group: str) -> str:
    return (_quoted(group)
            + r"|\{(?:[^{}]|\{[^{}]*\})*\}"                        # {object}
            r"|\[(?:[^\[\]]|\[[^\[\]]*\])*\]"                      # [array]
            r"|[\[{][^\r\n]*"                                      # unclosed: rest of line
            r"|(?:\\*[\"'])?(?:[^\s,;&\"'})\]\\]|\\(?![\"']))+")  # bare word


SECRET_RE = re.compile(
    # key=value, key: value, "key": "value", 'key' => 'value', \"key\":\"value\"
    r"(?P<key>(?<![\w.-])(?:(?P<cookie>[\w.-]*?cookie)|[\w.-]*?(?:" + _SECRET_KEYS + r")))"
    r"(?P<sep>" + _Q + r"?[ \t]*(?:=>|:=|[:=])[ \t]*)"
    r"(?P<scheme>" + _Q + r"?(?:" + _AUTH_SCHEMES + r")[ \t]+)?"
    r"(?!" + _Q + r"?" + DONE + r")"
    r"(?!" + _Q + r"?(?:" + _AUTH_SCHEMES + r")[ \t])"
    # a cookie header carries many name=value pairs: take the rest of the line
    r"(?P<val>(?(cookie)(?:" + _quoted("cq") + r"|[^\r\n]+)|(?:" + _value("vq") + r")))"
    # --password value, set PASSWORD value
    r"|(?P<flag>(?<![\w.-])--?|\b(?:set|setenv)[ \t]+)"
    r"(?P<fkey>[\w.-]*?(?:" + _SECRET_KEYS + r"))"
    r"(?P<fsep>[ \t]+)"
    r"(?!" + _Q + r"?" + DONE + r")"
    r"(?P<fval>" + _value("fq") + r")",
    re.I)
_QUOTE_RE = re.compile(r"\\*[\"']")


def _secret(m: re.Match[str]) -> str | None:
    if m["key"] is not None:
        head, val = m["key"] + m["sep"] + (m["scheme"] or ""), m["val"]
    else:
        if not m["flag"].startswith("-") and not m["fkey"].isupper():
            return None                          # "set password for bob" is prose
        head, val = m["flag"] + m["fkey"] + m["fsep"], m["fval"]
    q = _QUOTE_RE.match(val)
    q_open = q[0] if q else ""
    q_close = q_open if q_open and len(val) > len(q_open) and val.endswith(q_open) else ""
    return f"{head}{q_open}{tag('SECRET')}{q_close}"


# ── contact PII / financial ───────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"(?<![\w.+%-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")

# A run of digits with single space/dash separators. The card is looked for
# inside it, so an expiry date or CVV next to the number doesn't hide it.
CARD_RE = re.compile(r"(?<![\w+-])\d(?:[ -]?\d){12,}(?![\w-])")
CARD_IIN_RE = re.compile(r"""
    4\d{12}(?:\d{3}){0,2}                                         # Visa 13/16/19
  | (?:5[1-5]\d\d|222[1-9]|22[3-9]\d|2[3-6]\d\d|27[01]\d|2720)\d{12}  # Mastercard
  | 220[0-4]\d{12,15}                                             # Mir
  | 3[47]\d{13}                                                   # Amex
  | 3(?:0[0-5]|[68]\d)\d{11}                                      # Diners
  | 35(?:2[89]|[3-8]\d)\d{12,15}                                  # JCB
  | (?:50|5[6-9]|6\d)\d{11,17}                    # Maestro, Discover, UnionPay
""", re.X)
_DIGITS_RE = re.compile(r"\d+")


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _is_card(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and bool(CARD_IIN_RE.fullmatch(digits)) and luhn_ok(digits)


def _card(m: re.Match[str]) -> str | None:
    text = m[0]
    groups = list(_DIGITS_RE.finditer(text))
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(groups):                       # windows of whole digit groups
        digits, last = "", None
        for j in range(i, len(groups)):
            digits += groups[j][0]
            if len(digits) > 19:
                break
            if _is_card(digits):
                last = j                         # keep the longest valid window
        if last is None:
            i += 1
            continue
        spans.append((groups[i].start(), groups[last].end()))
        i = last + 1
    if not spans:
        return None
    out, pos = [], 0
    for start, end in spans:
        out += [text[pos:start], tag("CREDIT_CARD")]
        pos = end
    out.append(text[pos:])
    return "".join(out)


PHONE_RE = re.compile(r"""
    (?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?![\w:]|[.-]\d)        # +CC …
  | (?<![\w+-])(?:\(\d{3}\)[ ]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-]|[.:]\d)  # 3-3-4
""", re.X)


def _phone(m: re.Match[str]) -> str | None:
    digits = sum(ch.isdigit() for ch in m[0])
    return tag("PHONE") if 7 <= digits <= 15 else None


# ── network ───────────────────────────────────────────────────────────────────
# MAC and IPv6 may sit right after a "key:" (hw:aa:bb:…, addr:fe80::1); the
# optional, lazily matched <pre> keeps that key out of the address.

MAC_RE = re.compile(
    r"(?<![\w.:-])(?P<pre>[A-Za-z_][\w-]*[:=])??"
    r"(?P<addr>[0-9A-Fa-f]{2}(?P<sep>[:-])(?:[0-9A-Fa-f]{2}(?P=sep)){4}[0-9A-Fa-f]{2})"
    r"(?![\w:-])")

_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4_RE = re.compile(rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?!\w|\.\d)")

IPV6_RE = re.compile(
    r"(?<![\w.:-])(?P<pre>[A-Za-z_][\w-]*:)??"
    r"(?P<addr>(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![\w:])")


def _ipv6(m: re.Match[str]) -> str | None:
    addr = m["addr"]
    if not any(ch.isdigit() for ch in addr):     # cafe::beef, a::b are code, not IPs
        return None
    try:
        ipaddress.IPv6Address(addr)
    except ValueError:                           # 14:30:22 etc.
        return None
    return f"{m['pre'] or ''}{tag('IPV6')}"


# ── paths ─────────────────────────────────────────────────────────────────────

PATH_RE = re.compile(
    r"(?<![\w.-])((?:/var|/usr|/export)?/home/|/Users/"
    r"|[A-Za-z]:(?:\\+|/)(?i:users)(?:\\+|/))"
    r"(?!" + DONE + r")([^/\\\s\"'" + S + r"]+)")

# ── rule table (order matters: specific before generic) ───────────────────────

RULES: list[Rule] = [
    Rule("private_key", KEY_BEGIN_RE, whole("PRIVATE_KEY"),
         "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBg-----END PRIVATE KEY-----", block=True),
    Rule("url_creds", URL_CREDS_RE, _url_creds,
         "postgres://admin:s3cret@db:5432/app", contains("://")),
    Rule("jwt", JWT_RE, whole("JWT"),
         "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", contains("eyJ")),
    Rule("bearer", BEARER_RE, _bearer,
         "Authorization: Bearer 9f8e7d6c5b4a39281706f5e4", contains_ci("bearer", "basic")),
    Rule("api_key", API_KEY_RE, whole("API_KEY"), "AKIAIOSFODNN7EXAMPLE",
         re.compile(r"sk-|AKIA|ASIA|gh[pousr]_|github_pat_|glpat-|xox[abprs]-|AIza"
                    r"|_live_|_test_").search),
    Rule("secret", SECRET_RE, _secret, "password=hunter2",
         contains_ci("pass", "secret", "token", "key", "cred", "auth", "cookie",
                     "session", "sid")),
    Rule("email", EMAIL_RE, whole("EMAIL"), "jane@example.com", contains("@")),
    Rule("credit_card", CARD_RE, _card, "4111 1111 1111 1111"),
    Rule("phone", PHONE_RE, _phone, "+1 555 123 4567"),
    Rule("mac", MAC_RE, lambda m: f"{m['pre'] or ''}{tag('MAC')}", "00:1a:2b:3c:4d:5e",
         lambda line: line.count(":") >= 5 or line.count("-") >= 5),
    Rule("ipv4", IPV4_RE, whole("IPV4"), "192.168.1.20"),
    Rule("ipv6", IPV6_RE, _ipv6, "2001:db8::8a2e:370:7334",
         lambda line: "::" in line or line.count(":") >= 7),   # 8 groups or ::
    Rule("path", PATH_RE, lambda m: f"{m[1]}{tag('USER')}", "/home/jane/.ssh/id_rsa",
         contains_ci("/home/", "users")),
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


def _sentinels(text: str) -> int:
    return len(_SENTINEL_RE.findall(text))


def _bind(rules: list[Rule], counts: Counter) -> list[Sub]:
    def bind(rule: Rule) -> Sub:
        def repl(m: re.Match[str]) -> str:
            out = rule.replace(m)
            if out is None:
                return m[0]
            # one match can hold several values (two cards in one digit run)
            counts[rule.name] += max(1, _sentinels(out) - _sentinels(m[0]))
            return out
        sub = partial(rule.pattern.sub, repl)
        if rule.hint is None:
            return sub
        hint = rule.hint
        return lambda line: sub(line) if hint(line) else line
    return [bind(rule) for rule in rules if not rule.block]


def _apply(subs: list[Sub], text: str) -> str:
    for sub in subs:
        text = sub(text)
    return text


def redact_stream(src: TextIO, dst: TextIO, rules: list[Rule]) -> Counter:
    """Copy src to dst line by line with every rule applied. Returns hits per rule."""
    counts: Counter = Counter()
    subs = _bind(rules, counts)
    key_rule = next((rule for rule in rules if rule.block), None)
    in_key, eol = False, ""

    def key_found() -> str:
        counts[key_rule.name] += 1
        return tag("PRIVATE_KEY")

    for line in src:
        if in_key and not KEY_END_RE.search(line) and not PEM_LINE_RE.fullmatch(line):
            in_key = False                       # key was cut off; log lines resume
            dst.write(eol)
        parts, pos = [], 0
        while True:
            if in_key:                           # inside a key: drop text up to END
                end = KEY_END_RE.search(line, pos)
                if end is None:
                    break
                in_key, pos = False, end.end()
                continue
            begin = KEY_BEGIN_RE.search(line, pos) if key_rule else None
            if begin is None:
                parts.append(_apply(subs, line[pos:]))
                break
            parts.append(_apply(subs, line[pos:begin.start()]))
            pos = begin.end()
            if KEY_END_RE.search(line, pos) or not line[pos:].strip():
                parts.append(key_found())        # body on this line or the next ones
                in_key = True
                eol = line[len(line.rstrip("\r\n")):]
            elif inline := KEY_INLINE_RE.match(line, pos):
                parts.append(key_found())        # one-line key, cut off before END
                pos = inline.end()
            else:
                parts.append(begin[0])           # only mentions the header
        dst.write(finish("".join(parts)))
    if in_key:                                   # key runs to end of file
        dst.write(eol)
    return counts


def redact_text(text: str, rules: list[Rule] = RULES) -> str:
    dst = io.StringIO(newline="")
    redact_stream(io.StringIO(text, newline=""), dst, rules)
    return dst.getvalue()


# ── files ─────────────────────────────────────────────────────────────────────

_BOMS = ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_COMPRESSED = ((rb"\x1f\x8b", "gzip", "zcat"), (rb"\xfd7zXZ\x00", "xz", "xzcat"),
               (rb"BZh[1-9]1AY&SY", "bzip2", "bzcat"), (rb"\x28\xb5\x2f\xfd", "zstd", "zstdcat"),
               (rb"PK\x03\x04", "zip", "unzip -p"))


def sniff(head: bytes) -> tuple[bytes, str]:
    """Return (BOM, encoding) of a text log. Raises ValueError for anything else."""
    for bom, enc in _BOMS:
        if head.startswith(bom):
            return bom, enc
    for magic, kind, tool in _COMPRESSED:
        if re.match(magic, head):
            raise ValueError(f"{kind}-compressed; decompress first, e.g. "
                             f"{tool} FILE | redact-log /dev/stdin -o OUT")
    if b"\x00" in head:
        raise ValueError("has NUL bytes: binary, or UTF-16 without a BOM; convert it "
                         "to UTF-8 first (iconv -f UTF-16LE -t UTF-8)")
    return b"", "utf-8"


def default_output(path: Path) -> Path:
    return path.with_name(f"{path.stem}.redacted{path.suffix}")


def write_redacted(src: Path, out: Path, rules: list[Rule]) -> Counter:
    """Redact src into out atomically (temp file + rename); src is only read.

    Output keeps the input's encoding and BOM. Raises ValueError for input
    that isn't text (compressed, binary) or doesn't decode.
    """
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".tmp", dir=out.parent)
    try:
        with open(src, "rb") as raw, open(fd, "wb") as sink:
            bom, enc = sniff(raw.peek(4096)[:4096])
            raw.read(len(bom))
            sink.write(bom)
            # utf-8: surrogateescape keeps undecodable bytes exactly as they were
            errors = "surrogateescape" if enc == "utf-8" else "surrogatepass"
            with io.TextIOWrapper(raw, encoding=enc, errors=errors, newline="") as fin, \
                 io.TextIOWrapper(sink, encoding=enc, errors=errors, newline="") as fout:
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
    except ValueError as e:                      # not text, or doesn't decode
        die(f"{src}: {e}")
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
