# syntax=docker/dockerfile:1.7
# Operator dashboard: the Next.js static export served by Caddy, which also
# proxies /api/v1/* to the API over the private network (see Caddyfile).
# Build context: repository root.

FROM node:22-bookworm-slim AS build
ENV NEXT_TELEMETRY_DISABLED=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
WORKDIR /app
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/dashboard/ ./
# same-origin: the browser calls /api/v1 on the dashboard's own origin (Caddy proxies it)
ARG NEXT_PUBLIC_AQ_API_ORIGIN=same-origin
ENV NEXT_PUBLIC_AQ_API_ORIGIN=${NEXT_PUBLIC_AQ_API_ORIGIN}
RUN npm run build

FROM caddy:2.10-alpine
RUN addgroup -S -g 10001 web && adduser -S -D -H -u 10001 -G web web
COPY deploy/docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/out /srv
ENV PORT=8080 \
    XDG_CONFIG_HOME=/tmp/caddy-config \
    XDG_DATA_HOME=/tmp/caddy-data
USER web
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
