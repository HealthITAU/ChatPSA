# Duo Security Setup

ChatPSA can sync MFA user enrolment and status from Duo Security, covering all child accounts under your MSP's parent Duo organisation.

## Prerequisites

- A Duo MSP (multi-tenant) account
- Administrative access to the parent Duo account

## 1. Create an Accounts API Application

ChatPSA uses the **Accounts API** at the MSP parent level — not a per-child Admin API application. This single set of credentials covers all child accounts.

1. Sign in to the [Duo Admin Panel](https://admin.duosecurity.com/) using your **parent MSP** account
2. Go to **Applications → Application**
3. Search for **Accounts API** and click **Protect**
4. Click the  **+ Add Application** button in Configured applications
5. Search for and Click  **+ Add** on Admin Api
6. Set Application name to ChatPSA
7. Note the following:
   - **Integration key** - `DUO_IKEY`
   - **Secret key** - `DUO_SKEY`
   - **API hostname** - `DUO_HOST` (e.g. `api-XXXXXXXX.duosecurity.com`)
8. Under **Settings** -permissions, grant the following:
   - **Grant read information**
   - **Grant read log**
	   - Read - Under **Grant Resource**

> **Important:** Use the *parent* account's Accounts API, not a child account's Admin API. The Accounts API lists all child accounts and can read user data across them.

## 2. Configure ChatPSA

You can add credentials using either method — both are equivalent.

### Option A: Admin UI (recommended for existing deployments)

1. Go to **Admin → Settings**
2. Find the **Duo Security** card
3. Enter your Integration Key, Secret Key, and API Hostname
4. Click **Save** on each field
5. The sync container detects the new credentials and starts syncing within 30 seconds

Use the **Test Connection** button to verify connectivity before waiting for the first sync.

### Option B: Environment file (for initial deployment)

Add the following to your `.env` file:

```env
DUO_IKEY=your-integration-key
DUO_SKEY=your-secret-key
DUO_HOST=api-XXXXXXXX.duosecurity.com
```

Then restart the containers:

```bash
docker compose down && docker compose up -d
```

## 3. Verify

### Via the UI

1. On the **Duo Security** card in **Admin → Settings**, click **Test Connection**. A green result with the number of child accounts found confirms the credentials are working.
2. Switch to the **Sync Status** tab — the Duo card will appear once the first sync completes, showing the number of synced users and child accounts.
3. Try a question in the chat: *"Which users are in Duo bypass mode?"*

Users in bypass mode will also appear as anomalies on the **Trends** page.

### Via the command line

```bash
docker compose logs -f psa-duo-sync
```

You should see it listing child accounts and pulling user data from each.

## Sync Schedule

The Duo sync runs every **6 hours** (around the clock, not limited to business hours). On first boot with an empty database, it waits 20 minutes to allow other syncs to settle first.

## How It Works

1. ChatPSA calls the parent Accounts API to list all child accounts
2. For each child account, it reads user data using the parent's credentials with the child's `account_id`
3. User enrolment status, last login, bypass status, and phone registration are stored locally
