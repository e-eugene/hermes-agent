# Hermes Agent Browser Runtime

Public container image for running an isolated
[Hermes Agent](https://github.com/NousResearch/hermes-agent) with headed
Chromium, authenticated private service endpoints, optional residential proxy
support, and runtime-provided social-account credentials.

The image contains runtime code only. It does not contain an admin application,
deployment credentials, LLM keys, proxy credentials, or social-account
credentials.

## Image

Images are published to GitHub Container Registry:

```text
ghcr.io/e-eugene/hermes-agent:<git-sha>
ghcr.io/e-eugene/hermes-agent:staging
```

Deployments must pin the full 40-character commit SHA. The `staging` tag is a
convenience pointer and is not an immutable deployment reference.

After the package is published for the first time, its GHCR visibility must be
set to **Public** in the package settings. Repository visibility alone does not
make a new container package public.

## Runtime layout

- `/opt/data` is the persistent Hermes data directory and should be mounted as
  a dedicated volume for each agent.
- `/opt/data/browser-profile` stores the persistent Chromium profile.
- `/tmp/hermes-secrets/social-accounts.json` is recreated on each start with
  mode `0600`; the containing directory has mode `0700`.
- Chromium CDP listens only on loopback port `9222` inside the container.

The image exposes these private-network ports:

| Port | Purpose | Authentication |
| --- | --- | --- |
| `8642` | Hermes API server | `Authorization: Bearer <API_SERVER_KEY>` |
| `8643` | Runtime control: readiness, X account status/login, browser network status | `Authorization: Bearer <API_SERVER_KEY>` |
| `6081` | Binary WebSocket bridge to the loopback Remote Chromium VNC server | `Authorization: Bearer <API_SERVER_KEY>` |
| `9120` | Dual-stack Hermes TUI and WebSocket bridge | Hermes bearer/session token |

Do not assign public domains to these ports. Place the runtime on a private
service network and let a trusted backend or orchestrator communicate with it.
Hermes TUI itself stays on container loopback; `socat` exposes its authenticated
HTTP/WebSocket traffic on the dual-stack bridge because upstream Hermes refuses
direct non-loopback dashboard binds without a registered auth provider.
Hermes API binds IPv6 directly and an internal `socat` listener forwards IPv4
connections to it, so private clients can use either Railway address family.

### Private runtime control API

The service on `8643` is private-only and requires `Authorization: Bearer
<API_SERVER_KEY>`. It exposes a small redacted contract for the dashboard:

- `GET /health` returns runtime readiness plus
  `capabilities: ["x_social_account", "x_manual_auth",
  "persistent_browser_profile", "remote_chromium", "network_status"]`
  and the last browser-network snapshot in `network`.
- `GET /network/status` makes a one-off request from the actual headed Chromium
  CDP browser to a fixed public IP-echo endpoint. It returns only
  `{status: "healthy"|"unhealthy"|"unavailable", mode,
  exit_ip?, error?}`; no proxy configuration is exposed.
- `POST /social-accounts/x/status` and
  `POST /social-accounts/x/login` return the assigned handle and one of
  `authenticated`, `not_logged_in`, `wrong_account`, `manual_attention`, or a
  redacted error. Login is an explicit manual handoff: the helper verifies the
  current identity, then opens and focuses the persistent X login tab for the
  operator in Remote Chromium. It never reads, fills, or submits the allocated
  password. CAPTCHA, 2FA and an already signed-in different account are never
  bypassed automatically.

Remote Chromium connects through a single-controller, binary WebSocket bridge
on `6081`. It forwards only to an `x11vnc` process bound to container loopback;
the browser profile, CDP and VNC ports are never public.

## Environment contract

The runtime entrypoint expects the following core variables:

| Variable | Required | Description |
| --- | --- | --- |
| `HERMES_PRESET_PROVIDER` | yes | Hermes provider identifier, for example `openai-api`, `openrouter`, `anthropic`, `nous-api`, or `custom`. |
| `HERMES_MODEL` | yes | Model name written to the Hermes configuration. |
| `HERMES_PRESET_BASE_URL` | no | Optional provider base URL. |
| `API_SERVER_ENABLED` | yes | Set to `true`. |
| `API_SERVER_HOST` | yes | Set to `::` for private dual-stack networking. |
| `API_SERVER_PORT` | yes | Set to `8642` to match the image contract. |
| `API_SERVER_KEY` | yes | Per-agent bearer token used by the API and readiness endpoint. |
| `API_SERVER_MODEL_NAME` | yes | Public model/agent identifier returned by Hermes. |
| `HERMES_DASHBOARD_SESSION_TOKEN` | yes | TUI session token; use the same per-agent value as `API_SERVER_KEY`. |
| `HERMES_TUI_PORT` | yes | Internal Hermes TUI port, normally `9119`. |
| `HERMES_TUI_BRIDGE_PORT` | yes | Private-network TUI bridge port, normally `9120`. |
| `HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS` | yes | TUI orphan WebSocket grace period, normally `120`. |
| `HERMES_SOCIAL_ACCOUNTS_PATH` | yes | Use `/tmp/hermes-secrets/social-accounts.json`. |
| `HERMES_SOCIAL_ACCOUNTS_JSON` | no | JSON array of assigned accounts; defaults to `[]`. |
| `HERMES_RUNTIME_CONTEXT` | no | Dashboard-generated, non-secret system guidance. It is written deterministically to `agent.system_prompt` on each boot and cleared when empty. |

Provider credentials are passed using the variables understood by Hermes:

| Provider | Credential | Optional base URL |
| --- | --- | --- |
| OpenAI/custom | `OPENAI_API_KEY` | `OPENAI_BASE_URL` |
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` |
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |
| Nous Portal | `NOUS_API_KEY` | `NOUS_BASE_URL` |

Browser settings have safe image defaults:

| Variable | Default | Description |
| --- | --- | --- |
| `DISPLAY` | `:99` | Xvfb display. |
| `HERMES_BROWSER_SCREEN` | `1440x900x24` | Virtual screen dimensions and color depth. |
| `HERMES_BROWSER_PROFILE_DIR` | `/opt/data/browser-profile` | Persistent Chromium profile. |
| `HERMES_BROWSER_STATE_DIR` | `/opt/data/browser` | Browser and supervisor logs. |
| `BROWSER_CDP_URL` | `http://127.0.0.1:9222` | Hermes browser tool CDP endpoint. |
| `HERMES_BROWSER_GATEWAY_PORT` | `6081` | Private authenticated Remote Chromium WebSocket port. |
| `HERMES_BROWSER_NETWORK_MODE` | Derived for legacy callers | `assigned_proxy` starts Chromium through the allocated bridge; `direct` starts Chromium without proxy credentials. Dashboard deployments must set this explicitly. |

### Residential proxy

Set `HERMES_BROWSER_NETWORK_MODE=assigned_proxy` and
`HERMES_RESIDENTIAL_PROXY_ENABLED=true` to route Chromium through the local
authenticated proxy bridge. These variables then become required:

- `HERMES_RESIDENTIAL_PROXY_HOST`
- `HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT`
- `HERMES_RESIDENTIAL_PROXY_BASE_USERNAME`
- `HERMES_RESIDENTIAL_PROXY_PASSWORD`
- `HERMES_RESIDENTIAL_PROXY_COUNTRY`
- `HERMES_RESIDENTIAL_PROXY_CITY`

Optional proxy settings are `HERMES_RESIDENTIAL_PROXY_PORT` (default `8899`)
and `HERMES_RESIDENTIAL_PROXY_STATE_PATH` (default
`/opt/data/residential-proxy/state.json`). The upstream username is constructed
as `<base>-<country>-city_<city>-<session>`, matching the expected residential
proxy account format.

`HERMES_BROWSER_NETWORK_MODE=direct` is an explicit operational choice. The
entrypoint removes all inherited proxy variables before child processes start,
so Chromium, browser tools, Remote Chromium and social-account actions are all
visibly direct for that deployment. There is no request-level direct fallback
while in `assigned_proxy` mode.

The `residential-proxy` command inside the container supports `status`, `url`,
`sticky [session-id]`, and `rotate`. It never prints upstream credentials.
Social-account status and login only verify that the selected browser route and
local proxy bridge are ready. They never invoke these mutating proxy commands,
so checking authentication cannot rotate the browser's sticky session.

### Social accounts

`HERMES_SOCIAL_ACCOUNTS_JSON` accepts an array such as:

```json
[
  {
    "id": "account-id",
    "type": "reddit",
    "login": "runtime-login",
    "password": "runtime-password",
    "is_active": true
  }
]
```

This is a runtime interface example only; never commit real values. On startup,
the entrypoint writes the array to the configured temporary path, applies mode
`0600`, atomically replaces any old file, and unsets the JSON variable in the
entrypoint process. The `social-account` command exposes only redacted status.
It supports existing Reddit login plus X commands used by Hermes and the
dashboard:

```sh
social-account status x
social-account login x
```

The X helper uses the same persistent headed Chromium profile and selected
browser network mode as Hermes Browser tools. `social-account login x` does not
require or read a password. It checks the assigned identity in a separate
temporary CDP target, closes that target without disturbing the operator's
current page, and then opens or focuses an X login/challenge tab in the visible
persistent browser. Existing non-auth X tabs are never navigated or replaced;
if no login/challenge tab exists, the helper opens a new foreground tab at
`https://x.com/i/flow/login`. The operator completes username, password,
CAPTCHA, 2FA, or identity checks directly in Remote Chromium and then runs
`social-account status x` again.

`social-account status x` also performs its identity check in a separate
temporary target and always closes it, so polling cannot replace an in-progress
operator challenge. It waits for the hydrated X application and accepts only a
strictly validated handle from X's authenticated profile navigation or account
switcher. A login/onboarding page is positive evidence of a logged-out session;
an unknown or incomplete page returns `manual_attention` instead of being
misclassified as logged out. A successful session is stored in
`/opt/data/browser-profile` and survives container restarts and image
redeployments as long as the per-agent volume is preserved. X may still revoke
cookies or require a later re-verification. The helper never logs out a
different account, clears cookies, takes attention screenshots, or attempts to
solve a challenge automatically; those states return `manual_attention` or
`wrong_account` for operator resolution.

## Secret handling

- Supply all LLM, proxy, API, and social credentials at container runtime.
- Use a distinct `API_SERVER_KEY` for every agent.
- Configure variables before the first deployment so a partially configured
  container is never started.
- Do not place secrets in image build arguments, labels, tags, logs, or source.
- Treat the container environment and its private volume as sensitive data.
- Rotate credentials in the orchestrator and redeploy the affected agent when
  assignments change.

## Build and test

Build locally from the repository root:

```sh
docker build -t hermes-agent:local .
```

Override the upstream Hermes version only when intentionally testing an update:

```sh
docker build \
  --build-arg HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.7.20 \
  -t hermes-agent:local .
```

Run the helper and repository checks:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
bash -n runtime/*.sh
python3 scripts/check-repository.py
```

## Publishing

`.github/workflows/publish.yml` runs on pushes to `main` and on manual
dispatch. It verifies the runtime, logs in to GHCR using the repository-scoped
`GITHUB_TOKEN`, and publishes both the immutable commit SHA and `staging` tags.

Production and staging consumers should record the immutable image reference in
their own configuration. Runtime secrets remain owned by the consumer and are
never published with the image.
