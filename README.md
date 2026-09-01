# Autoposting platform — clean MVP

This is a new, multi-tenant foundation for a publishing service. It was written
from scratch; the previously inspected public repository is **not** a dependency
or source file for this project.

The MVP already provides:

- owner-created customer accounts, roles and login with Argon2id password hashes
  and short-lived JWTs;
- no self-service account, API key or OAuth flow: the owner creates every
  cabinet and assigns every destination;
- each customer creates their own Zernio API key, Telegram Bot token and/or
  Whapi token, then gives it to the owner outside the app;
- the owner saves that token in the customer's card, encrypted at rest with
  Fernet, then selects the customer's destinations from a list rather than
  entering opaque IDs by hand;
- Zernio account selection is automatic: after the owner saves the customer's
  Zernio token, the Admin page loads their connected Instagram/TikTok accounts
  and stores the selected account internally rather than asking for an ID;
- Whapi selection is automatic: the Admin page loads groups and WhatsApp
  Channels visible to the saved Whapi token;
- Telegram selection uses Bot API updates: after a bot receives a group command
  or a channel post, the Admin page can discover that group/channel and offer it
  in a list. Telegram Bot API has no endpoint for listing every bot chat;
- Instagram Story publishing is available alongside standard Instagram posts,
  Reels and carousels. A Story must contain exactly one image or video and can
  be scheduled like any other post;
- private local uploads, database-backed scheduled deliveries, idempotency keys,
  retry state, final-status checks for Zernio, and a separate worker process;
- publication adapters for Zernio, Telegram and Whapi. Whapi receives media as
  Base64 directly in its API request, so local testing doesn't require a public
  media URL;
- every uploaded video is prepared just before delivery as MP4/H.264/AAC, the
  broadly compatible baseline for Instagram, TikTok and WhatsApp. Common MOV,
  MKV, AVI, WebM, WMV and similar video extensions are accepted even when a
  browser labels them as a generic binary upload;
- a responsive Russian-language browser interface at `/`: login, owner-managed
  cabinets, connections, immediate/scheduled posts and delivery history.

Billing, password reset email, two-factor authentication and self-service signup
are deliberately not enabled in the first deployment. The owner controls access
until those flows are designed and tested.

## Local development with uv

The dependency graph is pinned in `uv.lock`. On your computer, use:

```bash
uv sync --extra dev
uv run ruff check app tests
uv run pytest
uv run uvicorn app.main:app --reload
```

For a browser-only local test, use SQLite and initialise the database:

```bash
mkdir -p data
uv run python -m app.init_db
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

This local checkout includes an ignored `.env` solely for the first test. It
creates the local administrator configured there. Replace all values before
production. Production is started only through Docker Compose; do not copy a
local `.env` with real credentials into Git.

## Before the first start

1. Create a new private GitHub repository owned by you, then copy this directory
   into it. Do not deploy from the former developer’s account.
2. On the new server, copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`,
   the first owner email/password, and generate unique `APP_JWT_SECRET` and
   `APP_ENCRYPTION_KEY` values. Keep an offline copy of the encryption key:
   losing it makes stored Whapi tokens unreadable by design.
3. Create an A record for `biamino.cc` pointing to the new server's public IPv4
   address. Before deployment, ensure no other web server occupies ports 80/443.
   Set `APP_DOMAIN=biamino.cc`, a real `CERTBOT_EMAIL`, and a unique `APP_PATH`.
   Generate it locally with
   `printf '/panel-%s/\\n' "$(openssl rand -hex 24)"`, then paste the printed
   value into `.env`. The path is an extra obscurity layer, not a substitute
   for a strong password and TLS.
4. Run `bash deploy/deploy-production.sh`. It starts Nginx in HTTP-only mode,
   requests the first Let's Encrypt certificate through HTTP-01, restarts Nginx
   with HTTPS, then starts the worker. Only Nginx publishes ports 80 and 443;
   PostgreSQL and the application have no host ports.
5. Keep the project at `/opt/autoposting`, then install the certificate renewal
   timer once. If you choose another directory, edit both paths in
   `deploy/systemd/autoposting-cert-renew.service` before installing it:

   ```bash
   sudo install -m 0755 deploy/renew-certificates.sh /opt/autoposting/deploy/renew-certificates.sh
   sudo install -m 0644 deploy/systemd/autoposting-cert-renew.service /etc/systemd/system/
   sudo install -m 0644 deploy/systemd/autoposting-cert-renew.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now autoposting-cert-renew.timer
   sudo systemctl list-timers autoposting-cert-renew.timer
   ```

   The timer checks renewal daily and reloads Nginx so the renewed certificate
   is used. Open the URL printed by the deploy script, for example
   `https://biamino.cc/panel-YOUR_SECRET/`.

The server `.env` contains infrastructure secrets (database password, JWT,
encryption key, domain and the private route). Keep it mode `600` and out of
Git. The owner enters every customer's Zernio, Telegram Bot or Whapi token
through the protected Admin page; every token is encrypted before it reaches
PostgreSQL and is write-only in the UI/API. It is never returned to a browser or
logged.

## Important operational limits

- This implementation defaults Telegram uploads to 50 MB. For larger Telegram
  video, deploy Telegram’s Local Bot API server separately, point
  `TELEGRAM_API_BASE_URL` at it, and raise all three limits coherently: Nginx,
  `APP_MAX_UPLOAD_BYTES`, and `TELEGRAM_MAX_UPLOAD_BYTES`.
- Whapi receives local media directly as Base64. It therefore works during a
  local test as well as in production and never needs direct access to the
  media directory. `WHAPI_MAX_MEDIA_BYTES` defaults to 100 MB; account for the
  Base64 overhead when configuring proxy limits.
- The production Docker image installs FFmpeg. It creates a temporary MP4/H.264
  copy of every video before delivery, then removes it; source uploads stay
  untouched. Images remain in their original JPEG/PNG form. PDFs are available
  only for Telegram and WhatsApp, because Instagram and TikTok do not accept
  document posts.
- For TikTok, the user must choose one visibility value returned for that exact
  TikTok account and confirm preview/consent. Comment, Duet and Stitch switches
  are always sent to the API but can be off; AI disclosure, commercial label,
  TikTok Inbox draft, photo music and cover choices are optional. TikTok allows
  one video or up to 35 photos; Instagram carousel destinations keep their
  documented ten-item limit.
- In the Admin page, select a customer, save their Zernio/Telegram/Whapi token,
  then add their destination. Zernio and Whapi destinations load automatically.
  For Telegram, add the bot to the group and send it a command; for a channel,
  make it an admin and publish one test post, then click «Обновить список».
  The internal ID remains invisible. The current on-demand scan uses
  `getUpdates`, so it cannot run while that bot has a Telegram webhook. No end
  user sees or completes an OAuth process inside this application.
  If a channel post still isn't delivered to the bot, start a private chat with
  that bot and forward a channel post to it; Telegram includes the source channel
  in the forwarded message, so it can be selected without exposing an ID.
- Whapi is a linked-device service. Present a clear user consent screen and an
  anti-spam policy before allowing a user to connect a number.
- A delivery's attempt counter means provider attempts, not the number of posts
  visible on a social network. History shows every destination separately so a
  failed WhatsApp retry cannot be confused with a successful Telegram or Zernio
  delivery.
- Production starts two worker containers by default. They claim database rows
  with `SKIP LOCKED`, so the same delivery is never sent twice by two workers.
  Set `APP_WORKER_REPLICAS` to a value from 1 to 4 in `.env` and redeploy; start
  with 2 because higher concurrency can hit per-account social-platform limits.

## Core API flow

1. The bootstrap owner logs in using `POST /v1/auth/login`, then creates customer
   accounts through `POST /v1/admin/users`.
2. Send the access token as `Authorization: Bearer <token>`.
3. The owner saves a customer's provider token with
   `POST /v1/admin/users/{user_id}/credentials`, then selects a destination and
   assigns it with `POST /v1/admin/users/{user_id}/connections`. The Admin UI
   obtains candidate accounts through `/zernio/accounts`, `/whapi/targets`, and
   `/telegram/targets`; `external_id` remains internal to the API.
4. A customer can only view assigned destinations, upload files using
   `POST /v1/uploads`, then create a post with
   `POST /v1/posts`. One delivery is created for every selected destination.

The first integration test before real launch should use separate test accounts,
one small image, and one destination per platform. Check both the remote post and
the delivery status in `GET /v1/posts/{id}`.
