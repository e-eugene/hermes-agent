#!/opt/x-use/.venv/bin/python
"""Generate ephemeral non-secret x-use configuration at runtime startup."""

from __future__ import annotations

import json
import os
import sys


sys.path.insert(0, "/opt/hermes-runtime")
os.umask(0o077)

from hermes_x_use_common import configure_runtime  # noqa: E402


def main() -> None:
    try:
        payload = configure_runtime()
    except Exception:
        print(json.dumps({"status": "error", "error": "x-use configuration failed"}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "ok", **payload}))


if __name__ == "__main__":
    main()
