#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
compose=(docker compose -f compose.yaml -f compose.prod.yaml)

"${compose[@]}" run --rm certbot renew --webroot --webroot-path /var/www/certbot --quiet
"${compose[@]}" exec -T nginx nginx -s reload
