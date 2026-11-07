#!/bin/sh
# Local docker leaves PORT unset and listens on 8000, same as before.
# TRUST_PROXY_HEADERS is set on Fly so the player IP is taken from
# X-Forwarded-For instead of the proxy's TCP address. It stays off locally,
# where the TCP peer is the client and a spoofed header must not be trusted.
set -eu
port="${PORT:-8000}"
set -- uvicorn backend.main:app --host 0.0.0.0 --port "$port"
flag=$(printf '%s' "${TRUST_PROXY_HEADERS:-}" | tr '[:upper:]' '[:lower:]')
case "$flag" in
  1|true|yes|on)
    set -- "$@" --proxy-headers --forwarded-allow-ips=*
    ;;
esac
exec "$@"
