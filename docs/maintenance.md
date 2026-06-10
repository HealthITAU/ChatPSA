# Maintenance

Server-side tasks for keeping ChatPSA running — updating, backups, container management, and recovery. These tasks require SSH access to the server; for settings you can change from the web UI, see the [Administration Guide](administration.md).

---

## Updating

Your database and settings are preserved across updates. They live on a Docker volume (`cw-data`) that is independent of the application containers — rebuilding or replacing containers never touches your data.

### Standard update

```bash
cd ~/chatpsa
git pull
docker compose up -d --build
```

Docker rebuilds the image with the new code and recreates only the containers that changed.

### Updating a fork

If you forked ChatPSA on GitHub rather than cloning directly, you'll need to sync your fork when updates are released:

1. Go to your fork on GitHub — you'll see a banner saying "This branch is X commits behind HealthITAU/ChatPSA". Click **Sync fork → Update branch**.
2. On your server, pull the updated code and rebuild:
   ```bash
   cd ~/chatpsa
   git pull
   docker compose up -d --build
   ```

If you cloned directly from the main repository (without forking), `git pull` picks up updates automatically.

### Deploying from a local copy

If you don't `git pull` on the server directly:

```bash
# From your workstation:
rsync -avz --exclude='venv' --exclude='*.db' --exclude='__pycache__' \
  ./chatpsa/ user@your-server-ip:~/chatpsa/

# On the server:
cd ~/chatpsa
docker compose up -d --build
```

### After updating

1. **Check for new settings** — new releases may add optional settings to Admin → Settings. Review the [changelog](/changelog) or `.env.example` for additions.
2. **Review container logs** — confirm the app started cleanly:
   ```bash
   docker compose logs psa-app --tail 20
   ```
3. **Clean up old images** — Docker keeps previous images after a rebuild. Reclaim disk space with:
   ```bash
   docker image prune -f
   ```

### Rolling back

If an update causes problems, check out the previous version and rebuild:

```bash
cd ~/chatpsa
git log --oneline -5          # find the last good commit
git checkout <commit-hash>
docker compose up -d --build
```

To return to the latest version afterward: `git checkout main && git pull && docker compose up -d --build`.

---

## Container management

Common commands for managing the Docker containers:

```bash
# View logs (live follow)
docker compose logs -f psa-app
docker compose logs -f psa-sync

# Restart the web app after config changes
docker compose restart chatpsa

# Rebuild and restart after code changes
docker compose up -d --build

# Stop everything (preserves data)
docker compose down

# Force a manual ConnectWise sync
docker compose exec chatpsa python sync_cw_data.py --db /data/cw_data.db

# Check database size
docker compose exec chatpsa ls -lh /data/cw_data.db
```

### Container overview

| Container | Purpose |
|-----------|---------|
| `psa-app` | Flask web app (gunicorn) |
| `psa-sync` | ConnectWise Manage sync (hourly, business hours) |
| `psa-cipp-sync` | CIPP / Microsoft 365 sync (every 4 hours) |
| `psa-duo-sync` | Duo Security user sync (every 6 hours) |
| `psa-threatlocker-sync` | ThreatLocker endpoint sync (every 6 hours) |
| `psa-huntress-sync` | Huntress EDR sync (every 6 hours) |
| `psa-timeline-parse` | Timeline event extraction (every 2 hours) |

Optional sync containers start automatically but wait for credentials. Configure them via **Admin → Settings** or `.env` and the sync begins within 30 seconds. To disable one entirely, comment out its service block in `docker-compose.yml`.

---

## Backups

All ChatPSA data lives in SQLite databases on the `cw-data` Docker volume. This volume is preserved across container rebuilds and updates, but is not backed up automatically.

See the [Backup and Restore Guide](backup.md) for a ready-made daily backup script with cron scheduling, manual backup commands, and restore instructions.

---

## Emergency recovery

### Secret expired — can't log in

If the Azure client secret has expired and no one can access the Admin UI:

1. Create a new client secret in [Entra admin centre](https://entra.microsoft.com)
2. Update your `.env` file:
   ```env
   AZURE_CLIENT_SECRET=your-new-secret-value
   ENV_OVERRIDE=true
   ```
3. Restart the container: `docker compose down && docker compose up -d`
4. ChatPSA writes the env values into the database on startup
5. Log in and verify everything works
6. Remove `ENV_OVERRIDE=true` from `.env` and restart again
7. From now on, manage the secret through **Admin → Settings**

For more detail on credential rotation, see [Authentication — Rotating a Client Secret](authentication.md#rotating-a-client-secret).

### ENV_OVERRIDE mode

Setting `ENV_OVERRIDE=true` in `.env` forces all configurable settings to read from environment variables, taking precedence over the database. Settings whose env var is commented out or missing continue to read from the database — only vars explicitly present in the environment are overridden.

This is useful for emergency recovery, config resets, and CI/CD pipelines. The Admin Settings page becomes read-only while active. See [Authentication — ENV_OVERRIDE Mode](authentication.md#env_override-mode) for full details.

---

## Monitoring

### Application logs

The web app logs to stdout, which Docker captures:

```bash
docker compose logs psa-app --tail 50
docker compose logs psa-app | grep -i error
```

### Admin diagnostics

The **Logs**