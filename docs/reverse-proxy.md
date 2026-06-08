# Reverse Proxy Setup

ChatPSA binds to `127.0.0.1:5001` by default — it is not directly accessible from the network. A reverse proxy handles HTTPS termination, header forwarding, and public-facing access.

If you don't need remote access, skip this entirely — just open `http://localhost:5001` in your browser.

## Key Requirements

Regardless of which proxy you use, two things are critical:

1. **Forward the `X-Forwarded-Proto` header** — ChatPSA uses this to construct correct OAuth callback URLs. Without it, Azure AD redirects will fail with protocol mismatches.

2. **Set the proxy timeout to at least 120 seconds** — each chat message makes one or two Claude API calls that can take up to a minute. Default proxy timeouts (30–60s) will cut off responses mid-generation.

## Nginx

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your-domain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:5001;

        # Required headers
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Timeout for Claude API calls
        proxy_read_timeout 120s;
        proxy_send_timeout 120s;
    }
}
```

## Apache

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

    ProxyPreserveHost On
    ProxyPass        / http://127.0.0.1:5001/
    ProxyPassReverse / http://127.0.0.1:5001/

    # Required headers
    RequestHeader set X-Forwarded-Proto "https"
    RequestHeader set X-Forwarded-For   "%{REMOTE_ADDR}s"

    # Timeout for Claude API calls
    ProxyTimeout 120
    Timeout      120
</VirtualHost>
```

Required Apache modules:

```bash
sudo a2enmod proxy proxy_http rewrite headers ssl
sudo apachectl configtest
sudo systemctl reload apache2
```

For a full step-by-step Apache deployment on Ubuntu, see [DEPLOY.md](../DEPLOY.md).

## Caddy

Caddy is the simplest option — it handles HTTPS automatically via Let's Encrypt with zero configuration:

```
your-domain.com {
    reverse_proxy 127.0.0.1:5001 {
        transport http {
            read_timeout 120s
        }
    }
}
```

That's the entire config. Caddy provisions and renews TLS certificates automatically.

Install and run:

```bash
sudo apt install -y caddy
# Place the config above in /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## Changing the Port

If port 5001 conflicts with another service, set `APP_PORT` in your `.env`:

```env
APP_PORT=5050
```

Then update your proxy config to point to the new port and rebuild:

```bash
docker compose down && docker compose up -d
```

## Verifying Headers

After setting up the proxy, verify that ChatPSA sees the correct forwarded headers:

```bash
docker compose logs psa-app | grep "Forwarded"
```

If OAuth callbacks fail, the most common cause is a missing `X-Forwarded-Proto` header — ChatPSA constructs the callback URL based on this, and Azure AD will reject mismatches between `http` and `https`.

## Firewall

Port 5001 should **not** be open to the internet. It is bound to `127.0.0.1` in Docker, so only the local proxy can reach it. Ensure only ports 80 and 443 are publicly accessible:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 22/tcp
sudo ufw enable
```
