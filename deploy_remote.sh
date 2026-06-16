#!/usr/bin/env bash
# deploy_remote.sh — bring up the secure, mobile-accessible trading stack.
#
# Run from the project directory on the VM (as the deploy user):
#   bash deploy_remote.sh
#
# Requires a populated .env with at least:
#   GATEWAY_EMAIL, GATEWAY_PASSWORD, GATEWAY_SECRET_KEY,
#   STREAMLIT_ORIGIN, DOMAIN, TLS_EMAIL, SESSION_COOKIE_SECURE,
#   SESSION_COOKIE_DOMAIN

set -euo pipefail

log() { printf '\n[deploy] %s\n' "$1"; }

if [[ ! -f .env ]]; then
    echo "Missing .env. Copy .env.example to .env and fill required values first." >&2
    exit 1
fi

REQUIRED_VARS=(GATEWAY_EMAIL GATEWAY_PASSWORD GATEWAY_SECRET_KEY DOMAIN TLS_EMAIL)
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
    value="$(grep -E "^${var}=" .env | head -n1 | cut -d= -f2-)"
    if [[ -z "${value}" ]]; then
        MISSING+=("${var}")
    fi
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo "The following required .env values are empty: ${MISSING[*]}" >&2
    exit 1
fi

log "Building and starting the secure remote stack"
docker compose \
    -f docker-compose.yml \
    -f docker-compose.remote.yml \
    --profile secure \
    up --build -d

log "Waiting for services to report healthy"
sleep 8
docker compose -f docker-compose.yml -f docker-compose.remote.yml ps

DOMAIN_VALUE="$(grep -E '^DOMAIN=' .env | head -n1 | cut -d= -f2-)"
log "Stack is starting. Once TLS is issued, open: https://${DOMAIN_VALUE}"
log "Check proxy logs with: docker compose logs -f options-proxy"
