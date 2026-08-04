#!/usr/bin/env bash
set -euo pipefail

browser_display="${DISPLAY:-:99}"
browser_screen="${HERMES_BROWSER_SCREEN:-1440x900x24}"
browser_profile="${HERMES_BROWSER_PROFILE_DIR:-/opt/data/browser-profile}"
browser_state="${HERMES_BROWSER_STATE_DIR:-/opt/data/browser}"
browser_proxy_args=()

if [[ "${HERMES_RESIDENTIAL_PROXY_ENABLED:-false}" == "true" ]]; then
  browser_proxy_args=(
    "--proxy-server=http://127.0.0.1:${HERMES_RESIDENTIAL_PROXY_PORT:-8899}"
    "--proxy-bypass-list=<-loopback>"
  )
fi

mkdir -p "${browser_profile}" "${browser_state}"
chmod 0700 "${browser_profile}"
rm -f \
  "${browser_profile}/SingletonCookie" \
  "${browser_profile}/SingletonLock" \
  "${browser_profile}/SingletonSocket"

cleanup() {
  if [[ -n "${chromium_pid:-}" ]]; then
    kill "${chromium_pid}" 2>/dev/null || true
  fi
  if [[ -n "${openbox_pid:-}" ]]; then
    kill "${openbox_pid}" 2>/dev/null || true
  fi
  if [[ -n "${xvfb_pid:-}" ]]; then
    kill "${xvfb_pid}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

Xvfb "${browser_display}" \
  -screen 0 "${browser_screen}" \
  -nolisten tcp \
  -ac \
  >"${browser_state}/xvfb.log" 2>&1 &
xvfb_pid=$!

for _ in $(seq 1 100); do
  if DISPLAY="${browser_display}" xdpyinfo >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
    echo "Xvfb exited before the display became ready" >&2
    exit 1
  fi
  sleep 0.1
done

DISPLAY="${browser_display}" openbox-session \
  >"${browser_state}/openbox.log" 2>&1 &
openbox_pid=$!

while true; do
  DISPLAY="${browser_display}" chromium \
    --no-sandbox \
    --disable-dev-shm-usage \
    --no-first-run \
    --no-default-browser-check \
    --disable-component-update \
    --disable-features=Translate,MediaRouter \
    --remote-allow-origins='*' \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port=9222 \
    --user-data-dir="${browser_profile}" \
    --window-size=1440,900 \
    "${browser_proxy_args[@]}" \
    about:blank \
    >"${browser_state}/chromium.log" 2>&1 &
  chromium_pid=$!
  wait "${chromium_pid}" || true
  chromium_pid=""
  sleep 1
done
