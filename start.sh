#!/usr/bin/env bash
#
# start.sh - one-command launcher for Parlay.
#
# Brings up the PO-token provider and the bot together via docker compose.
# Verifies prerequisites and a populated .env before starting.
#
set -euo pipefail

cd "$(dirname "$0")"

err() { printf 'error: %s\n' "$1" >&2; exit 1; }

# --- Prerequisites -----------------------------------------------------------
command -v docker >/dev/null 2>&1 || err "docker is not installed or not on PATH"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  err "docker compose is not available"
fi

# --- Configuration -----------------------------------------------------------
if [[ ! -f .env ]]; then
  err ".env not found. Copy .env.example to .env and fill in your values."
fi

# Ensure required keys are present and non-empty.
required=(TELEGRAM_API_ID TELEGRAM_API_HASH OPERATOR_ID GEMINI_API_KEY)
for key in "${required[@]}"; do
  if ! grep -qE "^${key}=.+" .env; then
    err "missing or empty ${key} in .env"
  fi
done

mkdir -p data

# --- Launch ------------------------------------------------------------------
echo "Starting Parlay (bot + PO-token provider)..."
"${COMPOSE[@]}" up --build -d

echo "Up. Follow logs with: ${COMPOSE[*]} logs -f parlay"
