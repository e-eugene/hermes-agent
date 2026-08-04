#!/usr/bin/env bash
set -euo pipefail

timeout_seconds="${1:-60}"
deadline=$((SECONDS + timeout_seconds))

while (( SECONDS < deadline )); do
  if curl --fail --silent --show-error \
    http://127.0.0.1:9222/json/version \
    >/dev/null 2>&1; then
    exit 0
  fi
  sleep 0.25
done

echo "Headful Chromium CDP endpoint did not become ready" >&2
exit 1
