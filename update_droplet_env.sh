#!/bin/bash
# Quick script to update .env on droplet with missing deployment settings

# SSH to droplet and update .env with gateway config
ssh -o IdentitiesOnly=yes -i ~/.ssh/trade_agent_do deploy@134.209.79.232 << 'REMOTE_EOF'

cd ~/Options-Trading-AI

# Backup existing .env
cp .env .env.backup.$(date +%s)

# Update or add gateway configuration
grep -q "^DOMAIN=" .env && sed -i 's/^DOMAIN=.*/DOMAIN=trade.simplestudiotrades.com/' .env || echo "DOMAIN=trade.simplestudiotrades.com" >> .env
grep -q "^TLS_EMAIL=" .env && sed -i 's/^TLS_EMAIL=.*/TLS_EMAIL=simplestudiohub@gmail.com/' .env || echo "TLS_EMAIL=simplestudiohub@gmail.com" >> .env
grep -q "^GATEWAY_EMAIL=" .env && sed -i 's/^GATEWAY_EMAIL=.*/GATEWAY_EMAIL=simplesstudiohub@gmail.com/' .env || echo "GATEWAY_EMAIL=simplesstudiohub@gmail.com" >> .env
grep -q "^GATEWAY_PASSWORD=" .env && sed -i 's/^GATEWAY_PASSWORD=.*/GATEWAY_PASSWORD=MFSclassof2018/' .env || echo "GATEWAY_PASSWORD=MFSclassof2018" >> .env
grep -q "^GATEWAY_SECRET_KEY=" .env && sed -i 's/^GATEWAY_SECRET_KEY=.*/GATEWAY_SECRET_KEY=200020182022/' .env || echo "GATEWAY_SECRET_KEY=200020182022" >> .env
grep -q "^STREAMLIT_ORIGIN=" .env && sed -i 's|^STREAMLIT_ORIGIN=.*|STREAMLIT_ORIGIN=http://options-dashboard:8502|' .env || echo "STREAMLIT_ORIGIN=http://options-dashboard:8502" >> .env
grep -q "^SESSION_MAX_AGE=" .env && sed -i 's/^SESSION_MAX_AGE=.*/SESSION_MAX_AGE=28800/' .env || echo "SESSION_MAX_AGE=28800" >> .env
grep -q "^SESSION_COOKIE_SECURE=" .env && sed -i 's/^SESSION_COOKIE_SECURE=.*/SESSION_COOKIE_SECURE=true/' .env || echo "SESSION_COOKIE_SECURE=true" >> .env
grep -q "^SESSION_COOKIE_DOMAIN=" .env && sed -i 's/^SESSION_COOKIE_DOMAIN=.*/SESSION_COOKIE_DOMAIN=.simplestudiotrades.com/' .env || echo "SESSION_COOKIE_DOMAIN=.simplestudiotrades.com" >> .env

echo "✓ .env updated with gateway configuration"
echo "Backup saved to: .env.backup.$(date +%s)"

# Rebuild containers
echo "Rebuilding containers..."
alias dc="docker compose -f docker-compose.yml -f docker-compose.remote.yml --profile secure"
dc pull
dc up --build -d options-gateway options-dashboard

# Check status
dc ps

REMOTE_EOF

echo "Deployment complete. Try logging in at https://trade.simplestudiotrades.com"
