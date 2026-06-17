# Fix Dashboard Authentication — Deployment Guide

## Problem
The dashboard login is failing with "Incorrect email or password" because the droplet's `.env` file is missing the gateway authentication configuration.

## Solution

### Step 1: SSH to Droplet
```bash
ssh -o IdentitiesOnly=yes -i ~/.ssh/trade_agent_do deploy@134.209.79.232
```

### Step 2: Update .env with Gateway Configuration
```bash
cd ~/Options-Trading-AI

# Add/update the gateway and deployment settings
cat >> .env << 'EOF'
DOMAIN=trade.simplestudiotrades.com
TLS_EMAIL=simplestudiohub@gmail.com
GATEWAY_EMAIL=simplesstudiohub@gmail.com
GATEWAY_PASSWORD=MFSclassof2018
GATEWAY_SECRET_KEY=200020182022
STREAMLIT_ORIGIN=http://options-dashboard:8502
SESSION_MAX_AGE=28800
SESSION_COOKIE_SECURE=true
SESSION_COOKIE_DOMAIN=.simplestudiotrades.com
EOF
```

**Or** manually edit with nano:
```bash
nano .env
# Add the above lines at the end, save with Ctrl+X, then Y
```

### Step 3: Rebuild Containers
```bash
cd ~/Options-Trading-AI

# Create alias for convenience
alias dc="docker compose -f docker-compose.yml -f docker-compose.remote.yml --profile secure"

# Rebuild and restart
dc pull
dc up --build -d options-gateway options-dashboard

# Watch startup (Ctrl+C to exit)
dc logs -f options-gateway
```

### Step 4: Verify Deployment

#### Check Container Health
```bash
dc ps
# All 4 containers should show "Up" or "Up (healthy)"
```

#### Check Gateway Health
```bash
curl http://localhost:8080/_health
# Should return: {"status":"ok","service":"auth_gateway",...}
```

#### Test Login from Browser
- Navigate to: https://trade.simplestudiotrades.com/login
- Email: `simplesstudiohub@gmail.com`
- Password: `MFSclassof2018`
- Should redirect to dashboard (not show "Incorrect password")

### Step 5: If Still Having Issues

Check logs:
```bash
dc logs --tail=50 options-gateway   # Auth gateway logs
dc logs --tail=50 options-dashboard # Streamlit logs
dc logs --tail=50 options-worker    # Worker logs
dc logs --tail=50 options-proxy     # Caddy HTTPS logs
```

Verify .env was read correctly:
```bash
cd ~/Options-Trading-AI
cat .env | grep GATEWAY
cat .env | grep DOMAIN
cat .env | grep TLS_EMAIL
```

---

## Why This Happened

The `.env` file is excluded from git (in `.gitignore`) for security reasons. When you first deployed, the environment variables needed for the Docker containers weren't added to the droplet's `.env` file. The auth_gateway couldn't find the credentials and defaulted to empty strings, which is why authentication failed.

## After Fix

Once the containers restart, you should:
1. ✅ Login succeeds
2. ✅ Dashboard loads (may show startup message if worker hasn't run yet)
3. ✅ See "Waiting for first market data" if outputs/*.csv files are missing
4. ✅ Dashboard populates with data once worker runs its first cycle
