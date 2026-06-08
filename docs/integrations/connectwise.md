# ConnectWise Manage Setup

ChatPSA requires a ConnectWise Manage API member to pull ticket, time entry, agreement, and company data. If this is the first time you are using the Connectwise API you will have to sign up from the [Connectwise Developer Registration Portal](https://register.developer.connectwise.com/)

## 1. Create an Security Role

1. In ConnectWise Manage, go to **System → Security Roles**
2. Click **+** to add a new role
3. Set **Role ID:** to **ChatPSA**
4. Save
5. Under **Companies** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
6. Under **Finance** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
7. Under **Procurement** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
8. Under **Project** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
9. Under **Sales** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
10. Under **Service Desk** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
11. Under **System** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
12. Under **Time & Expense** Set Inquire Level to All for all fields. Leave Add Level, Edit Level and Delete Level as None
13. Click Save

> This gives users of this role full read over your Connectwise Instance. These permissions are excessive and you could strip them down. Future updates to this application will seek to strip back the permissions documented.

>The Connectwise Syncing container touches the following endpoints;

| Endpoint                            | Data                                 | Function              |
| ----------------------------------- | ------------------------------------ | --------------------- |
| `service/tickets`                   | Service tickets (full + incremental) | `sync_tickets`        |
| `service/tickets/{id}/notes`        | Ticket notes (for timeline parsing)  | `sync_ticket_notes`   |
| `time/entries`                      | Time entries (full + incremental)    | `sync_time_entries`   |
| `finance/agreements`                | Agreements                           | `sync_agreements`     |
| `finance/agreements/{id}/additions` | Agreement line items/additions       | `sync_agreements`     |
| `finance/invoices`                  | Invoices (full + incremental)        | `sync_invoices`       |
| `company/companies`                 | Companies                            | `sync_companies`      |
| `company/contacts`                  | Contacts                             | `sync_contacts`       |
| `company/configurations`            | Configurations/assets                | `sync_configurations` |
| `system/members`                    | Members (active only)                | `sync_members`        |
| `project/projects`                  | Projects                             | `sync_projects`       |
| `procurement/catalog`               | Product catalog items                | `sync_catalog`        |
## 2. Create an API Member & Generate API Keys

1. In ConnectWise Manage, go to **System → Members**
2. Click the **API Members** tab
3. Click the **``+``** To add a new API Member
4. Set **Member ID** to ChatPSA
5. Set **Member Name** to ChatPSA
6. Set **Timezone** to your preferred timezone
7. Set notes to Read Only API connector for ChatPSA
8. From the **Role ID** dropdown select ``ChatPSA`` 
9. Click Save
10. Click the **API Keys** tab
11. Click the **``+``** button to add a new API key
12. Set **Description** to ChatPSA
13. Click Save
14. Record both **Public Key and Private Key** for later use.


## 3. Get a Client ID

ConnectWise requires a client ID for all API requests.

1. Go to the [ConnectWise Developer Portal](https://developer.connectwise.com/)
2. Sign in or create an account
3. Navigate to **API Keys** (or **My Account → API Keys**)
4. Create a new client ID — this is a GUID like `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`

## 4. Find Your API Site URL

Your CW API site URL depends on your region and if you are hosted or not:

| Region        | API URL                     |
| ------------- | --------------------------- |
| North America | `api-na.myconnectwise.net`  |
| Europe        | `api-eu.myconnectwise.net`  |
| Australia     | `api-aus.myconnectwise.net` |

If you use an on-premise ConnectWise server, use your server's hostname, no API subdomain is required.

## 5. Configure ChatPSA

ConnectWise is the only required integration and must be configured before the first deployment. Add the credentials to your `.env` file:

```env
CW_SITE=api-na.myconnectwise.net
CW_COMPANY_ID=yourcompany
CW_PUBLIC_KEY=your-public-key
CW_PRIVATE_KEY=your-private-key
CW_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

After the initial deployment, these credentials can also be viewed and updated from **Admin → Settings** in the web UI without redeploying.

## 6. Verify

### Via the UI

1. Once the app is running, go to **Admin → Settings** and find the **ConnectWise Manage** card. Click **Test Connection** — a green result confirms the API credentials are valid.
2. Switch to the **Sync Status** tab — the ConnectWise card will show row counts for tickets, companies, time entries, etc. once the initial sync completes.
3. Try a question in the chat: *"How many tickets were opened this week?"*

### Via the command line

```bash
docker compose logs -f psa-sync
```

You should see it pulling companies, tickets, time entries, etc. You can also verify API connectivity directly:

```bash
docker compose exec chatpsa python -c "
from sync_cw_data import load_config, api_get
c = load_config()
print(api_get(c, 'system/info'))
"
```

## Sync Schedule

The ConnectWise sync runs every **60 minutes** during business hours (Mon–Fri, 6am–7pm in your configured timezone). On first boot with an empty database, it performs a full sync after a 5-minute delay.

Subsequent syncs are incremental — only tickets and time entries modified in the last 7 days are updated.

To trigger a manual sync:

```bash
docker compose exec chatpsa python sync_cw_data.py --db /data/cw_data.db
```
