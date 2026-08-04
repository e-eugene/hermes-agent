#!/usr/bin/env bash
set -euo pipefail

required_variables=(
  API_SERVER_ENABLED
  API_SERVER_HOST
  API_SERVER_PORT
  API_SERVER_KEY
  API_SERVER_MODEL_NAME
  HERMES_DASHBOARD_SESSION_TOKEN
  HERMES_PRESET_PROVIDER
  HERMES_MODEL
  HERMES_TUI_PORT
  HERMES_TUI_BRIDGE_PORT
  HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS
  HERMES_SOCIAL_ACCOUNTS_PATH
)

for variable in "${required_variables[@]}"; do
  if [[ -z "${!variable:-}" ]]; then
    echo "Required runtime variable is missing or empty: ${variable}" >&2
    exit 1
  fi
done

if [[ "${API_SERVER_ENABLED}" != "true" ]]; then
  echo "API_SERVER_ENABLED must be true" >&2
  exit 1
fi

if [[ "${API_SERVER_HOST}" != "::" ]]; then
  echo "API_SERVER_HOST must be :: for private dual-stack networking" >&2
  exit 1
fi

if [[ "${API_SERVER_KEY}" != "${HERMES_DASHBOARD_SESSION_TOKEN}" ]]; then
  echo "API_SERVER_KEY and HERMES_DASHBOARD_SESSION_TOKEN must match" >&2
  exit 1
fi

for variable in API_SERVER_PORT HERMES_TUI_PORT HERMES_TUI_BRIDGE_PORT; do
  value="${!variable}"
  if ! [[ "${value}" =~ ^[1-9][0-9]{0,4}$ ]] || (( value > 65535 )); then
    echo "${variable} must be a TCP port between 1 and 65535" >&2
    exit 1
  fi
done

if [[ "${API_SERVER_PORT}" != "8642" ]]; then
  echo "API_SERVER_PORT must be 8642" >&2
  exit 1
fi

if ! [[ "${HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS must be a non-negative integer" >&2
  exit 1
fi

mkdir -p /opt/data/browser /opt/data/workspace /tmp/hermes-secrets
chmod 0700 /tmp/hermes-secrets

/opt/hermes/.venv/bin/python -c '
import json
import os
from pathlib import Path

path = Path(os.environ["HERMES_SOCIAL_ACCOUNTS_PATH"])
accounts = json.loads(os.environ.get("HERMES_SOCIAL_ACCOUNTS_JSON", "[]"))
temporary = path.with_suffix(".json.tmp")
temporary.write_text(json.dumps(accounts))
os.chmod(temporary, 0o600)
os.replace(temporary, path)
'
unset HERMES_SOCIAL_ACCOUNTS_JSON

cleanup() {
  for pid in "${health_pid:-}" "${gateway_pid:-}" "${api_bridge_v4_pid:-}" "${tui_bridge_v4_pid:-}" "${tui_bridge_v6_pid:-}" "${hermes_serve_pid:-}" "${residential_proxy_pid:-}" "${supervisor_pid:-}"; do
    if [[ -n "${pid}" ]]; then kill "${pid}" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 0' INT TERM

if [[ "${HERMES_RESIDENTIAL_PROXY_ENABLED:-false}" == "true" ]]; then
  /usr/local/bin/residential-proxy serve >>/opt/data/browser/residential-proxy.log 2>&1 &
  residential_proxy_pid=$!
fi

/usr/local/bin/hermes-browser-supervisor >>/opt/data/browser/supervisor.log 2>&1 &
supervisor_pid=$!
/usr/local/bin/wait-for-hermes-browser 60

hermes=/opt/hermes/bin/hermes
"${hermes}" config set model.provider "${HERMES_PRESET_PROVIDER}"
"${hermes}" config set model.default "${HERMES_MODEL}"
"${hermes}" config set terminal.cwd "/opt/data/workspace"
if [[ -n "${HERMES_PRESET_BASE_URL:-}" ]]; then
  "${hermes}" config set model.base_url "${HERMES_PRESET_BASE_URL}"
fi

/opt/hermes/.venv/bin/python -c '
import os
from pathlib import Path
import yaml

path = Path("/opt/data/config.yaml")
config = yaml.safe_load(path.read_text()) or {}
config.setdefault("platform_toolsets", {})["api_server"] = ["terminal", "browser"]
browser = config.setdefault("browser", {})
browser["headed"] = True
browser["cdp_url"] = "http://127.0.0.1:9222"
temporary = path.with_suffix(".yaml.tmp")
temporary.write_text(yaml.safe_dump(config, sort_keys=False))
os.chmod(temporary, 0o600)
os.replace(temporary, path)
'

HERMES_TUI_WS_ORPHAN_REAP_GRACE_S="${HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS}" \
  "${hermes}" serve --host 127.0.0.1 --port "${HERMES_TUI_PORT}" --skip-build \
  >>/opt/data/browser/hermes-serve.log 2>&1 &
hermes_serve_pid=$!

hermes_tui_ready=false
for _ in $(seq 1 120); do
  if curl --fail --silent --show-error \
    -H "Host: localhost:${HERMES_TUI_PORT}" \
    -H "Authorization: Bearer ${HERMES_DASHBOARD_SESSION_TOKEN}" \
    "http://127.0.0.1:${HERMES_TUI_PORT}/api/status" >/dev/null 2>&1; then
    hermes_tui_ready=true
    break
  fi
  if ! kill -0 "${hermes_serve_pid}" 2>/dev/null; then
    echo "Hermes serve exited before becoming ready" >&2
    exit 1
  fi
  sleep 0.25
done
[[ "${hermes_tui_ready}" == "true" ]]

socat "TCP4-LISTEN:${HERMES_TUI_BRIDGE_PORT},fork,reuseaddr" "TCP:127.0.0.1:${HERMES_TUI_PORT}" >>/opt/data/browser/hermes-serve-bridge.log 2>&1 &
tui_bridge_v4_pid=$!
socat "TCP6-LISTEN:${HERMES_TUI_BRIDGE_PORT},fork,reuseaddr,ipv6only=1" "TCP:127.0.0.1:${HERMES_TUI_PORT}" >>/opt/data/browser/hermes-serve-bridge.log 2>&1 &
tui_bridge_v6_pid=$!

# Hermes API binds an IPv6-only socket for API_SERVER_HOST=::. Add the IPv4
# listener explicitly so Railway private DNS clients can use either address family.
socat "TCP4-LISTEN:${API_SERVER_PORT},fork,reuseaddr" "TCP6:[::1]:${API_SERVER_PORT}" >>/opt/data/browser/hermes-api-bridge.log 2>&1 &
api_bridge_v4_pid=$!

/usr/local/bin/hermes-runtime-health >>/opt/data/browser/runtime-health.log 2>&1 &
health_pid=$!

"${hermes}" gateway run --replace &
gateway_pid=$!
wait "${gateway_pid}"
