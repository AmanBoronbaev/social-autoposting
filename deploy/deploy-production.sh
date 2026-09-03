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
worker_replicas="$(read_env APP_WORKER_REPLICAS)"
worker_replicas="${worker_replicas:-2}"
telegram_api_base_url="$(read_env TELEGRAM_API_BASE_URL)"
telegram_profiles="$(read_env COMPOSE_PROFILES)"
telegram_api_id="$(read_env TELEGRAM_API_ID)"
telegram_api_hash="$(read_env TELEGRAM_API_HASH)"
[[ -n "$domain" && -n "$email" && -n "$path" ]] || die "APP_DOMAIN, CERTBOT_EMAIL and APP_PATH are required"
[[ "$domain" != "post.example.com" && "$path" != *"CHANGE_ME"* ]] || die "replace example domain and secret path"
[[ "$domain" =~ ^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$ ]] || die "APP_DOMAIN must be a valid DNS name"
[[ "$path" =~ ^/[A-Za-z0-9._-]{12,128}/$ ]] || die "APP_PATH must be a URL-safe path ending in /"
[[ "$worker_replicas" =~ ^[1-4]$ ]] || die "APP_WORKER_REPLICAS must be a number from 1 to 4"

use_local_telegram_api=false
if [[ "$telegram_api_base_url" == "http://telegram-bot-api:8081" ]]; then
  use_local_telegram_api=true
  [[ ",$telegram_profiles," == *",telegram-local-api,"* ]] || die "COMPOSE_PROFILES must include telegram-local-api for the local Telegram API"
  [[ "$telegram_api_id" =~ ^[0-9]+$ ]] || die "TELEGRAM_API_ID must be a numeric Telegram app ID"
  [[ "$telegram_api_hash" =~ ^[A-Fa-f0-9]{32}$ ]] || die "TELEGRAM_API_HASH must be a 32-character hexadecimal value"
elif [[ ",$telegram_profiles," == *",telegram-local-api,"* ]]; then
  die "set TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081 when enabling telegram-local-api"
fi

compose=(docker compose -f compose.yaml -f compose.prod.yaml)
certbot_compose=(docker compose --profile tools -f compose.yaml -f compose.prod.yaml)
"${compose[@]}" config -q
if [[ "$use_local_telegram_api" == true ]]; then
  "${compose[@]}" up -d telegram-bot-api
fi
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
"${compose[@]}" up -d --build --scale "worker=$worker_replicas" worker
"${compose[@]}" ps
printf 'Open: https://%s%s\n' "$domain" "$path"
