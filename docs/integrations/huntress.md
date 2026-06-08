# Huntress Setup

ChatPSA can sync Huntress EDR organisation, agent, and incident data to provide endpoint coverage visibility and surface open incidents as anomalies.

## 1. Generate API Credentials

1. Sign in to the [Huntress Dashboard](https://huntress.io/)
2. Click the Navigation Hamburger Menu (3 lines) in the top right corner
3. Click API Credentials
4. Click the Setup API Credentials button under Account API Credentials
5. Click Generate
6. Note down the API Key and API Secret Key
7. Click Exit

> The API secret is only shown once. Store it securely.

## 2. Configure ChatPSA

You can add credentials to ChatPSA using either method — both are equivalent.

### Option A: Admin UI (recommended for existing deployments)

1. **Administration → Settings**
2. Find the **Huntress EDR** card
3. Enter your API Key and API Secret
4. Click **Save** on each field
5. The sync container detects the new credentials and starts syncing within 30 seconds

Use the **Test Connection** button to verify connectivity before waiting for the first sync.

### Option B: Environment file (for initial deployment)

Add the following to your `.env` file:

```env
HUNTRESS_API_KEY=your-api-key
HUNTRESS_API_SECRET=your-api-secret
```

Then restart the containers:

```bash
docker compose down && docker compose up -d
```

## 3. Verify

### Via the UI

1. On the **Huntress EDR** card in **Admin → Settings**, click **Test Connection**. A green result with the number of organisations found confirms the credentials are working.
2. Switch to the **Sync Status** tab — the Huntress card will appear once the first sync completes, showing row counts for organisations, agents, and incidents.
3. Try a question in the chat: *"Which clients have open Huntress incidents?"*

Open incidents will also appear as anomalies on the **Trends** page.

### Via the command line

```bash
docker compose logs -f psa-huntress-sync
```

You should see it pulling organisations, agents, and incident reports.

## Sync Schedule

The Huntress sync runs every **6 hours** around the clock. On first boot with an empty database, it waits 30 minutes to allow other syncs to settle first.

## Permissions

Huntress API credentials are read-only by default. ChatPSA accesses:

| Endpoint | Data |
|----------|------|
| `/organizations` | Organisation names and IDs |
| `/agents` | Endpoint agents per organisation |
| `/incident_reports` | Incident details, severity, and status |

No write operations are performed. See the [Huntress API documentation](https://api.huntress.io/docs) for details.
