# CIPP / Microsoft 365 Setup

ChatPSA can pull Microsoft 365 tenant, licence, and alert data via your existing [CIPP](https://cipp.app/) instance.

## 1. Generate Your CIPP API Credentials

1. In CIPP, go to **CIPP → Integrations → cipp**
2. From the Actions drop down under Settings → Function Authentication select Create New Client
3. Under App Name: ChatPSA
4. Choose  ```readonly``` from the Select Role dropdown
5. Enter the IP Address that your application will be using in Enter IP Ranges
6. Toggle Enable this client
7. Confirm
8. Click the Copy symbol in the API Client created notice and save it for later
9. Click Close
10. Note the following values:
   - **Application (client) ID** - `CIPP_CLIENT_ID`
   - **Client Secret** - `CIPP_CLIENT_SECRET`
   - **Tenant ID** - `CIPP_TENANT_ID`
   - **API Url** - `CIPP_API_URL`


> **Note:** ChatPSA calls the CIPP API endpoints, not the Microsoft Graph API directly. CIPP handles all the delegated tenant access internally.

## 2. Configure ChatPSA

You can add credentials using either method — both are equivalent.

### Option A: Admin UI (recommended for existing deployments)

1. Go to **Admin → Settings**
2. Find the **CIPP / Microsoft 365** card
3. Enter your API URL, Client ID, Client Secret, and Tenant ID
4. Click **Save** on each field
5. The sync container detects the new credentials and starts syncing within 30 seconds

Use the **Test Connection** button to verify connectivity before waiting for the first sync.

### Option B: Environment file (for initial deployment)

Add the following to your `.env` file:

```env
CIPP_API_URL=https://your-cipp-instance.azurewebsites.net
CIPP_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
CIPP_CLIENT_SECRET=your-client-secret
CIPP_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

Then restart the containers:

```bash
docker compose down && docker compose up -d
```

### Optional Variables

| Variable | Description |
|----------|-------------|
| `CIPP_TOKEN_URL` | Full OAuth2 token endpoint URL. Overrides the URL derived from `CIPP_TENANT_ID`. Use if your token endpoint is non-standard. |
| `CIPP_SCOPE` | OAuth2 scope. Defaults to `{CIPP_CLIENT_ID}/.default` — you rarely need to set this. |
| `CIPP_SYNC_POLICIES` | Set to `true` to also sync M365 security and compliance policies (conditional access, Intune, transport rules, etc.). Disabled by default as it significantly increases sync time. |

## 3. Verify

### Via the UI

1. On the **CIPP / Microsoft 365** card in **Admin → Settings**, click **Test Connection**. A green result with the number of tenants found confirms the credentials are working.
2. Switch to the **Sync Status** tab — the CIPP card will appear once the first sync completes, showing row counts for tenants, licences, and alerts.
3. Try a question in the chat: *"Which M365 licences are unassigned?"*

Unused licences will also appear as anomalies on the **Trends** page.

### Via the command line

```bash
docker compose logs -f psa-cipp-sync
```

You should see it authenticating and pulling tenant data, licences, and alerts.

## Sync Schedule

The CIPP sync runs every **4 hours** during business hours (Mon–Fri, 6am–7pm). On first boot with an empty database, it waits 10 minutes before the initial sync to allow the ConnectWise sync to complete first.

## Data Pulled

| CIPP Endpoint | Data |
|---------------|------|
| `ListTenants` | Tenant names and IDs |
| `ListLicenses` | Licence SKUs, quantities, and assignments per tenant |
| `ExecAlertsList` | Active alerts per tenant |

When `CIPP_SYNC_POLICIES=true`, the sync additionally pulls 19 types of security and compliance policies per tenant (conditional access, Intune policies, spam filters, safe links, etc.).
