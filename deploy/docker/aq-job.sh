#!/bin/sh
# Run an `aq` command as the unprivileged app user, e.g. in a `railway ssh`
# shell on the worker:  aq-job research run ...   /   aq-job kill-switch status
set -eu
if [ "$(id -u)" = "0" ]; then
  exec setpriv --reuid=app --regid=app --init-groups -- aq "$@"
fi
exec aq "$@"
