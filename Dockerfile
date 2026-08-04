ARG HERMES_BASE_IMAGE=nousresearch/hermes-agent:v2026.7.20
FROM ${HERMES_BASE_IMAGE}

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
        openbox \
        socat \
        x11-utils \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY runtime/hermes-browser-supervisor.sh /usr/local/bin/hermes-browser-supervisor
COPY runtime/wait-for-hermes-browser.sh /usr/local/bin/wait-for-hermes-browser
COPY runtime/residential-proxy.py /usr/local/bin/residential-proxy
COPY runtime/social-account.py /usr/local/bin/social-account
COPY runtime/hermes-runtime-entrypoint.sh /usr/local/bin/hermes-runtime-entrypoint
COPY runtime/hermes-runtime-health.py /usr/local/bin/hermes-runtime-health

RUN chmod 0755 \
    /usr/local/bin/hermes-browser-supervisor \
    /usr/local/bin/wait-for-hermes-browser \
    /usr/local/bin/residential-proxy \
    /usr/local/bin/social-account \
    /usr/local/bin/hermes-runtime-entrypoint \
    /usr/local/bin/hermes-runtime-health

ENV DISPLAY=:99 \
    AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium \
    AGENT_BROWSER_HEADED=true \
    BROWSER_CDP_URL=http://127.0.0.1:9222

EXPOSE 8642 8643 9120

LABEL org.opencontainers.image.source="https://github.com/e-eugene/hermes-agent" \
      org.opencontainers.image.title="Hermes Agent Browser Runtime" \
      org.opencontainers.image.description="Hermes Agent runtime with headed Chromium and private service endpoints" \
      org.opencontainers.image.licenses="MIT"

CMD ["/usr/local/bin/hermes-runtime-entrypoint"]
