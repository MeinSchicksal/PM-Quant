#!/usr/bin/env bash
# PM-Quant Ubuntu 7x24 Production Baseline Setup Script
# Safe to run on 2C/1G~2G RAM, 30GB disk Ubuntu 22.04/24.04 VPS.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/pm-quant}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/pm-quant}"

echo "=== [1/6] Checking Swap (target: >= 2GB for 1G~2G RAM VPS) ==="
if ! swapon --show | grep -q '/swapfile'; then
    if [ ! -f /swapfile ]; then
        sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
    fi
    sudo swapon /swapfile
    if ! grep -q '^/swapfile ' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    fi
    echo "[OK] 2GB /swapfile enabled."
else
    echo "[OK] /swapfile already active."
fi

echo "=== [2/6] Bounding systemd-journald disk usage (max 200M) ==="
sudo mkdir -p /etc/systemd/journald.conf.d
cat <<'EOF' | sudo tee /etc/systemd/journald.conf.d/pm-quant.conf >/dev/null
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=64M
MaxRetentionSec=14day
EOF
sudo systemctl restart systemd-journald || true
echo "[OK] journald capped at 200M."

echo "=== [3/6] Verifying NTP clock synchronization ==="
if command -v timedatectl >/dev/null 2>&1; then
    timedatectl set-ntp true || true
    timedatectl status | grep -E "System clock synchronized|NTP service" || true
fi
echo "[TIP] For minimal clock step-back risk, consider installing chrony: sudo apt install -y chrony"

echo "=== [4/6] Preparing /etc/pm-quant.env template ==="
if [ ! -f /etc/pm-quant.env ]; then
    cat <<'EOF' | sudo tee /etc/pm-quant.env >/dev/null
# PM-Quant runtime environment variables
PM_WEB_HOST=127.0.0.1
PM_WEB_USER=admin
# REQUIRED: replace with a strong random password (>= 12 chars)
PM_WEB_PASS=CHANGE_ME_TO_A_STRONG_PASSWORD
PM_PROXY=
PM_TZ_OFFSET_HOURS=8
# Spot WS provider: binance (default) / kraken / coinbase / okx / auto (lowest RTT)
PM_SPOT_WS_PROVIDER=auto

# Optional external phone/IM alerting (leave blank to disable):
# Telegram Bot:
PM_TELEGRAM_BOT_TOKEN=
PM_TELEGRAM_CHAT_ID=
# iOS Bark (e.g. https://api.day.app/your_key):
PM_BARK_URL=
# Feishu / DingTalk / Discord / Generic Webhook:
PM_ALERT_WEBHOOK_URL=

# Optional off-site rclone remote for daily summary.jsonl backup (e.g. gdrive:pm-quant-backups):
PM_BACKUP_RCLONE_REMOTE=
EOF
    sudo chmod 600 /etc/pm-quant.env
    echo "[ACTION REQUIRED] Edit /etc/pm-quant.env and set a strong PM_WEB_PASS."
else
    echo "[OK] /etc/pm-quant.env already exists."
fi

echo "=== [5/6] Installing version-agnostic off-peak ledger backup cron (keeps 30 days) ==="
sudo mkdir -p "$BACKUP_DIR"
cat <<EOF | sudo tee /usr/local/bin/pm-quant-backup.sh >/dev/null
#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR}"
BACKUP_DIR="${BACKUP_DIR}"
DAY="\$(date -u +%Y%m%d)"
mkdir -p "\$BACKUP_DIR"
if [ -f /etc/pm-quant.env ]; then
    # shellcheck disable=SC1091
    set -a; source /etc/pm-quant.env; set +a
fi
for f in "\${APP_DIR}"/data/2_ledgers/v*/summary.jsonl; do
    [ -f "\$f" ] || continue
    ver="\$(basename "\$(dirname "\$f")")"
    cp -f "\$f" "\${BACKUP_DIR}/summary_\${ver}_\${DAY}.jsonl"
done
find "\$BACKUP_DIR" -name 'summary_*.jsonl' -mtime +30 -delete
if [ -n "\${PM_BACKUP_RCLONE_REMOTE:-}" ] && command -v rclone >/dev/null 2>&1; then
    rclone copy "\$BACKUP_DIR" "\${PM_BACKUP_RCLONE_REMOTE}" --include "summary_*_\${DAY}.jsonl" >/dev/null 2>&1 || true
fi
EOF
sudo chmod 750 /usr/local/bin/pm-quant-backup.sh
# Run at 00:17 UTC (2 minutes away from 5-minute settlement boundaries :00/:05/:10/:15)
echo "17 0 * * * root /usr/local/bin/pm-quant-backup.sh" | sudo tee /etc/cron.d/pm-quant-backup >/dev/null
sudo chmod 644 /etc/cron.d/pm-quant-backup
echo "[OK] Version-agnostic daily backup scheduled at 00:17 UTC in /etc/cron.d/pm-quant-backup."

echo "=== [6/6] Firewall baseline reminder ==="
if command -v ufw >/dev/null 2>&1; then
    sudo ufw status || true
    echo "[TIP] Ensure port 8900 is NOT exposed publicly; only allow 22/tcp, 80/tcp, 443/tcp."
fi

echo "=== Done! Next steps: ==="
echo "1. Edit /etc/pm-quant.env (set strong PM_WEB_PASS + optional Telegram/Bark/Webhook alerts)."
echo "2. Recommended isolated deployment:"
echo "   sudo cp deploy/pm-quant-engine.service deploy/pm-quant-web.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload && sudo systemctl enable --now pm-quant-engine pm-quant-web"
echo "3. Configure Caddy (deploy/Caddyfile.example) for HTTPS mobile/desktop access."
echo "4. Point UptimeRobot / Healthchecks.io to https://<your-domain>/healthz for 7x24 liveness alerts."
