#!/usr/bin/env python3
"""Reject common hardcoded credentials in added Git lines without printing values."""

from __future__ import annotations

import re
import subprocess
import sys

EXCLUDED = {"scripts/check-public-secrets.py"}
PLACEHOLDERS = re.compile(
    r"(?i)(?:example|placeholder|changeme|change-me|your[-_ ]|dummy|test[-_ ]only|redacted|<[^>]+>)"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
PROVIDER_TOKEN = re.compile(
    r"(?:\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b|\bxox[baprs]-[A-Za-z0-9-]{20,}\b|\bAIza[0-9A-Za-z_-]{30,}\b)"
)
HEX_SECRET = re.compile(r"(?i)(?<![a-f0-9])[a-f0-9]{40,}(?![a-f0-9])")
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?:^|[^A-Z0-9])(?:secret|token|api[_-]?key|password|credential|private[_-]?key)(?=$|[^A-Z0-9])"
)
EMAIL_ASSIGNMENT = re.compile(r"(?i)(?:^|[^A-Z0-9])(?:email|mailbox|allowed[_-]?email)(?=$|[^A-Z0-9])")
EMAIL_LITERAL = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")


def added_lines(base: str | None, head: str | None) -> list[tuple[str, int, str]]:
    diff_args = ["git", "diff", "--no-ext-diff", "--unified=0"]
    if base is not None and head is not None:
        diff_args.extend([base, head])
    else:
        diff_args.append("--cached")
    diff = subprocess.run(
        [*diff_args, "--"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    result: list[tuple[str, int, str]] = []
    path = ""
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@"):
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                new_line = int(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            result.append((path, new_line, line[1:]))
            new_line += 1
        elif line.startswith(" "):
            new_line += 1
    return result


def findings(lines: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
    hits = []
    for path, number, line in lines:
        if path in EXCLUDED:
            continue
        if PRIVATE_KEY.search(line):
            hits.append((path, number, "private-key material"))
            continue
        provider_token = PROVIDER_TOKEN.search(line)
        if provider_token and not PLACEHOLDERS.search(provider_token.group(0)):
            hits.append((path, number, "provider token pattern"))
            continue
        if SENSITIVE_ASSIGNMENT.search(line) and HEX_SECRET.search(line):
            hits.append((path, number, "long hexadecimal credential assignment"))
            continue
        if EMAIL_ASSIGNMENT.search(line) and EMAIL_LITERAL.search(line):
            literals = EMAIL_LITERAL.findall(line)
            if any(not PLACEHOLDERS.search(value) for value in literals):
                hits.append((path, number, "hardcoded email identity"))
    return hits


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--staged":
        base = head = None
    elif len(sys.argv) == 3:
        base, head = sys.argv[1:]
    else:
        print("usage: check-public-secrets.py BASE_COMMIT HEAD_COMMIT | --staged", file=sys.stderr)
        return 2
    try:
        hits = findings(added_lines(base, head))
    except subprocess.CalledProcessError:
        print("secret scan could not calculate the requested Git diff", file=sys.stderr)
        return 2
    for path, line, category in hits:
        print(f"::error file={path},line={line}::{category}; value redacted")
    if hits:
        return 1
    print("Public-change secret scan passed (added lines only; values not emitted).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
