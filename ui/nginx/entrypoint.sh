#!/bin/sh
# mias-ui start-up: render the nginx configuration into /tmp (the only writable path) and exec nginx.
# Inputs (non-secret): MIAS_UI_API_UPSTREAM (host:port, a fully qualified Service name; nginx's resolver ignores
# search domains) and optionally MIAS_UI_RESOLVER (an IP; default: the first nameserver in /etc/resolv.conf).
# Hardening Task 8: MIAS_UI_API_TOKEN_FILE (a path, not the token) names the read-only Secret file with the API read
# token. It is read into a shell variable only (never an argument, environment variable or log line), checked
# against a conservative token alphabet, and written as one nginx directive to /tmp/nginx/api-auth.conf (mode 0600).
# Unset: no token is injected (API calls then get 401). Set but unreadable or invalid: start-up fails (exit 2).
set -eu

upstream="${MIAS_UI_API_UPSTREAM:-mias-api.mias.svc.cluster.local:8080}"
case "$upstream" in
  *[!a-z0-9.:-]* | "" | :* | *: | *:*:*)
    echo "mias-ui: MIAS_UI_API_UPSTREAM must be host:port (lowercase DNS name or IPv4, no scheme or path)" >&2
    exit 2 ;;
esac
port="${upstream##*:}"
host="${upstream%:*}"
if [ "$host" = "$upstream" ] || [ -z "$host" ] || ! printf '%s' "$port" | grep -Eq '^[0-9]{1,5}$'; then
  echo "mias-ui: MIAS_UI_API_UPSTREAM must include a numeric port" >&2
  exit 2
fi

resolver="${MIAS_UI_RESOLVER:-$(awk '$1 == "nameserver" { print $2; exit }' /etc/resolv.conf 2>/dev/null || true)}"
if printf '%s' "$resolver" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'; then
  :
elif printf '%s' "$resolver" | grep -Eq '^[0-9a-fA-F:]+$'; then
  resolver="[$resolver]"
else
  echo "mias-ui: no usable DNS resolver (set MIAS_UI_RESOLVER or provide /etc/resolv.conf)" >&2
  exit 2
fi

mkdir -p /tmp/nginx/client_body /tmp/nginx/proxy /tmp/nginx/fastcgi /tmp/nginx/uwsgi /tmp/nginx/scgi
sed -e "s|@MIAS_UI_RESOLVER@|$resolver|g" -e "s|@MIAS_UI_API_UPSTREAM@|$upstream|g" \
  /opt/mias-ui/etc/nginx.conf.template > /tmp/nginx/nginx.conf

injection=disabled
if [ -n "${MIAS_UI_API_TOKEN_FILE:-}" ]; then
  if [ ! -r "$MIAS_UI_API_TOKEN_FILE" ]; then
    echo "mias-ui: the API token file is not readable" >&2
    exit 2
  fi
  token="$(cat "$MIAS_UI_API_TOKEN_FILE")"
  case "$token" in
    "" | *[!A-Za-z0-9._~+/=-]*)
      unset token
      echo "mias-ui: the API token file is empty or contains characters outside the token alphabet" >&2
      exit 2 ;;
  esac
  if [ "${#token}" -lt 16 ] || [ "${#token}" -gt 4096 ]; then
    unset token
    echo "mias-ui: the API token length is out of range" >&2
    exit 2
  fi
  ( umask 077; printf 'proxy_set_header Authorization "Bearer %s";\n' "$token" > /tmp/nginx/api-auth.conf )
  unset token
  injection=enabled
else
  ( umask 077; printf 'proxy_set_header Authorization "";\n' > /tmp/nginx/api-auth.conf )
fi

echo "{\"service.name\":\"mias-ui\",\"message\":\"starting nginx\",\"upstream\":\"$upstream\",\"resolver\":\"$resolver\",\"api_token_injection\":\"$injection\"}"
exec nginx -c /tmp/nginx/nginx.conf -e stderr -g "daemon off;"
