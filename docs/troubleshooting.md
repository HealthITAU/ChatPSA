# Troubleshooting

## Container Issues

### 502 Bad Gateway from reverse proxy

The Flask app hasn't started yet or the container is unhealthy.

```bash
docker compose ps
docker compose logs psa-app
```

If the container is restarting, check for missing environment variables or a corrupt database.

### Container won't start

Check for port conflicts:

```bash
sudo lsof -i :5001
```

If another service is using port 5001, change `APP_PORT` in your `.env` file.

### "Initial sync" indicator won't clear

The first ConnectWise sync takes a few minutes. Monitor progress:

```bash
docker compose logs -f psa-sync
```

If the sync is stuck, verify your CW API credentials:

```bash
docker compose exec chatpsa python -c "
from sync_cw_data import load_config, api_get
c = load_config()
print(api_get(c, 'system/info'))
"
```

## Authentication Issues

### OAuth redirect fails after login

The redirect URI registered in Azure must **exactly** match `https://your-domain.com/auth/callback` — including the `https` scheme and no trailing slash.

Also verify that your reverse proxy sends the `X-Forwarded-Proto` header. ChatPSA uses this to construct the callback URL. Without it, the app thinks it's running on `http` and Azure rejects the mismatch.

### AADSTS50105 error at Microsoft login

This means "Assignment required" is enabled on the Enterprise Application but the user hasn't been assigned. Go to **Entra admin centre → Enterprise applications → ChatPSA → Users and groups** and add the user.

### First user doesn't get admin access

The auto-admin grant happens on the first sign-in to a fresh deployment. If the database already has user records (e.g. restored from backup), the auto-grant won't trigger. Manually grant admin access:

```bash
docker compose exec chatpsa python -c "
from db import get_db
db = get_db()
db.execute(\"INSERT OR REPLACE INTO feature_access (email, feature) VALUES ('user@example.com', 'admin')\")
db.commit()
print('Done')
"
```

## Chat and AI Issues

### Chat requests time out

Each message makes one or two Claude API calls that can take up to a minute. Ensure your reverse proxy timeout is set to at least **120 seconds**:

- **Nginx:** `proxy_read_timeout 120s;`
- **Apache:** `ProxyTimeout 120` and `Timeout 120`
- **Caddy:** `read_timeout 120s` in `transport http`

### AI responses are inaccurate or outdated

The AI queries a local SQLite mirror, not ConnectWise directly. If data seems stale:

1. Check when the last sync ran: **Admin → Sync Status**
2. Trigger a manual sync: `docker compose exec chatpsa python sync_cw_data.py --db /data/cw_data.db`
3. Check for sync errors: `docker compose logs psa-sync`

### "AI summaries are disabled" error

Timeline AI summaries can be toggled in **Admin → Settings → Timeline**. Set "AI ticket summaries" to `true` to re-enable.

## Sync Issues

### Sync fails with 401/403 errors

API credentials are incorrect or have expired. Verify each integration's credentials in **Admin → Settings** or your `.env` file:

- **ConnectWise:** Check that the API member is active and the public/private key pair is valid
- **CIPP:** Client secrets expire — check the expiry date in Azure
- **Duo:** Integration keys don't expire, but the application may have been deleted
- **Huntress:** API credentials may have been regenerated
- **ThreatLocker:** API key may have been revoked

### Sync fails with 429 (rate limited)

The sync automatically handles rate limiting with retries. If it persists, the sync interval may be too aggressive for your data volume. Check logs for the specific integration:

```bash
docker compose logs psa-cipp-sync
```

### Sync containers are healthy but data is missing

Optional sync containers check for their required credentials on startup. If credentials aren't set, the container runs but does nothing. Check that the credentials are present:

```bash
docker compose exec chatpsa python -c "
from config import is_cipp_enabled, is_duo_enabled, is_huntress_enabled, is_threatlocker_enabled
print(f'CIPP: {is_cipp_enabled()}')
print(f'Duo: {is_duo_enabled()}')
print(f'Huntress: {is_huntress_enabled()}')
print(f'ThreatLocker: {is_threatlocker_enabled()}')
"
```

### Database is growing too large

Check database size:

```bash
docker compose exec chatpsa ls -lh /data/cw_data.db
```

If it's unusually large, old ticket notes may be accumulating. The sync only adds and updates records — it doesn't delete tickets that have been removed from ConnectWise.

## See also

- [Maintenance Guide](maintenance.md) — updating, backups, rollbacks, and container management
- [Administration Guide](administration.md) — managing users, settings, and integrations from the web UI
- [Authentication](authentication.md) — Azure AD setup, secret rotation, and credential validation
