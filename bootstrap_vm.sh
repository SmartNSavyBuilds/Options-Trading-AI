#!/usr/bin/env bash
# bootstrap_vm.sh — prepare a fresh Ubuntu 24.04 VM to host the trading stack.
#
# Run as root on the VM:
#   bash bootstrap_vm.sh deploy
#
# Where "deploy" is the non-root user to create for running the stack.
# This script is idempotent and safe to re-run.

set -euo pipefail

DEPLOY_USER="${1:-deploy}"

log() { printf '\n[bootstrap] %s\n' "$1"; }

if [[ "$(id -u)" -ne 0 ]]; then
    echo "This script must be run as root." >&2
    exit 1
fi

log "Updating base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y ca-certificates curl gnupg ufw git chrony

log "Enabling time sync"
systemctl enable --now chrony

log "Creating deploy user: ${DEPLOY_USER}"
if ! id "${DEPLOY_USER}" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "${DEPLOY_USER}"
fi
usermod -aG sudo "${DEPLOY_USER}"

if [[ -f /root/.ssh/authorized_keys ]]; then
    log "Propagating root SSH keys to ${DEPLOY_USER}"
    install -d -m 700 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" "/home/${DEPLOY_USER}/.ssh"
    install -m 600 -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" /root/.ssh/authorized_keys "/home/${DEPLOY_USER}/.ssh/authorized_keys"
fi

log "Hardening SSH (key-only, no root login)"
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl restart ssh || systemctl restart sshd || true

log "Configuring firewall (allow 22, 80, 443)"
ufw allow OpenSSH || ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

log "Installing Docker Engine and Compose plugin"
install -m 0755 -d /etc/apt/keyrings
if [[ ! -f /etc/apt/keyrings/docker.gpg ]]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
fi
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

log "Enabling Docker at boot and granting access to ${DEPLOY_USER}"
systemctl enable --now docker
usermod -aG docker "${DEPLOY_USER}"

log "Done. Next steps:"
cat <<EOF

  1. Log in as ${DEPLOY_USER}:        su - ${DEPLOY_USER}
  2. Clone the repository.
  3. Copy .env.example to .env and fill gateway + TLS values.
  4. Run: bash deploy_remote.sh

EOF
