# NudgeBot Operational Runbook

This guide covers troubleshooting and recovery procedures for common production issues.

---

## 1. Telegram `Conflict` Error (409 Conflict: terminated by other getUpdates request)

### Symptom
Log shows:
`telegram.error.Conflict: Conflict: terminated by other getUpdates request; make sure that only one bot instance is running`

### Cause
Telegram long-polling allows **strictly ONE** active listener process per bot token. This error means two processes (e.g. Docker container + local process, or old container shutting down slowly, or duplicate Fly.io/Railway machine) are attempting to poll `getUpdates` with the same token.

### Resolution
1. Verify how many instances are running:
   ```bash
   docker ps | grep nudgebot
   ```
2. Stop all competing instances:
   ```bash
   docker compose down
   # If running locally or as systemd:
   pkill -f "python main.py"
   ```
3. If deployed on Fly.io, verify `fly scale show` has count `1` (autoscaling MUST be disabled).
4. Restart single instance:
   ```bash
   docker compose up -d
   ```

---

## 2. Decryption Failed / Key Changed

### Symptom
Log shows:
`ValueError: Could not decrypt stored credential. The ciphertext is invalid or ENCRYPTION_KEY / ENCRYPTION_KEYS does not contain the key used to encrypt it.`

### Cause
`ENCRYPTION_KEY` in `.env` was changed or rotated, but previous user credentials were encrypted with the old key.

### Resolution (Key Rotation)
1. Do **NOT** delete the old key!
2. Set `ENCRYPTION_KEYS` in `.env` as a comma-separated list with the **newest primary key first**, followed by the **old key**:
   ```bash
   ENCRYPTION_KEYS=new_key_here,old_key_here
   ```
3. NudgeBot will automatically use the first key for new encryptions while decrypting existing credentials using the fallback keys via `MultiFernet`.
4. Restart the bot.

---

## 3. Stale / Expired Canvas Token

### Symptom
Log shows:
`PortalAuthError: Canvas personal access token was rejected or expired.`
User receives:
`⚠️ Your portal link stopped working. I've paused polling. Run /link to reconnect.`

### Cause
The user's Canvas token expired, was revoked in Canvas settings, or user password was changed.

### Resolution
1. The bot automatically pauses polling for this user so logs aren't spammed.
2. The user should go to Canvas > Account > Settings > "+ New Access Token", generate a fresh token, and run `/link` in Telegram.

---

## 4. Generic HTML Layout Changed

### Symptom
Log shows:
`PortalFetchError: ...` or 0 items parsed when assignments are known to exist.

### Cause
The school portal updated its frontend CSS classes, table structure, or login form field names.

### Resolution
1. Open the portal in a browser, inspect elements (F12).
2. Identify the updated CSS selectors for rows, titles, and due dates.
3. Test updated selectors against sample HTML in `tests/test_scraper_generic.py`.
4. Instruct the user to run `/link`, choose "Other (generic HTML)", and paste the updated JSON configuration.

---

## 5. Health Check Failing (HTTP 503)

### Symptom
`docker ps` shows container status `(unhealthy)`, or `curl http://localhost:8080/healthz` returns `503 Service Unavailable`.

### Cause
The scheduler tick hasn't executed in > 180 seconds (3x the 60s interval). This indicates an uncaught exception deadlocked the scheduler, an unhandled blocking call froze the event loop, or disk I/O stalled.

### Resolution
1. Check recent container logs:
   ```bash
   docker compose logs --tail=100 -f
   ```
2. Look for any deadlock or freeze trace.
3. Restart container:
   ```bash
   docker compose restart
   ```

---

## 6. Database Locked (`sqlite3.OperationalError: database is locked`)

### Symptom
`sqlite3.OperationalError: database is locked`

### Cause
SQLite WAL mode was disabled or an external process held an exclusive write lock without a timeout.

### Resolution
1. Verify SQLite pragmas: NudgeBot enables `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` automatically.
2. Verify no external process (like an unclosed GUI SQLite browser) has opened `nudgebot.db` with exclusive locks.
3. Check permissions on `data/nudgebot.db` and directory.

