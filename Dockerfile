FROM nousresearch/hermes-agent:v2026.7.20@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a

USER root

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-driver \
        curl \
        dbus-x11 \
        fonts-liberation \
        fonts-noto-color-emoji \
        fonts-noto-core \
        git \
        openbox \
        python3-venv \
        socat \
        x11vnc \
        x11-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/x-use-requirements.lock /opt/x-use/requirements.lock
COPY runtime/patch_x_use.py /usr/local/bin/patch_x_use.py

# x-use is intentionally isolated from Hermes' Python environment. The exact
# audited upstream revision is retained as an editable source install so its
# project-root-relative metrics paths remain deterministic. Our adapter never
# invokes undetected-chromedriver or webdriver-manager; it attaches the system
# chromedriver to the already-running persistent Chromium CDP endpoint.
RUN git clone https://github.com/ihuzaifashoukat/x-use.git /opt/x-use/source \
    && git -C /opt/x-use/source checkout --detach e57e215e45b3e68cbd8cd7c46799cd932c234eac \
    && test "$(git -C /opt/x-use/source rev-parse HEAD)" = e57e215e45b3e68cbd8cd7c46799cd932c234eac \
    && python3 /usr/local/bin/patch_x_use.py /opt/x-use/source \
    && python3 -m venv /opt/x-use/.venv \
    && /opt/x-use/.venv/bin/pip install --no-cache-dir --requirement /opt/x-use/requirements.lock \
    && /opt/x-use/.venv/bin/pip install --no-cache-dir --no-deps --no-build-isolation --editable /opt/x-use/source \
    && ! /opt/x-use/.venv/bin/python -c 'import undetected_chromedriver' \
    && ! /opt/x-use/.venv/bin/python -c 'import selenium_stealth' \
    && mkdir -p /licenses \
    && cp /opt/x-use/source/LICENSE /licenses/x-use-LICENSE \
    && rm -rf /opt/x-use/source/.git /opt/x-use/source/config /opt/x-use/source/data /opt/x-use/source/logs \
    && ln -s /tmp/hermes-x-use/config /opt/x-use/source/config \
    && ln -s /opt/data/x-use/metrics/data /opt/x-use/source/data \
    && ln -s /opt/data/x-use/metrics/logs /opt/x-use/source/logs

COPY LICENSE /licenses/hermes-agent-LICENSE

COPY runtime/hermes-browser-supervisor.sh /usr/local/bin/hermes-browser-supervisor
COPY runtime/wait-for-hermes-browser.sh /usr/local/bin/wait-for-hermes-browser
COPY runtime/residential-proxy.py /usr/local/bin/residential-proxy
COPY runtime/hermes-browser-gateway.py /usr/local/bin/hermes-browser-gateway
COPY runtime/hermes-browser-network-status.py /usr/local/bin/hermes-browser-network-status
COPY runtime/hermes-runtime-entrypoint.sh /usr/local/bin/hermes-runtime-entrypoint
COPY runtime/hermes-runtime-health.py /usr/local/bin/hermes-runtime-health
COPY runtime/hermes-x-use-mcp.py /usr/local/bin/hermes-x-use-mcp
COPY runtime/hermes-x-use-configure.py /usr/local/bin/hermes-x-use-configure
COPY runtime/hermes-x-use-native-preflight.py /usr/local/bin/hermes-x-use-native-preflight
COPY runtime/hermes_x_use_common.py /opt/hermes-runtime/hermes_x_use_common.py
COPY runtime/hermes_x_use_adapter.py /opt/hermes-runtime/hermes_x_use_adapter.py

RUN chmod 0755 \
    /usr/local/bin/hermes-browser-supervisor \
    /usr/local/bin/wait-for-hermes-browser \
    /usr/local/bin/residential-proxy \
    /usr/local/bin/hermes-browser-gateway \
    /usr/local/bin/hermes-browser-network-status \
    /usr/local/bin/hermes-runtime-entrypoint \
    /usr/local/bin/hermes-runtime-health \
    /usr/local/bin/hermes-x-use-mcp \
    /usr/local/bin/hermes-x-use-configure \
    /usr/local/bin/hermes-x-use-native-preflight \
    && chmod 0644 \
      /usr/local/bin/patch_x_use.py \
      /opt/hermes-runtime/hermes_x_use_common.py \
      /opt/hermes-runtime/hermes_x_use_adapter.py

ENV DISPLAY=:99 \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    AGENT_BROWSER_HEADED=true \
    BROWSER_CDP_URL=http://127.0.0.1:9222 \
    HERMES_X_USE_CONFIG_DIR=/tmp/hermes-x-use/config \
    HERMES_X_USE_DATA_DIR=/opt/data/x-use

EXPOSE 6081 8642 8643 9120

LABEL org.opencontainers.image.source="https://github.com/e-eugene/hermes-agent" \
      org.opencontainers.image.title="Hermes Agent Browser Runtime" \
      org.opencontainers.image.description="Hermes Agent runtime with headed Chromium and private service endpoints" \
      org.opencontainers.image.x-use.revision="e57e215e45b3e68cbd8cd7c46799cd932c234eac" \
      org.opencontainers.image.licenses="MIT"

CMD ["/usr/local/bin/hermes-runtime-entrypoint"]
