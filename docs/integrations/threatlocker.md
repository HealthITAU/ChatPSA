# ThreatLocker Setup

ChatPSA can sync ThreatLocker endpoint inventory across all child organisations to surface unsecured endpoints and deny spikes as anomalies.

## 1. Generate a Portal API Key

1. Sign in to the [ThreatLocker Portal](https://portal.threatlocker.com/)
2. Navigate to your **parent organisation** (the top-level MSP account)
3. Go to **Users → API Users** (or the equivalent in your portal version)
4. Create a new API user.
5. Give it a name like **ChatPSA**
6. Select the Administrator Role from the Roles dropdown in Roles/Permissions
7. Leave Organization set to All Organizations and click the + button
8. Check the Accept EULA checkbox
9. Click Save
10. Refresh your API Users list.
11. Click Edit on your ChatPSA@{uuid}
12. Click Reset API Token under API Token
13. Record the Token for use shortly

> The key must be from the **parent** organisation level to cover all child organisations. A child-level key will only return data for that single organisation.

>These permissions represent permissions that ensure that the relevant data is able to be pulled, you might find that you can create roles with lesser permissions that work just fine as we only need read over organizations and computers. Please feel free to test with restricted permissions after you succeed with Administrator ones.
## 2. Configure ChatPSA

You can add the credential using your preferred method.

### Option A: Admin UI (recommended for existing deployments)

1. Go to **Admin → Settings**
2. Find the **ThreatLocker** card
3. Enter your API Key
4. Click **Save**
5. The sync container detects the new credential and starts syncing within 30 seconds

Use the **Test Connection** button to verify connectivity before waiting for the first sync.

### Option B: Environment file (for initial deployment)

Add the following to your `.env` file:

```env
THREATLOCKER_API_KEY=your-api-key
```

Then restart the containers:

```bash
docker compose down && docker compose up -d
```

## 3. Verify

### Via the UI

1. On the **ThreatLocker** card in **Admin → Settings**, click **Test Connection**. A green result with the number of organisations and endpoints found confirms the credential is working.
2. Switch to the **Sync Status** tab — the ThreatLocker card will appear once the first sync completes, showing the number of synced endpoints and organisations.
3. Try a question in the chat: *"Which endpoints aren't in ThreatLocker Secure mode?"*

Two types of anomalies appear on the **Trends** page:

- **Unsecured Endpoints** — computers not in Secure mode (in Application Control Learning Mode or Monitor Only)
- **Deny Spikes** — organisations with unusually high deny counts compared to their weekly average

### Via the command line

```bash
docker compose logs -f psa-threatlocker-sync
```

You should see it pulling computer and organisation data.

## Sync Schedule

The ThreatLocker sync runs every **6 hours** around the clock. On first boot with an empty database, it waits 25 minutes to allow other syncs to settle first.

## How It Works

ChatPSA calls two ThreatLocker API endpoints:

| Endpoint | Data |
|----------|------|
| `Computer/ComputerGetByAllParameters` | Endpoint inventory with mode, deny counts, and OS info (with `childOrganizations: true` to include all children) |
| `Organization/OrganizationGetChildOrganizationsByParameters` | Organisation hierarchy |

Both are POST requests with pagination. The API key is sent directly in the `Authorization` header (no `Bearer` or `Basic` prefix — just the raw key).

## Permissions

ChatPSA only reads computer and organisation data. No changes are made to ThreatLocker policies, applications, or settings.
