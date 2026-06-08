# Authentication

ChatPSA supports optional single sign-on via Microsoft Entra ID (Azure AD). When enabled, users sign in with their Microsoft 365 account.

## Two Modes

**With authentication (recommended for production):** Set all three Azure variables. Users are redirected to Microsoft login, and only members of your tenant can access the application.

**Without authentication (local/development):** If any Azure variable is missing, authentication is disabled entirely. All routes are publicly accessible. The UI shows a warning banner in this mode.

> **Important:** ChatPSA has no built-in username/password system. If you run without Azure AD, restrict access at the network level (VPN, firewall, SSH tunnel). Never expose an unauthenticated instance to the public internet.

## Setup

### 1. Register an App in Azure

1. Go to [https://entra.microsoft.com](https://entra.microsoft.com)
2. Navigate to **Identity → Applications → App registrations**
3. Click **New registration**
   - **Name:** `ChatPSA` (or your chosen `APP_NAME`)
   - **Supported account types:** Accounts in this organizational directory only (single tenant)
   - **Redirect URI:** Web → `https://your-domain.com/auth/callback`
4. Click **Register**

### 2. Collect the Values

On the app's **Overview** page, copy:

- **Application (client) ID** → `AZURE_CLIENT_ID`
- **Directory (tenant) ID** → `AZURE_TENANT_ID`

### 3. Create a Client Secret

1. Go to **Certificates & secrets → New client secret**
2. Description: `ChatPSA`
3. Expiry: 24 months (set a calendar reminder to rotate before expiry)
4. Copy the **Value** (not the Secret ID) → `AZURE_CLIENT_SECRET`

### 4. Verify API Permissions

Under **API permissions**, ensure `User.Read` (Microsoft Graph, Delegated) is present. This is the only permission required — it reads the user's display name and email for session identity.

**Optional:** To enable automatic client-secret expiry warnings (see [Secret Expiry Monitoring](#secret-expiry-monitoring) below), also add `Application.Read.All` (Microsoft Graph, **Application** type — not Delegated) and click **Grant admin consent**.

### 5. Configure ChatPSA

Add to your `.env` file:

```env
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_CLIENT_SECRET=your-secret-value
AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Rebuild and restart:

```bash
docker compose down && docker compose up -d
```

## Restricting Access to Specific Users

By default, every user in your Azure AD tenant can sign in. To limit access:

1. In [Entra admin centre](https://entra.microsoft.com), go to **Identity → Applications → Enterprise applications**
2. Find and select your ChatPSA app
3. Go to **Properties** and set **Assignment required?** to **Yes**, then **Save**
4. Go to **Users and groups → Add user/group**
5. Select the users or security groups that should have access

Anyone not assigned will be blocked with an `AADSTS50105` error at the Microsoft login screen.

> **Tip:** Create a security group (e.g. `ChatPSA Users`) and assign the group instead of individual users. Then manage access by adding/removing group members.

## First-User Auto-Admin

On a fresh deployment, the **first user to sign in** is automatically granted all feature access (admin, timeline, analytics). No manual SQL or configuration needed — just sign in and you'll see the Admin page.

After that, use **Admin → Permissions** to grant or revoke access for other users.

## Feature-Based Access Control

ChatPSA uses feature flags rather than role hierarchies. Available features:

| Feature | Controls Access To |
|---------|--------------------|
| `admin` | Admin settings, permissions, sync status, customer mapping |
| `timeline` | Timeline view |

Users without a specific feature simply don't see that navigation item. The main chat interface is available to all authenticated users.

## Secret Expiry Monitoring

Azure client secrets have expiry dates. When a secret expires, authentication and integrations silently break. ChatPSA tracks expiry dates and shows a warning banner to admin users before it's too late.

Warning thresholds:

- **30+ days remaining** — no warning
- **8–30 days remaining** — yellow warning banner
- **1–7 days remaining** — red critical banner
- **Expired** — red expired banner

The check runs once per browser session (cached server-side for 6 hours) and only shows to admin users. The banner is dismissible per session.

### Option 1: Manual Date (No Extra Permissions)

Enter the expiry date in **Admin → Settings → Secret Expiry Tracking**. Azure shows the expiry date when you create a client secret — copy it into the field.

This works out of the box with no additional Azure configuration. Remember to update the date whenever you rotate a secret.

### Option 2: Auto-Detection via Graph API (Recommended)

ChatPSA can query the Microsoft Graph API to read the expiry date directly from the app registration. When auto-detection succeeds, the detected date is shown in the Admin settings field automatically. You can save it to persist the value, or override it with a manual date.

When multiple secrets exist on the app registration (e.g. during rotation), ChatPSA matches the `hint` field against the active `AZURE_CLIENT_SECRET` to identify which credential is in use.

**Important:** The required permission, `Application.Read.All`, grants read access to **all** app registrations in your Azure tenant — not just ChatPSA's. It is read-only (cannot modify anything), but you should be aware of the scope before granting it.

To enable auto-detection:

1. In [Entra admin centre](https://entra.microsoft.com), go to **Identity → Applications → App registrations**
2. Select your ChatPSA app
3. Go to **API permissions → Add a permission**
4. Select **Microsoft Graph → Application permissions**
5. Search for `Application.Read.All` and check it
6. Click **Add permissions**
7. Click **Grant admin consent for [your tenant]** (requires Global Admin or Privileged Role Administrator)
8. Restart ChatPSA

If the permission is not granted (or is later revoked), ChatPSA silently falls back to the manual date with no errors.

### Verifying It Works

Check the expiry status via the admin API:

```
GET /api/admin/secret-expiry
```

Each entry includes a `source` field (`"graph"` or `"manual"`) so you can confirm which mode is active.

## Rotating a Client Secret

Azure client secrets have expiry dates (typically 12 or 24 months). ChatPSA supports updating the secret through the Admin UI without rebuilding the container.

### Normal Rotation (Before Expiry)

1. In [Entra admin centre](https://entra.microsoft.com), create a new client secret on your app registration
2. In ChatPSA, go to **Admin → Settings → Authentication**
3. Paste the new secret value into **Azure Client Secret** and click **Save**
4. ChatPSA tests the new secret against Azure AD before saving — if the secret is invalid, the save is rejected with an error message, so typos are caught immediately
5. On success, the previous secret is automatically saved for rollback (see below)
6. Delete the old secret from Azure once you've confirmed the new one works

### Emergency Recovery (Secret Already Expired)

If the secret has expired and no one can log in:

1. In Azure, create a new client secret
2. Update your `.env` file with the new secret:
   ```env
   AZURE_CLIENT_SECRET=your-new-secret-value
   ENV_OVERRIDE=true
   ```
3. Restart the container: `docker compose down && docker compose up -d`
4. ChatPSA writes the env values into the database on startup
5. Log in and verify everything works
6. Remove `ENV_OVERRIDE=true` from `.env` and restart again
7. From now on, manage the secret through Admin → Settings

### Previous Secret Rollback

When you update the client secret through Admin → Settings, ChatPSA automatically saves the old value in the **Previous Client Secret** field. While the save-time validation catches most errors, this provides an additional safety net:

1. Go to **Admin → Settings → Authentication**
2. Copy the value from **Previous Client Secret**
3. Paste it into **Azure Client Secret** and save

The previous secret is stored (masked) in the Admin UI so you can always roll back to the last working value.

### Credential Validation

ChatPSA validates Azure credentials at three points to catch configuration errors as early as possible:

**Save-time validation (Admin UI):** When you save a new client secret through Admin → Settings, ChatPSA tests it against Azure AD before writing it to the database. If the secret is invalid, the save is rejected with a specific error message. This catches typos and copy-paste errors immediately.

**Startup validation (container logs):** Every time ChatPSA starts, it tests the configured credentials against Azure AD. If authentication fails, an `ERROR` level message is written to the container logs with the specific Azure AD error code and a remediation hint. This catches problems introduced via `.env` or ENV_OVERRIDE, where there is no UI to provide immediate feedback.

**Banner warning (nav bar):** When an admin user loads any page, ChatPSA checks credential health in the background. If the configured secret fails authentication, a red banner appears: *"Azure authentication failed: The configured client secret failed authentication. Users cannot log in."* This is separate from the expiry warning banner and takes priority over it — a broken secret is more urgent than an expiring one.

All three validation paths use the same fail-open approach for network errors: if Azure AD is unreachable (DNS not ready, transient outage), validation is skipped rather than blocking the operation. Only confirmed authentication failures are treated as errors.

The banner warning includes a link to the relevant documentation section when **Documentation URL** is configured in Admin → Settings. See [Documentation Links](administration.md#documentation-links) for setup instructions.

## ENV_OVERRIDE Mode

Setting `ENV_OVERRIDE=true` in your `.env` file changes how settings work:

- On startup, environment variables are **written into the database**, overwriting existing values
- Variables that are **missing or commented out** are skipped — the database value is preserved
- Variables set to **empty** (`VAR=` or `VAR=""`) write an empty string, effectively clearing that setting
- During the session, environment variables that are **present** take precedence over database values. Settings without a corresponding env var continue to read from the database
- The Admin Settings page shows all fields as disabled with a banner explaining the override

**Startup validation:** When credentials are loaded from environment variables, ChatPSA tests them against Azure AD at startup. If the secret is invalid, an `ERROR` level log message is written to the container logs. The nav banner also shows a red "Azure authentication failed" warning for admin users.

This is useful for:

- **Emergency recovery** — push new credentials via `.env` when you can't access the Admin UI
- **Config reset** — set all your values in `.env`, enable override, restart, then disable override
- **CI/CD pipelines** — manage settings via environment files in automated deployments

## Session and Security

- Sessions use Flask's signed cookie with `FLASK_SECRET_KEY`
- When Azure auth is enabled, `SESSION_COOKIE_SECURE` is set to `True` (requires HTTPS)
- `SESSION_COOKIE_HTTPONLY` and `SESSION_COOKIE_SAMESITE=Lax` are always enforced
- HSTS headers are automatically added when Azure auth is active

## Redirect URI Notes

- The redirect URI must exactly match what's registered in Azure: `https://your-domain.com/auth/callback`
- If running both production and staging, add both callback URLs under **Authentication → Redirect URIs** in the same app registration
- The app uses `X-Forwarded-Proto` from your reverse proxy to construct the correct callback URL — ensure your proxy sets this header (see [Reverse Proxy Setup](reverse-proxy.md))

## Troubleshooting Authentication

### "Azure authentication failed" Banner

If you see a red banner saying *"Azure authentication failed"*, the configured client secret is not valid. Users will not be able to log in until this is resolved.

**If you can access the Admin UI** (you're already logged in):

1. Go to **Admin → Settings → Authentication**
2. Verify the Client ID and Tenant ID match your Azure app registration
3. Create a new client secret in [Entra admin centre](https://entra.microsoft.com) and paste it into the Azure Client Secret field
4. ChatPSA will validate the secret before saving — you'll see a success or error immediately

**If you cannot access the Admin UI** (the secret expired while you were logged out):

Follow the [Emergency Recovery](#emergency-recovery-secret-already-expired) steps — create a new secret in Azure, set it via `.env` with `ENV_OVERRIDE=true`, and restart the container.

### Common Azure AD Error Codes

| Error Code | Meaning | Fix |
|------------|---------|-----|
| `AADSTS7000215` | Invalid client secret | The secret value is wrong. Create a new secret in Azure and copy the **Value** (not the Secret ID). |
| `AADSTS700016` | Application not found | The Client ID doesn't match any app registration in this tenant. Check `AZURE_CLIENT_ID`. |
| `AADSTS90002` | Tenant not found | The Tenant ID is invalid. Check `AZURE_TENANT_ID`. |
| `AADSTS50105` | User not assigned | The user exists but isn't assigned to the app. See [Restricting Access](#restricting-access-to-specific-users). |
| `AADSTS65001` | Consent not granted | The app needs admin consent for its API permissions. Grant consent in Entra admin centre. |

### Startup Log Errors

When credentials fail at startup, ChatPSA logs a message like:

```
ERROR — AZURE CREDENTIAL VALIDATION FAILED — the configured client secret
did not authenticate. Users will not be able to log in.
Error: invalid_client — AADSTS7000215: ...
Fix: update AZURE_CLIENT_SECRET in .env or Admin > Settings.
```

Check container logs with:

```bash
docker compose logs web | grep "CREDENTIAL VALIDATION"
```

If you see *"Azure credential validation skipped — could not reach Azure AD"* instead, this is a transient network issue at startup and is usually safe to ignore. The credentials will be tested again when a user tries to log in.
