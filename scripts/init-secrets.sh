#!/usr/bin/env bash
# Create the ./secrets files used by docker-compose.prod.yml.
#
# Values you must supply yourself are prompted for; the rest are generated with
# a CSPRNG. Files are written with 0600 permissions and are git-ignored.
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

echo "Creating Docker secrets in $SECRETS_DIR"

# Generated values
generate_secret postgres_password
generate_secret whatsapp_verify_token
generate_secret admin_api_key
generate_secret grafana_admin_password

# The app connects with the generated database password.
if ! skip_existing database_url; then
  db_password="$(cat "$SECRETS_DIR/postgres_password")"
  write_secret database_url \
    "postgresql+asyncpg://postgres:${db_password}@db:5432/whatsapp_ai_bot"
fi

# Values only you can provide
prompt_secret openai_api_key "OpenAI API key (sk-...)"
prompt_secret whatsapp_token "WhatsApp Cloud API token"
prompt_secret whatsapp_phone_number_id "WhatsApp phone number ID"
prompt_secret whatsapp_app_secret "Meta app secret"

echo
echo "Done. Set the webhook verify token in Meta to:"
cat "$SECRETS_DIR/whatsapp_verify_token"; echo
echo "Never commit ./secrets - it is listed in .gitignore."
