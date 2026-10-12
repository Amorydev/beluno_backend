#!/usr/bin/env bash
# Asks for the Resend API key (hidden input), stores it as the SMTP password in
# deploy/staging/.env, switches email to SMTP and restarts the API and the worker.
#   ssh -t root@<host> 'bash /opt/beluno/backend/deploy/vps/set-smtp-password.sh'
set -euo pipefail
cd "$(dirname "$0")/../staging"
[ -f .env ] || { echo "deploy/staging/.env is missing" >&2; exit 1; }
read -rsp "Resend API key (re_...): " key
echo
case "$key" in
  re_*) ;;
  *) echo "That does not look like a Resend API key; nothing changed." >&2; exit 1 ;;
esac
umask 077
tmp=$(mktemp)
grep -vE '^(BELUNO_SMTP_PASSWORD|BELUNO_EMAIL_BACKEND)=' .env > "$tmp"
printf "BELUNO_EMAIL_BACKEND=smtp\nBELUNO_SMTP_PASSWORD='%s'\n" "$key" >> "$tmp"
cat "$tmp" > .env
rm -f "$tmp"
docker compose -p beluno up -d api worker
echo "Email now goes through Resend."
