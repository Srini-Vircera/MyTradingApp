#!/bin/sh
# Container entrypoint for the Python services (Railway or any Docker host).
#
#   AQ_ROLE=api       deployment check, then the operator API on $PORT (default 8000)
#   AQ_ROLE=worker    deployment check, then the scheduler if AQ_SCHEDULER_ENABLED=true,
#                     otherwise stay idle so research/backtest jobs can be run in it
#   AQ_ROLE=migrate   apply database migrations and exit
#   <command ...>     run that command instead (one-off jobs, pre-deploy migrations)
#
# Always runs the application as the unprivileged "app" user. Live trading is
# never started from here: `aq deploy check` refuses live mode.
# AQ_ENTRYPOINT_DRY_RUN=1 prints the commands instead of running them (tests).
set -eu

dry="${AQ_ENTRYPOINT_DRY_RUN:-}"

run() {
  if [ -n "$dry" ]; then echo "$*"; else "$@"; fi
}

start() {
  if [ -n "$dry" ]; then echo "exec $*"; exit 0; fi
  exec "$@"
}

# Platform volumes are mounted root-owned: fix ownership of the state
# directory, then drop privileges and re-run this script as "app".
if [ "$(id -u)" = "0" ] && [ -z "$dry" ]; then
  mkdir -p /app/var
  chown -R app:app /app/var
  exec setpriv --reuid=app --regid=app --init-groups -- "$0" "$@"
fi

if [ "$#" -gt 0 ]; then
  start "$@"
fi

# Bind all interfaces: IPv6 dual-stack where available (Railway's private network
# is IPv6), IPv4 otherwise (plain Docker hosts without IPv6).
default_host() {
  if [ -s /proc/net/if_inet6 ]; then echo "::"; else echo "0.0.0.0"; fi
}

case "$(printf '%s' "${AQ_SCHEDULER_ENABLED:-false}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) scheduler=1 ;;
  *) scheduler="" ;;
esac

case "${AQ_ROLE:-}" in
  api)
    run aq deploy check --role api
    start aq api serve --host "${AQ_BIND_HOST:-$(default_host)}" --port "${PORT:-8000}" --behind-proxy
    ;;
  worker)
    run aq deploy check --role worker
    if [ -n "$scheduler" ]; then
      start aq trade run
    fi
    echo "worker: scheduler disabled (AQ_SCHEDULER_ENABLED is not true); idle for research and backtest jobs"
    start sleep infinity
    ;;
  migrate)
    start aq db upgrade
    ;;
  *)
    echo "AQ_ROLE must be api, worker or migrate (got '${AQ_ROLE:-}')" >&2
    exit 64
    ;;
esac
