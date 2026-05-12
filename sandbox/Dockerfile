FROM golang:1.26-trixie AS proxy-builder

ENV CGO_ENABLED=0

WORKDIR /src/proxy
COPY proxy/go.mod proxy/main.go ./
RUN go build -trimpath -ldflags='-s -w' -o /sandbox-proxy .

FROM debian:trixie-slim AS browser-builder

ARG DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates nodejs npm; \
    npm install -g --omit=dev --no-audit --no-fund dev-browser@latest; \
    dev-browser install; \
    npm cache clean --force; \
    find /usr/local/lib/node_modules/dev-browser -type d \( -name test -o -name tests -o -name __tests__ -o -name docs -o -name examples \) -prune -exec rm -rf '{}' +; \
    find /usr/local/lib/node_modules/dev-browser -type f \( -name '*.md' -o -name '*.map' -o -name 'CHANGELOG*' \) -delete; \
    rm -rf /root/.npm /var/lib/apt/lists/* /var/cache/apt/* /tmp/* /var/tmp/*

FROM debian:trixie-slim

ENV HOME=/root \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY=:0

ARG DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    printf '%s\n' \
      'path-exclude=/usr/share/doc/*' \
      'path-exclude=/usr/share/man/*' \
      'path-exclude=/usr/share/info/*' \
      'path-exclude=/usr/share/lintian/*' \
      'path-exclude=/usr/share/locale/*' \
      'path-include=/usr/share/locale/locale.alias' \
      > /etc/dpkg/dpkg.cfg.d/01_nodoc; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fonts-droid-fallback \
      libasound2t64 \
      libatk-bridge2.0-0t64 \
      libatk1.0-0t64 \
      libcairo2 \
      libcups2t64 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libglib2.0-0t64 \
      libgtk-3-0t64 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      nodejs \
      novnc \
      npm \
      python3 \
      python3-pip \
      supervisor \
      websockify \
      wget \
      x11vnc \
      xvfb; \
    ln -sf vnc.html /usr/share/novnc/index.html; \
    mkdir -p /data; \
    rm -rf /root/.npm /var/lib/apt/lists/* /var/cache/apt/* /tmp/* /var/tmp/*

COPY --from=browser-builder /root/.dev-browser /root/.dev-browser
COPY --from=browser-builder /root/.cache /root/.cache
COPY --from=browser-builder /usr/local/bin/dev-browser /usr/local/bin/dev-browser
COPY --from=browser-builder /usr/local/lib/node_modules/dev-browser /usr/local/lib/node_modules/dev-browser
RUN set -eux; \
    chromium_binary="$(find /root/.cache /root/.dev-browser -type f \( -path '*/chrome-linux/chrome' -o -path '*/chrome-linux64/chrome' -o -name chromium -o -name chrome \) -perm /111 | sort | head -n 1)"; \
    if [ -z "$chromium_binary" ]; then \
      echo 'No Chromium binary found from dev-browser install. Executable candidates:'; \
      find /root/.cache /root/.dev-browser -maxdepth 6 -type f -perm /111 -print | sort; \
      exit 1; \
    fi; \
    "$chromium_binary" --version; \
    ln -sf "$chromium_binary" /usr/local/bin/dev-browser-chromium

COPY --from=proxy-builder --chmod=755 /sandbox-proxy /usr/local/bin/sandbox-proxy
COPY --chmod=755 entrypoint.sh /entrypoint.sh
COPY supervisord.conf /supervisord.conf
COPY .agents /root/.agents

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
