#!/usr/bin/env bash
#
# One-time TLS bootstrap for a fresh production host.
#
# There is a chicken-and-egg problem to solve: nginx will not start without a
# certificate file to open, and certbot cannot obtain a certificate without an
# nginx already serving the ACME challenge over port 80. This script breaks the
# cycle by planting a throwaway self-signed certificate, starting nginx against
# it, swapping it for a real one, and reloading.
#
# Run once per domain:
#
#   DOMAIN=bot.example.com CERTBOT_EMAIL=ops@example.com ./scripts/init-letsencrypt.sh
#
# Renewal after this is automatic -- the certbot service in
# docker-compose.prod.yml wakes every 12 hours and nginx reloads every 6.
#
# Safe to re-run: it refuses to overwrite a real certificate unless you pass
# FORCE=1.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
COMPOSE=(docker compose -f "${COMPOSE_FILE}")

DOMAIN="${DOMAIN:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-}"
# Let's Encrypt rate-limits failed issuance hard (5 per account per hostname
# per hour). Set STAGING=1 while you are still fighting DNS or firewall rules.
STAGING="${STAGING:-0}"
FORCE="${FORCE:-0}"
RSA_KEY_SIZE=4096

if [[ -z "${DOMAIN}" ]]; then
  echo "DOMAIN is required, e.g. DOMAIN=bot.example.com $0" >&2
  exit 1
fi

if [[ -z "${CERTBOT_EMAIL}" ]]; then
  echo "CERTBOT_EMAIL is required. Let's Encrypt uses it for expiry warnings" >&2
  echo "-- the one mail that tells you a renewal has been silently failing." >&2
  exit 1
fi

echo "==> Bootstrapping TLS for ${DOMAIN}"

# --------------------------------------------------------------------------
# Diffie-Hellman parameters, required by the DHE suites in nginx/tls.conf.
# --------------------------------------------------------------------------
if ! "${COMPOSE[@]}" run --rm --entrypoint sh certbot -c \
  '[ -f /etc/letsencrypt/ssl-dhparams.pem ]' 2>/dev/null; then
  echo "==> Generating ssl-dhparams.pem (this takes a minute)"
  "${COMPOSE[@]}" run --rm --entrypoint sh certbot -c \
    'openssl dhparam -out /etc/letsencrypt/ssl-dhparams.pem 2048'
fi

# --------------------------------------------------------------------------
# Refuse to clobber a working certificate.
# --------------------------------------------------------------------------
live_path="/etc/letsencrypt/live/${DOMAIN}"
if "${COMPOSE[@]}" run --rm --entrypoint sh certbot -c \
  "[ -s ${live_path}/privkey.pem ]" 2>/dev/null; then
  if [[ "${FORCE}" != "1" ]]; then
    echo "A certificate for ${DOMAIN} already exists."
    echo "Renewal is automatic. Re-run with FORCE=1 only if you know why."
    exit 0
  fi
  echo "==> FORCE=1: replacing the existing certificate"
fi

# --------------------------------------------------------------------------
# Plant a self-signed placeholder so nginx has something to open.
# --------------------------------------------------------------------------
echo "==> Creating a temporary self-signed certificate"
"${COMPOSE[@]}" run --rm --entrypoint sh certbot -c "
  mkdir -p '${live_path}' &&
  openssl req -x509 -nodes -newkey rsa:${RSA_KEY_SIZE} -days 1 \
    -keyout '${live_path}/privkey.pem' \
    -out '${live_path}/fullchain.pem' \
    -subj '/CN=${DOMAIN}' &&
  cp '${live_path}/fullchain.pem' '${live_path}/chain.pem'
"

echo "==> Starting nginx against the placeholder"
"${COMPOSE[@]}" up -d nginx

# nginx needs a moment before it will answer the challenge.
sleep 3

# --------------------------------------------------------------------------
# Delete the placeholder. certbot will not overwrite an existing lineage, and
# a self-signed file in live/ makes it think one is already there.
# --------------------------------------------------------------------------
echo "==> Removing the placeholder"
"${COMPOSE[@]}" run --rm --entrypoint sh certbot -c "
  rm -rf '${live_path}' \
         '/etc/letsencrypt/archive/${DOMAIN}' \
         '/etc/letsencrypt/renewal/${DOMAIN}.conf'
"

# --------------------------------------------------------------------------
# Request the real certificate over http-01.
# --------------------------------------------------------------------------
staging_arg=""
if [[ "${STAGING}" != "0" ]]; then
  echo "==> STAGING=1: issuing an untrusted certificate from the staging CA"
  staging_arg="--staging"
fi

echo "==> Requesting a certificate from Let's Encrypt"
"${COMPOSE[@]}" run --rm --entrypoint certbot certbot \
  certonly --webroot -w /var/www/certbot \
  ${staging_arg} \
  --email "${CERTBOT_EMAIL}" \
  -d "${DOMAIN}" \
  --rsa-key-size "${RSA_KEY_SIZE}" \
  --agree-tos \
  --no-eff-email \
  --non-interactive \
  --force-renewal

echo "==> Reloading nginx with the real certificate"
"${COMPOSE[@]}" exec nginx nginx -s reload

cat <<EOF

==> Done.

Verify from another machine:

  curl -sSI https://${DOMAIN}/health | head -n 1
  curl -sSI http://${DOMAIN}/health | head -n 1     # expect 301
  curl -sS  https://${DOMAIN}/health

Check the headers and the protocol floor:

  curl -sSI https://${DOMAIN}/ | grep -i strict-transport-security
  openssl s_client -connect ${DOMAIN}:443 -tls1_1 </dev/null   # expect failure

Then point Meta at the HTTPS callback:

  https://${DOMAIN}/webhook

Meta will not accept an http:// callback, and it verifies the chain -- a
staging certificate will be rejected. Re-run without STAGING=1 before
configuring the webhook.
EOF
