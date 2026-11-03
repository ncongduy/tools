#!/usr/bin/env python3
"""Tests for redact-log.py — stdlib unittest, no dependencies.

Run: python3 -m unittest tests/test_redact_log.py -v
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "redact-log.py"

_spec = importlib.util.spec_from_file_location("redact_log", SCRIPT)
rl = importlib.util.module_from_spec(_spec)
sys.modules["redact_log"] = rl
_spec.loader.exec_module(rl)


def redact(text: str, rules=None):
    dst = io.StringIO(newline="")
    counts = rl.redact_stream(io.StringIO(text, newline=""), dst,
                              rules if rules is not None else rl.RULES)
    return dst.getvalue(), counts


def r(text: str) -> str:
    return redact(text)[0]


# ── secrets ───────────────────────────────────────────────────────────────────

class SecretRules(unittest.TestCase):

    def test_private_key_block_collapses_to_one_line(self):
        text = ("start\n"
                "key: -----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEowIBAAKCAQEA\n"
                "abcdefgh\n"
                "-----END RSA PRIVATE KEY-----\n"
                "after 10.0.0.1\n")
        out, counts = redact(text)
        self.assertEqual(out, "start\nkey: [REDACTED:PRIVATE_KEY]\nafter [REDACTED:IPV4]\n")
        self.assertEqual(counts["private_key"], 1)

    def test_private_key_escaped_on_one_line(self):
        text = '{"k": "-----BEGIN PRIVATE KEY-----\\nMIIE\\n-----END PRIVATE KEY-----\\n"}\n'
        self.assertEqual(r(text), '{"k": "[REDACTED:PRIVATE_KEY]\\n"}\n')

    def test_unterminated_private_key_redacts_to_eof(self):
        text = "a\n-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\nc2g\n"
        self.assertEqual(r(text), "a\n[REDACTED:PRIVATE_KEY]\n")

    def test_url_credentials_keep_scheme_and_host(self):
        self.assertEqual(r("db=postgres://admin:s3cr3t@db.internal:5432/app"),
                         "db=postgres://[REDACTED:CREDENTIALS]@db.internal:5432/app")

    def test_jwt(self):
        jwt = ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0."
               "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        self.assertEqual(r(f"session {jwt} ok"), "session [REDACTED:JWT] ok")

    def test_bearer_token_keeps_scheme(self):
        self.assertEqual(r("Authorization: Bearer abcdef0123456789abcdef"),
                         "Authorization: Bearer [REDACTED:TOKEN]")
        self.assertEqual(r("authorization: bearer abcdef0123456789abcdef"),
                         "authorization: bearer [REDACTED:TOKEN]")

    def test_short_bearer_in_authorization_header_still_redacted(self):
        self.assertEqual(r("Authorization: Bearer abc123"),
                         "Authorization: Bearer [REDACTED:SECRET]")

    def test_quoted_authorization_header_keeps_scheme(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        out, counts = redact(f'{{"Authorization": "Bearer {jwt}"}}')
        self.assertEqual(out, '{"Authorization": "Bearer [REDACTED:JWT]"}')
        self.assertEqual(counts["secret"], 0)
        self.assertEqual(r('{"Authorization": "Bearer abc123"}'),
                         '{"Authorization": "Bearer [REDACTED:SECRET]"}')

    def test_secret_after_unrelated_key(self):
        self.assertEqual(r("headers: Authorization: Bearer abc123"),
                         "headers: Authorization: Bearer [REDACTED:SECRET]")
        self.assertEqual(r("opts=password=abc"), "opts=password=[REDACTED:SECRET]")

    def test_basic_auth(self):
        # dXNlcjpwYXNz = base64("user:pass")
        self.assertEqual(r("Authorization: Basic dXNlcjpwYXNz"),
                         "Authorization: Basic [REDACTED:TOKEN]")

    def test_bearer_and_basic_in_prose_untouched(self):
        line = "Bearer token expired; basic setup done; Basic authentication on"
        self.assertEqual(r(line), line)

    def test_known_api_key_formats(self):
        # Built from pieces so no complete key-shaped literal sits in the source:
        # GitHub push protection blocks those even when they are fake.
        samples = [
            "AKIAIOSFODNN7EXAMPLE",
            "ghp_" + "a1B2" * 9,
            "github_pat_11ABCDEFG0_" + "a1" * 12,
            "sk-ant-api03-" + "x9" * 20,
            "sk-proj-" + "Ab3" * 12,
            "xoxb-" + "1234567890-" + "abcdefABCDEF",
            "AIzaSy" + "A1b2C3d4E5" * 3 + "f6G",
            "glpat-" + "x1Y2z3" * 4,
            "sk_live_" + "x1Y2" * 6,
        ]
        for key in samples:
            with self.subTest(key=key):
                self.assertEqual(r(f"used {key} here"), "used [REDACTED:API_KEY] here")

    def test_sk_prefix_without_digits_untouched(self):
        line = "installed sk-learn-compatible-wrapper-for-models"
        self.assertEqual(r(line), line)

    def test_secret_key_value_keeps_key(self):
        cases = {
            "password=hunter2": "password=[REDACTED:SECRET]",
            'DB_PASSWORD="p@ss word"': 'DB_PASSWORD="[REDACTED:SECRET]"',
            '{"client_secret": "abc123", "user": "bob"}':
                '{"client_secret": "[REDACTED:SECRET]", "user": "bob"}',
            "GET /cb?access_token=xyz789&state=ok":
                "GET /cb?access_token=[REDACTED:SECRET]&state=ok",
            "x-api-key: 12345abc": "x-api-key: [REDACTED:SECRET]",
            "GITHUB_TOKEN=abc": "GITHUB_TOKEN=[REDACTED:SECRET]",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_token_counters_and_lookalike_keys_untouched(self):
        line = "input_tokens=1234 max_tokens: 4096 token_count=5 auth_method=oauth PWD=/tmp"
        self.assertEqual(r(line), line)

    def test_placeholder_not_redacted_twice(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"
        out, counts = redact(f"token={jwt}")
        self.assertEqual(out, "token=[REDACTED:JWT]")
        self.assertEqual(counts["jwt"], 1)
        self.assertEqual(counts["secret"], 0)


# ── contact PII / financial ───────────────────────────────────────────────────

class PiiRules(unittest.TestCase):

    def test_email(self):
        self.assertEqual(r("user john.doe+tag@mail.example.co.uk logged in"),
                         "user [REDACTED:EMAIL] logged in")

    def test_email_lookalikes_untouched(self):
        line = "npm i @types/node pkg@1.2.3"
        self.assertEqual(r(line), line)

    def test_credit_cards(self):
        for card in ["4111 1111 1111 1111", "4111-1111-1111-1111",
                     "378282246310005", "5555555555554444"]:
            with self.subTest(card=card):
                self.assertEqual(r(f"card {card} ok"), "card [REDACTED:CREDIT_CARD] ok")

    def test_card_lookalikes_untouched(self):
        for line in ["ts=1730640000000",          # epoch ms, no card prefix
                     "card 4111111111111112",     # fails Luhn
                     "order 1234567890123456"]:   # no card prefix
            with self.subTest(line=line):
                self.assertEqual(r(line), line)

    def test_phones(self):
        for phone in ["+1 555 123 4567", "+358 40 123 4567", "+358401234567",
                      "(555) 123-4567", "040 123 4567", "555.123.4567"]:
            with self.subTest(phone=phone):
                self.assertEqual(r(f"call {phone} now"), "call [REDACTED:PHONE] now")

    def test_phone_does_not_swallow_following_time(self):
        self.assertEqual(r("call +1 555 123 4567 at 14:30"),
                         "call [REDACTED:PHONE] at 14:30")

    def test_timestamps_and_ids_untouched(self):
        for line in ["2026-11-03T14:30:22+02:00 pid 12345 took 1234567 ms",
                     "id=550e8400-e29b-41d4-a716-446655440000",
                     "[2026-11-03 14:30:22,123] INFO done in 0.25s"]:
            with self.subTest(line=line):
                self.assertEqual(r(line), line)


# ── network ───────────────────────────────────────────────────────────────────

class NetworkRules(unittest.TestCase):

    def test_mac(self):
        out, counts = redact("eth0 aa:bb:cc:dd:ee:ff up, wlan AA-BB-CC-DD-EE-FF")
        self.assertEqual(out, "eth0 [REDACTED:MAC] up, wlan [REDACTED:MAC]")
        self.assertEqual(counts["mac"], 2)
        self.assertEqual(counts["ipv6"], 0)

    def test_ipv4_keeps_port_and_trailing_dot(self):
        self.assertEqual(r("from 192.168.1.20:8080"), "from [REDACTED:IPV4]:8080")
        self.assertEqual(r("connected to 10.0.0.1."), "connected to [REDACTED:IPV4].")

    def test_ipv4_lookalikes_untouched(self):
        for line in ["version 1.2.3", "oid 1.2.3.4.5", "bad 999.1.1.1", "v1.2.3.4"]:
            with self.subTest(line=line):
                self.assertEqual(r(line), line)

    def test_ipv6(self):
        self.assertEqual(r("client 2001:db8::8a2e:370:7334 ok"), "client [REDACTED:IPV6] ok")
        self.assertEqual(r("listen [::1]:443"), "listen [[REDACTED:IPV6]]:443")

    def test_ipv6_lookalikes_untouched(self):
        line = "at 14:30:22 std::vector a::b cafe::beef"
        self.assertEqual(r(line), line)


# ── paths ─────────────────────────────────────────────────────────────────────

class PathRule(unittest.TestCase):

    def test_home_dirs_hide_username_only(self):
        cases = {
            "/home/ncd/.ssh/id_rsa": "/home/[REDACTED:USER]/.ssh/id_rsa",
            "/Users/alice/Library": "/Users/[REDACTED:USER]/Library",
            r"C:\Users\Bob\AppData": r"C:\Users\[REDACTED:USER]\AppData",
            r'"C:\\Users\\Bob\\x"': r'"C:\\Users\\[REDACTED:USER]\\x"',
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_url_path_untouched(self):
        line = "GET https://example.com/home/page"
        self.assertEqual(r(line), line)


# ── stream / rule selection ───────────────────────────────────────────────────

class StreamAndSelection(unittest.TestCase):

    def test_crlf_preserved(self):
        self.assertEqual(r("a 10.0.0.1\r\nb\r\n"), "a [REDACTED:IPV4]\r\nb\r\n")

    def test_counts_per_rule(self):
        _, counts = redact("a@b.co c@d.io 10.0.0.1\n")
        self.assertEqual(counts["email"], 2)
        self.assertEqual(counts["ipv4"], 1)

    def test_select_only(self):
        rules = rl.select_rules(only=["email"])
        out, _ = redact("a@b.co 10.0.0.1", rules)
        self.assertEqual(out, "[REDACTED:EMAIL] 10.0.0.1")

    def test_select_skip(self):
        rules = rl.select_rules(skip=["ipv4"])
        out, _ = redact("a@b.co 10.0.0.1", rules)
        self.assertEqual(out, "[REDACTED:EMAIL] 10.0.0.1")

    def test_select_unknown_rule_raises(self):
        with self.assertRaises(ValueError):
            rl.select_rules(only=["nope"])

    def test_every_rule_example_is_redacted_by_that_rule(self):
        for rule in rl.RULES:
            with self.subTest(rule=rule.name):
                _, counts = redact(rule.example)
                self.assertGreaterEqual(counts[rule.name], 1)

    def test_default_output(self):
        self.assertEqual(rl.default_output(Path("logs/app.log")), Path("logs/app.redacted.log"))
        self.assertEqual(rl.default_output(Path("syslog")), Path("syslog.redacted"))


# ── CLI ───────────────────────────────────────────────────────────────────────

class Cli(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.log = self.dir / "app.log"
        self.log.write_bytes(b"ip 10.0.0.1 mail a@b.co\n")

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args],
                              cwd=self.dir, capture_output=True, text=True)

    def test_writes_default_output_and_leaves_input(self):
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.dir / "app.redacted.log").read_bytes(),
                         b"ip [REDACTED:IPV4] mail [REDACTED:EMAIL]\n")
        self.assertEqual(self.log.read_bytes(), b"ip 10.0.0.1 mail a@b.co\n")
        self.assertIn("2 values redacted", res.stderr)

    def test_explicit_output(self):
        res = self.run_cli("app.log", "-o", "clean.txt")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.dir / "clean.txt").exists())

    def test_invalid_utf8_bytes_preserved(self):
        self.log.write_bytes(b"ip 10.0.0.1 \xff\xfe end\n")
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.dir / "app.redacted.log").read_bytes(),
                         b"ip [REDACTED:IPV4] \xff\xfe end\n")

    def test_refuses_existing_output_without_force(self):
        out = self.dir / "app.redacted.log"
        out.write_text("keep me")
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(out.read_text(), "keep me")
        res = self.run_cli("app.log", "--force")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[REDACTED:IPV4]", out.read_text())

    def test_refuses_output_equal_to_input(self):
        res = self.run_cli("app.log", "-o", "./app.log", "--force")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.log.read_bytes(), b"ip 10.0.0.1 mail a@b.co\n")

    def test_missing_input(self):
        res = self.run_cli("nope.log")
        self.assertEqual(res.returncode, 1)
        self.assertIn("nope.log", res.stderr)

    def test_unknown_rule(self):
        self.assertEqual(self.run_cli("app.log", "--skip", "bogus").returncode, 1)

    def test_only_and_skip_are_exclusive(self):
        self.assertEqual(self.run_cli("app.log", "--only", "email", "--skip", "ipv4").returncode, 2)

    def test_only_comma_list(self):
        res = self.run_cli("app.log", "--only", "email,mac")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual((self.dir / "app.redacted.log").read_text(),
                         "ip 10.0.0.1 mail [REDACTED:EMAIL]\n")

    def test_list_rules(self):
        res = self.run_cli("--list")
        self.assertEqual(res.returncode, 0, res.stderr)
        for rule in rl.RULES:
            self.assertIn(rule.name, res.stdout)

    def test_quiet(self):
        res = self.run_cli("app.log", "-q")
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stderr, "")

    def test_output_copies_input_mode(self):
        os.chmod(self.log, 0o640)
        self.run_cli("app.log")
        self.assertEqual((self.dir / "app.redacted.log").stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
