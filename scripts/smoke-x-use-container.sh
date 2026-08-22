#!/usr/bin/env bash
set -euo pipefail

image="${1:-hermes-agent:ci}"
smoke_scripts="${PWD}/scripts"

if [[ ! -f "${smoke_scripts}/smoke-x-use-mcp.py" ]]; then
  echo "Run this smoke from the repository root" >&2
  exit 2
fi

docker run --rm \
  --network none \
  --mount "type=bind,source=${smoke_scripts},target=/smoke,readonly" \
  --entrypoint bash \
  "${image}" \
  -lc '
    set -euo pipefail
    export HERMES_BROWSER_NETWORK_MODE=assigned_proxy
    export HERMES_RESIDENTIAL_PROXY_ENABLED=true
    export HERMES_RESIDENTIAL_PROXY_PORT=8899
    export RESIDENTIAL_PROXY_URL=http://127.0.0.1:8899
    export HTTP_PROXY=${RESIDENTIAL_PROXY_URL}
    export HTTPS_PROXY=${RESIDENTIAL_PROXY_URL}
    export http_proxy=${RESIDENTIAL_PROXY_URL}
    export https_proxy=${RESIDENTIAL_PROXY_URL}
    export NO_PROXY=127.0.0.1,localhost,::1
    export no_proxy=127.0.0.1,localhost,::1
    export HERMES_SOCIAL_ACCOUNTS_PATH=/tmp/hermes-secrets/social-accounts.json

    direct_log=/tmp/x-use-direct-mode.log
    if HERMES_BROWSER_NETWORK_MODE=direct \
      /usr/local/bin/hermes-x-use-mcp >"${direct_log}" 2>&1; then
      echo "x-use MCP unexpectedly started in direct mode" >&2
      exit 1
    fi
    grep -q "assigned proxy route" "${direct_log}"
    echo "{\"status\":\"ok\",\"direct_mode_rejected\":true}"

    /usr/local/bin/hermes-browser-supervisor &
    browser_supervisor_pid=$!
    cleanup() {
      kill "${browser_supervisor_pid}" 2>/dev/null || true
      wait "${browser_supervisor_pid}" 2>/dev/null || true
    }
    trap cleanup EXIT

    browser_ready=false
    for _ in $(seq 1 300); do
      if curl --fail --silent http://127.0.0.1:9222/json/version >/dev/null; then
        browser_ready=true
        break
      fi
      if ! kill -0 "${browser_supervisor_pid}" 2>/dev/null; then
        echo "Browser supervisor exited during smoke startup" >&2
        exit 1
      fi
      sleep 0.1
    done
    [[ "${browser_ready}" == true ]]

    /opt/x-use/.venv/bin/python /smoke/smoke-x-use-cdp.py
    /opt/x-use/.venv/bin/python /smoke/smoke-x-use-cookie-persistence.py
    /opt/x-use/.venv/bin/python /smoke/smoke-x-use-mcp.py
    /opt/hermes/.venv/bin/python /smoke/smoke-hermes-native-x-use.py --configure
    /usr/local/bin/hermes-x-use-native-preflight
    /opt/hermes/.venv/bin/python -c "
import json
from pathlib import Path
marker = json.loads(Path(\"/tmp/hermes-x-use/native-mcp-ready.json\").read_text())
assert marker == {
    \"commit\": \"e57e215e45b3e68cbd8cd7c46799cd932c234eac\",
    \"tool_count\": 13,
    \"version\": \"2.4.1\",
}
"

    test ! -e /usr/local/bin/social-account
    /opt/x-use/.venv/bin/python -c "
import json
import importlib.util
from importlib.metadata import distributions
from pathlib import Path
from packaging.utils import canonicalize_name
import sysconfig
import xuse
assert xuse.__version__ == \"2.4.1\"
assert importlib.util.find_spec(\"undetected_chromedriver\") is None
assert importlib.util.find_spec(\"selenium_stealth\") is None
locked = {}
for line in Path(\"/opt/x-use/requirements.lock\").read_text().splitlines():
    if line and not line.startswith(\"#\") and \"==\" in line:
        name, version = line.split(\"==\", 1)
        locked[canonicalize_name(name)] = version
actual = {
    canonicalize_name(dist.metadata[\"Name\"]): dist.version
    for dist in distributions(
        path=list(
            {
                sysconfig.get_paths()[\"purelib\"],
                sysconfig.get_paths()[\"platlib\"],
            }
        )
    )
}
expected = {**locked, \"x-use-mcp\": \"2.4.1\"}
assert actual == expected, {
    \"missing\": sorted(set(expected) - set(actual)),
    \"unexpected\": sorted(set(actual) - set(expected)),
    \"wrong_versions\": sorted(
        name for name in set(actual) & set(expected) if actual[name] != expected[name]
    ),
}
assert Path(\"/licenses/x-use-LICENSE\").is_file()
assert Path(\"/licenses/hermes-agent-LICENSE\").is_file()
print(json.dumps({
    \"status\": \"ok\",
    \"x_use_version\": xuse.__version__,
    \"locked_distribution_count\": len(locked),
    \"license_files_present\": True,
    \"undetected_chromedriver\": False,
    \"selenium_stealth\": False,
    \"legacy_helper_installed\": False,
}, sort_keys=True))
"
  '
