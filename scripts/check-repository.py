#!/usr/bin/env python3
"""Reject secret-like files and high-confidence credentials before publishing."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SELF = Path(__file__).resolve()
BANNED_NAMES = re.compile(
    r"(^|/)(\.env(?:\..+)?|[^/]+\.(?:pem|key|p12|pfx)|credentials\.json)$"
)
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(rb"\bsk-[A-Za-z0-9_-]{24,}\b"),
)


def repository_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / name.decode() for name in result.stdout.split(b"\0") if name]


def main() -> None:
    failures: list[str] = []
    for path in repository_files():
        relative = path.relative_to(ROOT).as_posix()
        if BANNED_NAMES.search(relative):
            failures.append(f"secret-like file name: {relative}")
            continue
        if path.resolve() == SELF or not path.is_file():
            continue
        content = path.read_bytes()
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            failures.append(f"high-confidence credential pattern: {relative}")

    if failures:
        raise SystemExit("Repository safety check failed:\n" + "\n".join(failures))

    print(f"Repository safety check passed ({len(repository_files())} files scanned).")


if __name__ == "__main__":
    main()

