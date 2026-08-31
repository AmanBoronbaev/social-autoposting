#!/bin/sh
set -eu

: "${APP_DOMAIN:?APP_DOMAIN is required}"
: "${APP_PATH:?APP_PATH is required}"

if ! printf '%s' "$APP_DOMAIN" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$'; then
  echo "APP_DOMAIN must be a valid DNS name" >&2
  exit 1
fi

case "$APP_PATH" in
  /*/) ;;
  *) echo "APP_PATH must start and end with /" >&2; exit 1 ;;
esac
path_body="${APP_PATH#/}"
path_body="${path_body%/}"
if [ "${#path_body}" -lt 12 ]; then
  echo "APP_PATH must contain at least 12 characters" >&2
  exit 1
fi
case "$path_body" in
  *[!A-Za-z0-9._-]*) echo "APP_PATH contains unsupported characters" >&2; exit 1 ;;
esac

certificate="/etc/letsencrypt/live/${APP_DOMAIN}/fullchain.pem"
template="/etc/nginx/templates/http-only.conf.template"
if [ -f "$certificate" ]; then
  template="/etc/nginx/templates/https.conf.template"
fi

envsubst '${APP_DOMAIN} ${APP_PATH}' < "$template" > /etc/nginx/conf.d/default.conf
exec nginx -g 'daemon off;'
