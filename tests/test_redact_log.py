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
import gzip
import tempfile
import time
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


SAMPLE = (
    "2026-11-03T14:30:22+02:00 GET /cb?access_token=xyz789 from 203.0.113.7 (2001:db8::1)\n"
    'headers {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"}\n'
    "user=jane@example.com phone=+358 40 123 4567 mac=00:1a:2b:3c:4d:5e card 4111 1111 1111 1111\n"
    "db postgres://admin:pw@10.0.0.5/app DB_PASSWORD=\"x y\" AWS=AKIAIOSFODNN7EXAMPLE HOME=/home/jane\n"
    "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----\n"
)


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


# ── review fixes: values that used to leak ────────────────────────────────────

class CardWindows(unittest.TestCase):

    def test_card_next_to_other_digits(self):
        cases = {
            "card 4111 1111 1111 1111 12/25": "card [REDACTED:CREDIT_CARD] 12/25",
            "card 4111111111111111 123": "card [REDACTED:CREDIT_CARD] 123",
            "14:30:22 4111111111111111": "14:30:22 [REDACTED:CREDIT_CARD]",
            "2026-11-03 4111111111111111": "2026-11-03 [REDACTED:CREDIT_CARD]",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_two_cards_in_one_digit_run(self):
        out, counts = redact("4111111111111111 5555555555554444")
        self.assertEqual(out, "[REDACTED:CREDIT_CARD] [REDACTED:CREDIT_CARD]")
        self.assertEqual(counts["credit_card"], 2)

    def test_more_card_networks(self):
        for card in ["6250941006528599",    # UnionPay
                     "6759649826438453",    # Maestro
                     "2200000000000004"]:   # Mir
            with self.subTest(card=card):
                self.assertEqual(r(f"card {card} ok"), "card [REDACTED:CREDIT_CARD] ok")


class SecretForms(unittest.TestCase):

    def test_escaped_json_in_json(self):
        src = r'{"body":"{\"password\":\"hunter2\",\"token\":\"abc\"}"}'
        want = r'{"body":"{\"password\":\"[REDACTED:SECRET]\",\"token\":\"[REDACTED:SECRET]\"}"}'
        self.assertEqual(r(src), want)

    def test_more_secret_key_names(self):
        cases = {
            "SECRET_KEY=django-insecure-abc123": "SECRET_KEY=[REDACTED:SECRET]",
            "ENCRYPTION_KEY=abcd1234": "ENCRYPTION_KEY=[REDACTED:SECRET]",
            "secretKey: abc": "secretKey: [REDACTED:SECRET]",
            "signing_key=abc": "signing_key=[REDACTED:SECRET]",
            "DB_PASS=hunter2": "DB_PASS=[REDACTED:SECRET]",
            '"pass": "hunter2"': '"pass": "[REDACTED:SECRET]"',
            "passphrase=correct": "passphrase=[REDACTED:SECRET]",
            "session_id=abc123": "session_id=[REDACTED:SECRET]",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_cookie_header_redacted_to_end_of_line(self):
        self.assertEqual(r("Cookie: session=abc123def456; theme=dark\n"),
                         "Cookie: [REDACTED:SECRET]\n")
        self.assertEqual(r("Set-Cookie: sid=abc; Path=/; HttpOnly"),
                         "Set-Cookie: [REDACTED:SECRET]")
        self.assertEqual(r('{"Cookie": "a=1; b=2", "Host": "x"}'),
                         '{"Cookie": "[REDACTED:SECRET]", "Host": "x"}')

    def test_ordinary_keys_untouched(self):
        line = "bypass=true compass=north cache_key=user:42 primary_key=id cookies_enabled=1"
        self.assertEqual(r(line), line)

    def test_arrow_and_walrus_separators(self):
        self.assertEqual(r("'password' => 'hunter2'"), "'password' => '[REDACTED:SECRET]'")
        self.assertEqual(r('password := "hunter2"'), 'password := "[REDACTED:SECRET]"')

    def test_array_and_object_values(self):
        cases = {
            '"credentials": ["sekrit1","sekrit2"]': '"credentials": [REDACTED:SECRET]',
            '{"auth": {"user": "bob", "pass": "hunter2"}}': '{"auth": [REDACTED:SECRET]}',
            '{"auth": {"a": {"b": "c"}, "d": "e"}}': '{"auth": [REDACTED:SECRET]}',
            '"token": ["abc", "def': '"token": [REDACTED:SECRET]',
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_cli_flag_with_space(self):
        self.assertEqual(r("mysql -u root --password hunter2"),
                         "mysql -u root --password [REDACTED:SECRET]")
        self.assertEqual(r("set PASSWORD hunter2"), "set PASSWORD [REDACTED:SECRET]")

    def test_cli_lookalikes_untouched(self):
        for line in ["please set password for bob",
                     "docker login --password-stdin",
                     "--password-file /run/pw"]:
            with self.subTest(line=line):
                self.assertEqual(r(line), line)

    def test_placeholder_inside_value_is_not_split(self):
        self.assertEqual(r("db_auth=postgres://u:p@h.example:5/x"), "db_auth=[REDACTED:SECRET]")

    def test_redacting_twice_changes_nothing(self):
        once = r(SAMPLE)
        self.assertEqual(r(once), once)


class UrlCredentials(unittest.TestCase):

    def test_hard_passwords(self):
        cases = {
            "redis://:s3cret@localhost:6379/0": "redis://[REDACTED:CREDENTIALS]@localhost:6379/0",
            "postgres://admin:pa/ss@db:5432/app": "postgres://[REDACTED:CREDENTIALS]@db:5432/app",
            "postgres://admin:p@ss@db": "postgres://[REDACTED:CREDENTIALS]@db",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)

    def test_port_and_path_are_not_a_password(self):
        line = "GET https://example.com:8080/@bob ok"
        self.assertEqual(r(line), line)


class PrivateKeyBlocks(unittest.TestCase):

    def test_second_begin_on_same_line(self):
        text = ("a -----BEGIN PRIVATE KEY-----x-----END PRIVATE KEY----- "
                "-----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEsecretbody\n"
                "-----END RSA PRIVATE KEY-----\n"
                "after\n")
        out, counts = redact(text)
        self.assertEqual(out, "a [REDACTED:PRIVATE_KEY] [REDACTED:PRIVATE_KEY]\nafter\n")
        self.assertEqual(counts["private_key"], 2)

    def test_back_to_back_keys(self):
        text = ("-----BEGIN RSA PRIVATE KEY-----\n"
                "MIIEbody1\n"
                "-----END RSA PRIVATE KEY----------BEGIN RSA PRIVATE KEY-----\n"
                "MIIEbody2secret\n"
                "-----END RSA PRIVATE KEY-----\n")
        self.assertEqual(r(text), "[REDACTED:PRIVATE_KEY][REDACTED:PRIVATE_KEY]\n")

    def test_header_mentioned_in_prose_keeps_rest_of_file(self):
        text = "error: expected -----BEGIN PRIVATE KEY----- header\nline2 10.0.0.1\nline3 important\n"
        self.assertEqual(r(text), "error: expected -----BEGIN PRIVATE KEY----- header\n"
                                  "line2 [REDACTED:IPV4]\nline3 important\n")

    def test_cut_off_key_ends_at_first_log_line(self):
        text = ("key: -----BEGIN PRIVATE KEY-----\n"
                "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
                "2026-11-03 INFO next 10.0.0.1\n")
        self.assertEqual(r(text), "key: [REDACTED:PRIVATE_KEY]\n2026-11-03 INFO next [REDACTED:IPV4]\n")

    def test_cut_off_escaped_key_on_one_line(self):
        self.assertEqual(r('"k": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkq'),
                         '"k": "[REDACTED:PRIVATE_KEY]')


class AddressAfterKey(unittest.TestCase):

    def test_ipv6_and_mac_right_after_colon_key(self):
        self.assertEqual(r("addr:fe80::1 ip:2001:db8::1 hw:aa:bb:cc:dd:ee:ff"),
                         "addr:[REDACTED:IPV6] ip:[REDACTED:IPV6] hw:[REDACTED:MAC]")
        self.assertEqual(r("mac:aa:bb:cc:dd:ee:ff"), "mac:[REDACTED:MAC]")


class MorePaths(unittest.TestCase):

    def test_lowercase_windows_and_var_home(self):
        cases = {
            r"C:\\users\\bob\\AppData": r"C:\\users\\[REDACTED:USER]\\AppData",
            "c:/users/bob/x": "c:/users/[REDACTED:USER]/x",
            "/var/home/jane/x": "/var/home/[REDACTED:USER]/x",
            "/usr/home/jane/x": "/usr/home/[REDACTED:USER]/x",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(r(src), want)


class Pathological(unittest.TestCase):
    """Long runs that used to trigger quadratic backtracking."""

    def test_long_runs_stay_fast(self):
        n = 5000
        lines = ["auth " + "." * n, "token " + "a." * n, "pass " + "-." * n,
                 "x@y.io " + "%" * n, "card " + "1 " * n + "x", ":: " + "a-" * n,
                 "://" + "a." * n, "key " + "-" * n, "hw " + "a-" * n + ":"]
        for line in lines:
            with self.subTest(line=line[:12]):
                start = time.perf_counter()
                r(line)
                self.assertLess(time.perf_counter() - start, 0.5)


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

    def test_utf16_with_bom_is_decoded_and_kept(self):
        raw = "ip 10.0.0.1\r\n".encode("utf-16")          # BOM + native byte order
        self.log.write_bytes(raw)
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = (self.dir / "app.redacted.log").read_bytes()
        self.assertEqual(out[:2], raw[:2])
        self.assertEqual(out.decode("utf-16"), "ip [REDACTED:IPV4]\r\n")

    def test_refuses_compressed_input(self):
        self.log.write_bytes(gzip.compress(b"ip 10.0.0.1\n"))
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 1)
        self.assertIn("gzip", res.stderr)
        self.assertFalse((self.dir / "app.redacted.log").exists())

    def test_refuses_binary_or_bomless_utf16(self):
        self.log.write_bytes("ip 10.0.0.1\n".encode("utf-16-le"))
        res = self.run_cli("app.log")
        self.assertEqual(res.returncode, 1)
        self.assertFalse((self.dir / "app.redacted.log").exists())
        self.assertEqual(list(self.dir.iterdir()), [self.log])   # no temp file left

    def test_output_copies_input_mode(self):
        os.chmod(self.log, 0o640)
        self.run_cli("app.log")
        self.assertEqual((self.dir / "app.redacted.log").stat().st_mode & 0o777, 0o640)


if __name__ == "__main__":
    unittest.main()
