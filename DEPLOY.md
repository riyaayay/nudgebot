# NudgeBot Deployment Guide

This guide contains copy-pasteable instructions for deploying NudgeBot to production.

> [!IMPORTANT]
> **Single Instance Constraint:** Telegram long-polling and SQLite require **EXACTLY ONE** running instance. Running multiple instances with the same bot token causes Telegram to return `409 Conflict: terminated by other getUpdates request`. Always disable autoscaling.

---

## Prerequisites
1. **Telegram Bot Token:** Create a bot with [@BotFather](https://t.me/BotFather) and copy the HTTP API token.
2. **Encryption Key:** Generate a Fernet key:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

---

## Option 1: VPS Deployment (Ubuntu 22.04 / 24.04 with Docker Compose)

### 1. Install Docker & Git
```bash
sudo apt-get update && sudo apt-get install -y git curl ca-certificates
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

### 2. Clone Repository & Setup Environment
```bash
git clone https://github.com/your-username/nudgebot.git /opt/nudgebot
cd /opt/nudgebot
cp .env.example .env
nano .env
```
Fill in at least:
```ini
TELEGRAM_BOT_TOKEN=123456789:AAExampleToken
ENCRYPTION_KEY=your_generated_fernet_key_here
ADMIN_TELEGRAM_ID=your_personal_telegram_chat_id
```

### 3. Start Container
```bash
docker compose up -d --build
```

### 4. Verify Health & Logs
```bash
# Check container status (should display 'healthy')
docker compose ps

# View live application logs
docker compose logs -f

# Probe health endpoint
curl http://localhost:8080/healthz
```

### 5. Automated Daily Backups (Cron)
Add a root cron job to run backups nightly:
```bash
sudo crontab -e
```
Add the following line (runs at 04:00 AM UTC daily):
```cron
0 4 * * * cd /opt/nudgebot && docker compose exec -T nudgebot python scripts/backup_db.py --source /app/data/nudgebot.db --backup-dir /app/data/backups --retention-days 14
```

### 6. Updates & Upgrades
```bash
cd /opt/nudgebot
git pull origin main
docker compose up -d --build
```

### 7. Backup Restore Procedure
```bash
# 1. Stop the bot
docker compose stop nudgebot

# 2. Restore SQLite database from target backup file
cp ./data/backups/nudgebot_backup_YYYYMMDD_HHMMSS.db ./data/nudgebot.db

# 3. Start the bot
docker compose start nudgebot
```

---

## Option 1B: Systemd Unit Alternative (Running directly on host without Docker)

```bash
# Create dedicated system user
sudo useradd -r -s /bin/false -d /opt/nudgebot nudgebot
sudo chown -R nudgebot:nudgebot /opt/nudgebot

# Setup virtual environment
cd /opt/nudgebot
sudo -u nudgebot python3 -m venv .venv
sudo -u nudgebot .venv/bin/pip install -r requirements.txt
```

Create `/etc/systemd/system/nudgebot.service`:
```ini
[Unit]
Description=NudgeBot Campus Portal Monitor
After=network.target

[Service]
Type=simple
User=nudgebot
WorkingDirectory=/opt/nudgebot
EnvironmentFile=/opt/nudgebot/.env
ExecStart=/opt/nudgebot/.venv/bin/python main.py
Restart=always
RestartSec=10
LimitNOFILE=4096

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nudgebot
sudo journalctl -u nudgebot -f
```

---

## Option 2: Fly.io Deployment

Fly.io is ideal for low-cost hosting with persistent volumes.

### 1. Install flyctl & Authenticate
```bash
curl -L https://fly.io/install.sh | sh
fly auth login
```

### 2. Launch App & Create Persistent Volume
```bash
cd /path/to/nudgebot

# Initialize app without deploying immediately
fly launch --no-deploy

# Create a 1GB persistent volume for SQLite (region matching fly.toml, e.g. iad)
fly volumes create nudgebot_data --region iad --size 1
```

### 3. Set Production Secrets
```bash
fly secrets set \
  TELEGRAM_BOT_TOKEN="your_token_here" \
  ENCRYPTION_KEY="your_fernet_key_here" \
  LLM_PROVIDER="none" \
  ADMIN_TELEGRAM_ID="your_telegram_id"
```

### 4. Deploy and Enforce Single Instance
```bash
fly deploy

# Ensure exactly 1 machine runs (disable scaling)
fly scale count 1
```

### 5. Check Health & Logs
```bash
fly status
fly logs
```

---

## Option 3: Railway / Render Deployment

### Railway:
1. Create a new project pointing to your Git repository.
2. Under **Volumes**, add a Persistent Volume mounted at `/app/data`.
3. In **Variables**, add:
   - `TELEGRAM_BOT_TOKEN`
   - `ENCRYPTION_KEY`
   - `DATABASE_PATH=/app/data/nudgebot.db`
   - `HEALTH_PORT=8080`
   - `PORT=8080`
4. Set replica count strictly to **1** (single instance).

### Render:
1. Deploy as a **Web Service** (Docker environment).
2. Under **Disks**, attach a persistent disk mounted at `/app/data` (size 1 GB).
3. Set environment variables.
4. Scale setting: 1 instance.

