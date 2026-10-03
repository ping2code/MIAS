#!/bin/sh
# mias-ui start-up: render the nginx configuration into /tmp (the only writable path) and exec nginx.
# Inputs (non-secret): MIAS_UI_API_UPSTREAM (host:port, a fully qualified Service name; nginx's resolver ignores
# search domains) and optionally MIAS_UI_RESOLVER (an IP; default: the first nameserver in /etc/resolv.conf).
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

echo "{\"service.name\":\"mias-ui\",\"message\":\"starting nginx\",\"upstream\":\"$upstream\",\"resolver\":\"$resolver\"}"
exec nginx -c /tmp/nginx/nginx.conf -e stderr -g "daemon off;"
