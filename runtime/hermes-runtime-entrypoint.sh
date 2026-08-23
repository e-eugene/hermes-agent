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

HERMES_BROWSER_GATEWAY_PORT="${HERMES_BROWSER_GATEWAY_PORT:-6081}"
export HERMES_BROWSER_GATEWAY_PORT
if ! [[ "${HERMES_BROWSER_GATEWAY_PORT}" =~ ^[1-9][0-9]{0,4}$ ]] || (( HERMES_BROWSER_GATEWAY_PORT > 65535 )); then
  echo "HERMES_BROWSER_GATEWAY_PORT must be a TCP port between 1 and 65535" >&2
  exit 1
fi

# The dashboard always sends an explicit mode.  Derive one for older callers
# so upgrading the public image does not turn a previously direct browser into
# a proxy-only runtime.  Once selected, no helper may silently change modes.
if [[ -z "${HERMES_BROWSER_NETWORK_MODE:-}" ]]; then
  if [[ "${HERMES_RESIDENTIAL_PROXY_ENABLED:-false}" == "true" ]]; then
    HERMES_BROWSER_NETWORK_MODE="assigned_proxy"
  else
    HERMES_BROWSER_NETWORK_MODE="direct"
  fi
fi
export HERMES_BROWSER_NETWORK_MODE

# Never inherit a platform/user proxy into x-use. Assigned mode explicitly
# gives only the no-auth loopback bridge to the MCP child below; direct mode
# gets no HTTP proxy at all.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY \
  http_proxy https_proxy all_proxy no_proxy

case "${HERMES_BROWSER_NETWORK_MODE}" in
  assigned_proxy)
    if [[ "${HERMES_RESIDENTIAL_PROXY_ENABLED:-false}" != "true" ]]; then
      echo "assigned_proxy browser mode requires HERMES_RESIDENTIAL_PROXY_ENABLED=true" >&2
      exit 1
    fi
    for variable in \
      HERMES_RESIDENTIAL_PROXY_HOST \
      HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT \
      HERMES_RESIDENTIAL_PROXY_BASE_USERNAME \
      HERMES_RESIDENTIAL_PROXY_PASSWORD \
      HERMES_RESIDENTIAL_PROXY_COUNTRY \
      HERMES_RESIDENTIAL_PROXY_CITY; do
      if [[ -z "${!variable:-}" ]]; then
        echo "assigned_proxy browser mode requires ${variable}" >&2
        exit 1
      fi
    done
    ;;
  direct)
    # Direct mode is an explicit dashboard action. Remove any inherited proxy
    # credentials before starting children so there is no hidden per-request
    # proxy bypass or accidental credential exposure in the direct runtime.
    unset HERMES_RESIDENTIAL_PROXY_ENABLED \
      HERMES_RESIDENTIAL_PROXY_HOST \
      HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT \
      HERMES_RESIDENTIAL_PROXY_BASE_USERNAME \
      HERMES_RESIDENTIAL_PROXY_PASSWORD \
      HERMES_RESIDENTIAL_PROXY_COUNTRY \
      HERMES_RESIDENTIAL_PROXY_CITY \
      HERMES_RESIDENTIAL_PROXY_PORT \
      HERMES_RESIDENTIAL_PROXY_STATE_PATH \
      RESIDENTIAL_PROXY_URL
    ;;
  *)
    echo "HERMES_BROWSER_NETWORK_MODE must be assigned_proxy or direct" >&2
    exit 1
    ;;
esac

mkdir -p \
  /opt/data/browser \
  /opt/data/workspace \
  /opt/data/x-use/drafts \
  /opt/data/x-use/media \
  /opt/data/x-use/metrics/data \
  /opt/data/x-use/metrics/logs \
  /tmp/hermes-secrets \
  /tmp/hermes-x-use/config
chmod 0700 /tmp/hermes-secrets
chmod 0700 \
  /opt/data/x-use \
  /opt/data/x-use/drafts \
  /opt/data/x-use/media \
  /opt/data/x-use/metrics \
  /opt/data/x-use/metrics/data \
  /opt/data/x-use/metrics/logs \
  /tmp/hermes-x-use \
  /tmp/hermes-x-use/config
rm -f /tmp/hermes-x-use/native-mcp-ready.json

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
  for pid in "${health_pid:-}" "${hermes_gateway_pid:-}" "${browser_gateway_pid:-}" "${vnc_pid:-}" "${api_bridge_v4_pid:-}" "${tui_bridge_v4_pid:-}" "${tui_bridge_v6_pid:-}" "${hermes_serve_pid:-}" "${residential_proxy_pid:-}" "${supervisor_pid:-}"; do
    if [[ -n "${pid}" ]]; then kill "${pid}" 2>/dev/null || true; fi
  done
}
trap cleanup EXIT
trap 'exit 0' INT TERM

if [[ "${HERMES_BROWSER_NETWORK_MODE}" == "assigned_proxy" ]]; then
  /usr/local/bin/residential-proxy serve >>/opt/data/browser/residential-proxy.log 2>&1 &
  residential_proxy_pid=$!

  proxy_bridge_ready=false
  for _ in $(seq 1 120); do
    if (echo >/dev/tcp/127.0.0.1/${HERMES_RESIDENTIAL_PROXY_PORT:-8899}) >/dev/null 2>&1; then
      proxy_bridge_ready=true
      break
    fi
    if ! kill -0 "${residential_proxy_pid}" 2>/dev/null; then
      echo "Residential proxy bridge exited before becoming ready" >&2
      exit 1
    fi
    sleep 0.25
  done
  [[ "${proxy_bridge_ready}" == "true" ]]

  # The bridge inherited the upstream configuration at fork and keeps it for
  # each client connection. Its children only need the loopback URL/port, so
  # discard upstream endpoint and credential values before starting Chromium,
  # Hermes, the VNC gateway and all later helper subprocesses.
  RESIDENTIAL_PROXY_URL="http://127.0.0.1:${HERMES_RESIDENTIAL_PROXY_PORT:-8899}"
  export RESIDENTIAL_PROXY_URL
  unset HERMES_RESIDENTIAL_PROXY_HOST \
    HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT \
    HERMES_RESIDENTIAL_PROXY_BASE_USERNAME \
    HERMES_RESIDENTIAL_PROXY_PASSWORD \
    HERMES_RESIDENTIAL_PROXY_COUNTRY \
    HERMES_RESIDENTIAL_PROXY_CITY \
    HERMES_RESIDENTIAL_PROXY_STATE_PATH

  # Generate one non-secret x-use account/config only after the no-auth
  # loopback proxy bridge is ready. Session cookies are imported later through
  # the private control API and are never written here.
  /usr/local/bin/hermes-x-use-configure >/dev/null
fi

/usr/local/bin/hermes-browser-supervisor >>/opt/data/browser/supervisor.log 2>&1 &
supervisor_pid=$!
/usr/local/bin/wait-for-hermes-browser 60

DISPLAY="${DISPLAY:-:99}" x11vnc \
  -display "${DISPLAY:-:99}" \
  -rfbport 5900 \
  -localhost \
  -nopw \
  -forever \
  -shared \
  -noclipboard \
  -nosetclipboard \
  -noxrecord \
  >>/opt/data/browser/x11vnc.log 2>&1 &
vnc_pid=$!

vnc_ready=false
for _ in $(seq 1 120); do
  if (echo >/dev/tcp/127.0.0.1/5900) >/dev/null 2>&1; then
    vnc_ready=true
    break
  fi
  if ! kill -0 "${vnc_pid}" 2>/dev/null; then
    echo "x11vnc exited before becoming ready" >&2
    exit 1
  fi
  sleep 0.25
done
[[ "${vnc_ready}" == "true" ]]

/usr/local/bin/hermes-browser-gateway >>/opt/data/browser/browser-gateway.log 2>&1 &
browser_gateway_pid=$!

hermes=/opt/hermes/bin/hermes
"${hermes}" config set model.provider "${HERMES_PRESET_PROVIDER}"
"${hermes}" config set model.default "${HERMES_MODEL}"
"${hermes}" config set terminal.cwd "/opt/data/workspace"
# This config is persistent under /opt/data. Always write the current context,
# including an empty value, so an unallocated account cannot leave stale model
# guidance after a runtime is redeployed.
"${hermes}" config set agent.system_prompt "${HERMES_RUNTIME_CONTEXT:-}"
unset HERMES_RUNTIME_CONTEXT
if [[ -n "${HERMES_PRESET_BASE_URL:-}" ]]; then
  "${hermes}" config set model.base_url "${HERMES_PRESET_BASE_URL}"
fi

/opt/hermes/.venv/bin/python -c '
import os
from pathlib import Path
from urllib.parse import urlsplit
import yaml

path = Path("/opt/data/config.yaml")
config = yaml.safe_load(path.read_text()) or {}
config.setdefault("platform_toolsets", {})["api_server"] = [
    "terminal",
    "browser",
    "x_use",
]
browser = config.setdefault("browser", {})
browser["headed"] = True
browser["cdp_url"] = "http://127.0.0.1:9222"
allowed_x_use_tools = [
    "list_accounts",
    "get_account",
    "get_account_health",
    "get_metrics",
    "search_tweets",
    "search_profile",
    "get_tweet",
    "prepare_reply",
    "like_tweet",
    "post_tweet",
    "reply_to_tweet",
    "list_drafts",
    "get_draft",
    "reject_draft",
]
direct_posting_enabled = (
    os.environ.get("HERMES_X_DIRECT_POSTING_ENABLED", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
mcp_env = {
    "NO_PROXY": "127.0.0.1,localhost,::1",
    "no_proxy": "127.0.0.1,localhost,::1",
    # Hermes launches MCP servers with this explicit environment instead of
    # inheriting the runtime container environment. Keep the administrator-
    # controlled direct-posting policy visible to the isolated x-use process.
    "HERMES_X_DIRECT_POSTING_ENABLED": (
        "true" if direct_posting_enabled else "false"
    ),
}
proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL")
network_mode = os.environ.get("HERMES_BROWSER_NETWORK_MODE")
if network_mode == "assigned_proxy":
    if not proxy_url:
        raise SystemExit("x-use requires the no-auth loopback proxy bridge")
    parsed_proxy = urlsplit(proxy_url)
    if (
        parsed_proxy.scheme != "http"
        or parsed_proxy.hostname != "127.0.0.1"
        or parsed_proxy.port is None
        or parsed_proxy.username is not None
        or parsed_proxy.password is not None
        or parsed_proxy.path not in {"", "/"}
        or parsed_proxy.query
        or parsed_proxy.fragment
    ):
        raise SystemExit("RESIDENTIAL_PROXY_URL must be a no-auth loopback URL")
    mcp_env.update({
        "HERMES_BROWSER_NETWORK_MODE": "assigned_proxy",
        "HERMES_RESIDENTIAL_PROXY_PORT": str(parsed_proxy.port),
        "RESIDENTIAL_PROXY_URL": proxy_url,
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
    })
mcp_servers = config.setdefault("mcp_servers", {})
if network_mode == "assigned_proxy":
    mcp_servers["x_use"] = {
        "command": "/usr/local/bin/hermes-x-use-mcp",
        "args": [],
        "env": mcp_env,
        "connect_timeout": 90,
        "timeout": 150,
        "tools": {
            "include": allowed_x_use_tools,
            "prompts": False,
            "resources": False,
        },
    }
else:
    # Config is persistent: remove a stale x-use server after a deployment is
    # explicitly switched to direct mode.
    mcp_servers.pop("x_use", None)
temporary = path.with_suffix(".yaml.tmp")
temporary.write_text(yaml.safe_dump(config, sort_keys=False))
os.chmod(temporary, 0o600)
os.replace(temporary, path)
'

if [[ "${HERMES_BROWSER_NETWORK_MODE}" == "assigned_proxy" ]]; then
  # Exercise installed Hermes discovery, then expose readiness only after it
  # sees exactly the curated x-use schemas and shuts the MCP child down.
  /usr/local/bin/hermes-x-use-native-preflight \
    >>/opt/data/browser/x-use-native-preflight.log 2>&1
fi

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
hermes_gateway_pid=$!
wait "${hermes_gateway_pid}"
