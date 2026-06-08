# ChatPSA — Deployment Guide

Step-by-step instructions for deploying on an Ubuntu server with Docker, behind an Apache reverse proxy with HTTPS and optional Microsoft Entra ID (Azure AD) authentication.

## Prerequisites

- An Ubuntu 22.04+ server with Apache already running
- A domain name pointed at your server's IP address (e.g. `your-domain.com`)
- Docker and Docker Compose installed on the server
- Apache modules: `proxy`, `proxy_http`, `rewrite`, `headers`, `ssl` (see Step 5)
- An Anthropic API key (https://console.anthropic.com)
- Your [ConnectWise Manage API](docs/integrations/connectwise.md) credentials
- (Recommended) Access to Microsoft Entra ID (Azure AD) to register an app for SSO

---

> **Path convention:** This guide uses `~/chatpsa` for simplicity. For production deployments, `/opt/chatpsa` is a common alternative — substitute the path throughout if you prefer it.

---

## Step 1: Install Docker on your server

```bash
# SSH into your server, then:
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER
# Log out and back in for the group change to take effect
```

---

## Step 2: Clone the project to your server

```bash
# On your server:
git clone https://github.com/your-org/ChatPSA.git ~/ChatPSA
cd ~/ChatPSA
```

Or, if deploying from a local copy:

```bash
# From your workstation — exclude venv, database, and __pycache__
rsync -avz --exclude='venv' --exclude='*.db' --exclude='__pycache__' \
  ./ChatPSA/ user@your-server-ip:~/ChatPSA/
```

---

## Step 3: Register a Microsoft Entra ID app (optional)

Skip this step if you do not need Azure AD authentication. Set `AZURE_AUTH_ENABLED=false` in your `.env` file to disable it.

1. Go to https://entra.microsoft.com
2. Navigate to **Identity → Applications → App registrations**
3. Click **New registration**
   - Name: `ChatPSA`
   - Supported account types: **Accounts in this organizational directory only**
   - Redirect URI: **Web** → `https://your-domain.com/auth/callback`
4. Click **Register**
5. On the app's Overview page, copy:
   - **Application (client) ID** → this is your `AZURE_CLIENT_ID`
   - **Directory (tenant) ID** → this is your `AZURE_TENANT_ID`
6. Go to **Certificates & secrets → New client secret**
   - Description: `ChatPSA`
   - Expiry: 24 months (set a calendar reminder to rotate)
   - Copy the **Value** (not the ID) → this is your `AZURE_CLIENT_SECRET`

### Restricting access to specific users or groups

By default, every user in your Azure AD tenant can sign in. To limit access to specific people:

1. In the [Entra admin centre](https://entra.microsoft.com), go to **Identity → Applications → Enterprise applications**
2. Find and select **ChatPSA** (the app you just registered)
3. Go to **Properties** and set **Assignment required?** to **Yes**, then click **Save**
4. Go to **Users and groups → Add user/group**
5. Select the users or security groups that should have access, then click **Assign**

Anyone not assigned will be blocked at the Microsoft login screen with an "AADSTS50105" error. You can come back here at any time to add or remove users.

> **Tip:** Create a security group (e.g. `ChatPSA Users`) in **Identity → Groups** and assign that group to the app. Then you can manage access by adding/removing group members instead of editing the app assignment each time.

---

## Step 4: Configure environment variables

On the server:

```bash
cd ~/ChatPSA
cp .env.example .env
nano .env
```

Fill in all the values:

```
ANTHROPIC_API_KEY=sk-ant-...

CW_SITE=api.cw.your-domain.com
CW_COMPANY_ID=your-company
CW_PUBLIC_KEY=your-public-key
CW_PRIVATE_KEY=your-private-key
CW_CLIENT_ID=your-cw-client-id

AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=your-secret-value
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

CW_DB_PATH=/data/cw_data.db
FLASK_SECRET_KEY=generate-a-random-string-here
```

Generate a Flask secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

---

## Step 5: Enable required Apache modules

These modules are needed for the reverse proxy. Enabling them has no effect on your existing VirtualHosts.

```bash
sudo a2enmod proxy proxy_http rewrite headers ssl
```

Verify they are loaded cleanly before continuing:

```bash
sudo apachectl configtest
```

---

## Step 6: Get an SSL certificate for the subdomain

If you don't already have a cert for your domain, issue one with Certbot. This runs on the host (not in Docker) using the Apache plugin, which handles domain verification automatically without touching other VirtualHosts:

```bash
sudo apt install -y certbot python3-certbot-apache
sudo certbot certonly --apache -d your-domain.com \
  --email your-email@example.com \
  --agree-tos \
  --no-eff-email
```

The certificate will be placed at `/etc/letsencrypt/live/your-domain.com/`.

Certbot installs a systemd timer that auto-renews certificates — no cron job or extra container needed.

---

## Step 7: Create the Apache VirtualHost

Create a new config file. Keeping it separate means no risk to your other sites:

```bash
sudo nano /etc/apache2/sites-available/ChatPSA.conf
```

Paste the following, replacing `your-domain.com` with your actual domain:

```apache
<VirtualHost *:80>
    ServerName your-domain.com
    RewriteEngine On
    RewriteRule ^(.*)$ https://%{HTTP_HOST}$1 [R=301,L]
</VirtualHost>

<VirtualHost *:443>
    ServerName your-domain.com

    SSLEngine On
    SSLCertificateFile      /etc/letsencrypt/live/your-domain.com/fullchain.pem
    SSLCertificateKeyFile   /etc/letsencrypt/live/your-domain.com/privkey.pem

    # Proxy to the Docker container (bound to localhost only)
    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:5001/
    ProxyPassReverse / http://127.0.0.1:5001/

    # Tell Flask it is behind HTTPS — required for correct OAuth callback URLs
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Forwarded-For   "%{REMOTE_ADDR}s"

    # Allow time for two Claude API calls per chat message
    ProxyTimeout 120
    Timeout      120

    # Security headers
    Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains"
    Header always set X-Content-Type-Options "nosniff"
    Header always set X-Frame-Options "DENY"

    ErrorLog  ${APACHE_LOG_DIR}/ChatPSA-error.log
    CustomLog ${APACHE_LOG_DIR}/ChatPSA-access.log combined
</VirtualHost>
```

Enable the site and reload Apache. Use `configtest` first — it validates all VirtualHosts together and will catch any errors before they affect your other sites:

```bash
sudo a2ensite chatpsa
sudo apachectl configtest        # Must say "Syntax OK" before proceeding
sudo systemctl reload apache2    # Graceful reload — does not drop active connections
```

---

## Step 8: Launch the Docker containers

```bash
cd ~/ChatPSA
docker compose up -d
```

This starts the following containers:

| Container | Purpose | Required? |
|-----------|---------|-----------|
| `psa-app` | Flask web app (gunicorn), bound to `127.0.0.1:5001` | Yes |
| `psa-sync` | ConnectWise Manage sync every 60 min during business hours | Yes |
| `psa-cipp-sync` | CIPP / Microsoft 365 data sync every 4 hours | Optional |
| `psa-duo-sync` | Duo Security user/MFA status sync every 6 hours | Optional |
| `psa-threatlocker-sync` | ThreatLocker endpoint inventory sync every 6 hours | Optional |
| `psa-huntress-sync` | Huntress EDR organisation and incident sync every 6 hours | Optional |
| `psa-timeline-parse` | Extracts future dates from ticket notes every 2 hours | Optional |

The **cipp-sync**, **duo-sync**, **threatlocker-sync**, **huntress-sync**, and **timeline-parse** containers are optional. Each starts automatically but waits for credentials — configure them via Admin → Settings or `.env` and the sync begins within 30 seconds. To disable any of them entirely, comment out or remove the corresponding service block in `docker-compose.yml` before running `docker compose up`.

SSL, HTTPS, and routing are handled entirely by Apache on the host. There is no nginx or certbot container.

### Initial data sync

On a fresh deployment, the sync container waits 5 minutes before pulling data from ConnectWise for the first time. Until the initial sync completes, the UI will show a pulsing "Initial sync" indicator — this is normal and will resolve automatically once data is available.

The initial sync typically takes a few minutes depending on the size of your ConnectWise instance. Check its progress:

```bash
docker compose logs -f cw-sync
```

If you don't want to wait, you can trigger a sync immediately:

```bash
docker compose exec chatpsa python sync_cw_data.py --db /data/cw_data.db
```

---

## Step 9: Verify

1. Open `https://your-domain.com` in a browser
2. If Azure AD is enabled, you should be redirected to Microsoft login
3. Sign in with your organization's Microsoft 365 account
4. On first load, you will see a "Waiting for initial sync" message — this clears automatically once the sync completes
5. After the sync, you should see the chat interface with your ConnectWise data

### First-user admin

The first user to sign in on a fresh deployment is **automatically granted admin access** (plus analytics and timeline). This means you don't need to manually run SQL to bootstrap permissions — just sign in and you'll see the Admin page.

### Runtime settings

Many configuration values (app name, helpdesk board, timezone, Claude model, etc.) can be changed live from **Admin → Settings** without redeploying. Environment variables seed the database on first boot; after that, changes made in the admin UI take effect within 30 seconds across all workers.

If you prefer environment variables to always be authoritative (e.g. in CI/CD or Docker-managed deployments), set `ENV_OVERRIDE=true` in your `.env` file. When enabled, the admin settings panel is read-only and displays a banner explaining that env vars are in control.

---

## Firewall

Port 5001 must **not** be open to the internet — it is bound to `127.0.0.1` in Docker, so only Apache on the same host can reach it. Ensure ports 80 and 443 are open for Apache:

```bash
# If using ufw on Ubuntu:
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 22/tcp
sudo ufw enable

# Confirm port 5001 is NOT listed in public rules:
sudo ufw status
```

---

## Managing the deployment

```bash
# View logs
docker compose logs -f psa-app
docker compose logs -f psa-sync

# Restart app after config changes
docker compose restart chatpsa

# Rebuild and restart after code changes
docker compose up -d --build

# Stop everything
docker compose down

# Force a manual sync
docker compose exec cw-sync python sync_cw_data.py --db /data/cw_data.db

# Check database size
docker compose exec chatpsa ls -lh /data/cw_data.db
```

---

## Updating the app

After pulling or making changes to the code:

```bash
# On the server: pull latest changes and rebuild
cd ~/ChatPSA
git pull
docker compose up -d --build
```

Or, if deploying from a local copy:

```bash
# From your workstation:
rsync -avz --exclude='venv' --exclude='*.db' --exclude='__pycache__' \
  ./ChatPSA/ user@your-server-ip:~/ChatPSA/

# On the server: rebuild and restart
cd ~/ChatPSA
docker compose up -d --build
```

---

## Troubleshooting

**"502 Bad Gateway" from Apache:**
The Flask app hasn't started yet, or the container is unhealthy. Check:
```bash
docker compose logs psa-app
docker compose ps
```

**OAuth redirect fails / wrong URL after login:**
Confirm the redirect URI registered in Entra ID exactly matches `https://your-domain.com/auth/callback` (https, no trailing slash). Also confirm `ProxyFix` is active — check that `app.py` contains the `ProxyFix` line and the container has been rebuilt with `docker compose up -d --build`.

**Chat requests time out:**
Each message makes two Claude API calls. If requests are failing after ~30 seconds, check that both `ProxyTimeout 120` and `Timeout 120` are set in the Apache VirtualHost.

**Sync fails with 500 errors:**
The CW API may be rate-limiting or temporarily unavailable. The sync retries automatically. Check:
```bash
docker compose logs psa-sync
```

**Database is empty after sync:**
Verify CW API credentials are correct:
```bash
docker compose exec cw-sync python -c "from sync_cw_data import load_config, api_get; c=load_config(); print(api_get(c, 'system/info'))"
```

**Changing the app port:**
The container listens on port 5001 internally, and the host-side port defaults to 5001 as well. If another service is already using that port, set `APP_PORT` in your `.env` file to a different value:
```
APP_PORT=5050
```
Then update the Apache VirtualHost to match:
```apache
ProxyPass        / http://127.0.0.1:5050/
ProxyPassReverse / http://127.0.0.1:5050/
```
Rebuild and reload:
```bash
docker compose up -d --build
sudo apachectl configtest
sudo systemctl reload apache2
```

**Certificate renewal:**
Certbot on the host auto-renews via systemd timer. Check renewal status:
```bash
sudo systemctl status certbot.timer
sudo certbot renew --dry-run
```
After renewal, reload Apache so it picks up the new cert:
```