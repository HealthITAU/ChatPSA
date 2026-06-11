# Backup and Restore

ChatPSA stores all data — synced records, conversation history, agent memories, user permissions, and application settings — in SQLite databases on the `cw-data` Docker volume, mounted at `/data` in all containers.

---

## Automated daily backups

A simple script that creates a rotating daily backup, keeping the last 7 days:

### 1. Create the backup script

```bash
cat > ~/chatpsa/backup.sh << 'SCRIPT'
#!/bin/bash
set -e
cd ~/chatpsa
dow=$(date +%A)
mkdir -p ~/chatpsabackups
docker compose exec -T chatpsa cp /data/cw_data.db /data/backup_${dow}.db
docker cp psa-app:/data/backup_${dow}.db ~/chatpsabackups/cw_data_${dow}.db
docker compose exec -T chatpsa rm -f /data/backup_${dow}.db
echo "$(date) — backup complete: cw_data_${dow}.db"
SCRIPT
chmod +x ~/chatpsa/backup.sh
```

### 2. Schedule with cron

```bash
crontab -e
```

Add this line to run the backup daily at 2 AM:

```
0 2 * * * ~/chatpsa/backup.sh >> ~/chatpsabackups/backup.log 2>&1
```

### 3. Verify

Run the script manually to confirm it works:

```bash
mkdir -p ~/chatpsabackups
~/chatpsa/backup.sh
ls -lh ~/chatpsabackups/
```

You should see a file like `cw_data_Monday.db`. The day-of-week naming means each day's backup overwrites the same day from the previous week, so you always have the last 7 days without accumulating old files.

The log file at `~/chatpsabackups/backup.log` records each run. If a backup fails, `set -e` stops the script immediately rather than silently continuing.

**Disk space:** The rotating backups keep up to 7 additional copies of the database. Check your database size before enabling automated backups and ensure you have at least 8× that amount free (7 backups + the live copy):

```bash
docker compose exec chatpsa ls -lh /data/cw_data.db
```

---

> **Security note:** The database contains plaintext credentials (Azure, CIPP, Duo, Huntress, ThreatLocker) in the `app_settings` table. Treat backup files with the same care as your `.env` file — do not commit them to version control or store them on shared/public storage.

## Manual backup

To take a one-off backup at any time:

```bash
cd ~/chatpsa
docker compose exec chatpsa cp /data/cw_data.db /data/backup.db
docker cp psa-app:/data/backup.db ~/cw_data_backup.db
docker compose exec chatpsa rm -f /data/backup.db
```

---

## Restoring from backup

```bash
cd ~/chatpsa
docker compose down
docker cp ~/chatpsabackups/cw_data_Monday.db psa-app:/data/cw_data.db
docker compose up -d
```

Replace `cw_data_Monday.db` with whichever day's backup you want to restore. The app picks up the restored database on startup — no migration or import step needed.

---

## Resetting the database

To start completely fresh (re-syncs all data from ConnectWise):

```bash
cd ~/chatpsa
docker compose down
docker volume rm chatpsa_cw-data
docker compose up -d
```

This deletes all synced data, conversation history, agent memories, and user permissions. The first user to sign in will become admin again.

---

## See also

- [Maintenance Guide](maintenance.md) — updating, rollbacks, and container management
- [Administration Guide](administration.md) — managin