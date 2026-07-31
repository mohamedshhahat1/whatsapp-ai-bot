#!/usr/bin/env bash
# Create the ./secrets files used by docker-compose.prod.yml.
#
# Values you must supply yourself are prompted for; the rest are generated with
# a CSPRNG. Files are written with 0600 permissions and are git-ignored.
#
# Safe to re-run: existing non-empty secrets are never overwritten, so this can
# be used to add newly introduced secrets to an already-deployed stack.
#
# Usage: ./scripts/init-secrets.sh
set -euo pipefail

SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets"
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

write_secret() {
  local name="$1" value="$2"
  printf '%s' "$value" > "$SECRETS_DIR/$name"
  chmod 600 "$SECRETS_DIR/$name"
  echo "  wrote secrets/$name"
}

skip_existing() {
  local name="$1"
  if [[ -s "$SECRETS_DIR/$name" ]]; then
    echo "  secrets/$name already exists - skipping"
    return 0
  fi
  return 1
}

random_secret() {
  openssl rand -base64 36 | tr -d '\n/+=' | cut -c1-40
}

prompt_secret() {
  local name="$1" label="$2" value=""
  skip_existing "$name" && return 0
  read -r -s -p "$label: " value
  echo
  [[ -n "$value" ]] || { echo "  $name cannot be empty" >&2; exit 1; }
  write_secret "$name" "$value"
}

generate_secret() {
  local name="$1"
  skip_existing "$name" && return 0
  write_secret "$name" "$(random_secret)"
}

# Docker refuses to start a stack when a file backing a declared secret does
# not exist. Optional credentials therefore have to exist as EMPTY files rather
# than be absent -- otherwise leaving off-site backups unconfigured would take
# the entire stack down instead of just disabling uploads.
#
# Note the deliberate use of -e rather than -s here: an empty placeholder is a
# valid end state, and re-running must not keep recreating it.
placeholder_secret() {
  local name="$1"
  if [[ -e "$SECRETS_DIR/$name" ]]; then
    echo "  secrets/$name already exists - skipping"
    return 0
  fi
  : > "$SECRETS_DIR/$name"
  chmod 600 "$SECRETS_DIR/$name"
  echo "  wrote secrets/$name (empty placeholder)"
}

echo "Creating Docker secrets in $SECRETS_DIR"

# Generated values
generate_secret postgres_password
generate_secret whatsapp_verify_token
generate_secret admin_api_key
generate_secret grafana_admin_password

# Redis authentication (docs/REDIS_SECURITY.md).
#
# Two separate passwords on purpose. The exporter's ACL user is read-only, so
# a compromised metrics container cannot clear the rate limits, spend counters
# or reply-idempotency keys -- which sharing one password would allow.
#
# random_secret strips /+= so the value is safe to percent-encode into a URL
# and to embed in a Redis ACL directive, where whitespace and quoting rules
# are unforgiving.
generate_secret redis_password
generate_secret redis_exporter_password

# The app connects with the generated database password.
if ! skip_existing database_url; then
  db_password="$(cat "$SECRETS_DIR/postgres_password")"
  write_secret database_url \
    "postgresql+asyncpg://postgres:${db_password}@db:5432/whatsapp_ai_bot"
fi

# Backup encryption.
#
# Generated whether or not off-site backups are enabled, because backup-upload.sh
# REFUSES to upload an unencrypted dump -- a database of customer conversations
# sitting unencrypted in someone else's storage is not an acceptable failure
# mode, so the passphrase must always be available.
#
# WARNING: back this value up somewhere OUTSIDE this server. Losing it makes
# every off-site backup permanently unreadable. A backup you cannot decrypt is
# not a backup.
generate_secret backup_encryption_passphrase

# Off-site storage credentials. Fill in only the provider you use; the rest
# stay as empty placeholders.
#
#   AWS S3 / Backblaze B2 : backup_s3_access_key_id, backup_s3_secret_access_key
#   Google Cloud Storage  : backup_gcs_credentials  (the service-account JSON)
#   Azure Blob Storage    : backup_azure_sas_token
placeholder_secret backup_s3_access_key_id
placeholder_secret backup_s3_secret_access_key
placeholder_secret backup_gcs_credentials
placeholder_secret backup_azure_sas_token

# Values only you can provide
prompt_secret openai_api_key "OpenAI API key (sk-...)"
prompt_secret whatsapp_token "WhatsApp Cloud API token"
prompt_secret whatsapp_phone_number_id "WhatsApp phone number ID"
prompt_secret whatsapp_app_secret "Meta app secret"

echo
echo "Done. Set the webhook verify token in Meta to:"
cat "$SECRETS_DIR/whatsapp_verify_token"; echo
echo
echo "IMPORTANT: copy secrets/backup_encryption_passphrase somewhere off this"
echo "server. Without it, off-site backups cannot be restored."
echo
echo "Never commit ./secrets - it is listed in .gitignore."
