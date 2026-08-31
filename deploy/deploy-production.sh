#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
read_env() { sed -n "s/^$1=//p" .env | tail -n 1; }

[[ -f .env ]] || die "create .env from .env.example first"
domain="$(read_env APP_DOMAIN)"
email="$(read_env CERTBOT_EMAIL)"
path="$(read_env APP_PATH)"
[[ -n "$domain" && -n "$email" && -n "$path" ]] || die "APP_DOMAIN, CERTBOT_EMAIL and APP_PATH are required"
[[ "$domain" != "post.example.com" && "$path" != *"CHANGE_ME"* ]] || die "replace example domain and secret path"
[[ "$domain" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] || die "APP_DOMAIN must be a valid DNS name"
[[ "$path" =~ ^/[A-Za-z0-9._-]{12,128}/$ ]] || die "APP_PATH must be a URL-safe path ending in /"

compose=(docker compose -f compose.yaml -f compose.prod.yaml)
certbot_compose=(docker compose --profile tools -f compose.yaml -f compose.prod.yaml)
"${compose[@]}" config -q
"${compose[@]}" up -d --build postgres init-media migrate web
# Nginx reads the mounted templates only at container startup. Recreate just
# this stateless proxy on deploy so template and APP_PATH changes take effect.
"${compose[@]}" up -d --force-recreate nginx

if ! "${certbot_compose[@]}" run --rm --no-deps --entrypoint sh certbot \
  -c "test -s /etc/letsencrypt/live/${domain}/fullchain.pem"; then
  printf 'Requesting the first TLS certificate for %s...\n' "$domain"
  "${certbot_compose[@]}" run --rm --no-deps certbot certonly --webroot --webroot-path /var/www/certbot \
    --email "$email" --agree-tos --no-eff-email -d "$domain"
  "${compose[@]}" restart nginx
fi

# `worker` has its own Compose image, so it must be rebuilt as well; otherwise
# provider fixes can reach the web API while the old publishing code keeps
# running in the worker container.
"${compose[@]}" up -d --build worker
"${compose[@]}" ps
printf 'Open: https://%s%s\n' "$domain" "$path"
