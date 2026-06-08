# Administration

The **Admin** page is available to users with the admin feature permission. It provides five tabs for managing users, configuring the application, monitoring data sync, reviewing usage analytics, and linking companies across services.

Access the admin panel from the **Admin** link in the navigation bar. If you don't see it, an existing admin needs to grant you the **admin** feature from the Permissions tab.

---

## Permissions

Controls which features each user can access.

### User table

Every user who has signed in via Azure AD (or been seeded from ConnectWise members) appears in the table. Each row shows the user's name, email, and a toggle for each available feature:

- **Admin** — access to this administration page and the analytics dashboard
- **Timeline** — access to the Timeline page

Toggling a permission takes effect on the user's next page load. A "Saving..." indicator confirms the change was persisted.

### Hidden users

Users seeded from ConnectWise who haven't signed in can clutter the list. Click **Show hidden users** to reveal them, or hide them individually to keep the table focused on active users.

### First-user bootstrap

On a fresh deployment with no permissions in the database, the first user to sign in is automatically granted all features (admin + timeline). This person becomes the initial admin and can then grant access to others. See [Authentication](authentication.md) for details.

---

## Settings

All configurable application settings are managed here, grouped into cards. Changes take effect within 30 seconds across all workers — no restart or redeployment required.

### General settings

Core settings that control the application's identity and behaviour:

| Setting | Env var | Description |
|---------|---------|-------------|
| App Name | `APP_NAME` | Display name shown in the UI and the AI system prompt |
| Documentation URL | `DOCS_BASE_URL` | Base URL for contextual documentation links (see [Documentation Links](#documentation-links) below) |
| Helpdesk Board | `HELPDESK_BOARD` | Primary ConnectWise board used for anomaly detection |
| Timezone | `APP_TZ_NAME` | IANA timezone name (e.g. Australia/Brisbane). Handles DST automatically. |
| Claude Model | `CLAUDE_MODEL` | Which Anthropic model to use for chat queries |
| Max Tokens | `CLAUDE_MAX_TOKENS` | Maximum token length per AI response (100–32,000) |
| Exclude Companies | `TRENDS_EXCLUDE_COMPANIES` | Companies to exclude from trend/anomaly queries |
| High Priorities | `TRENDS_HIGH_PRIORITIES` | Priority names that trigger the stale-priority anomaly |

### Feature settings

Non-credential settings grouped by feature (e.g., Timeline). These control feature-specific behaviour like whether AI summaries are generated or which boards the timeline parser scans.

### Integration credentials

Each integration (CIPP, Duo, Huntress, ThreatLocker) has its own card showing all required credentials. A status indicator shows whether the integration is **Configured** (all credentials set) or **Not configured** (one or more missing).

Each integration card includes a **Test Connection** button that makes a live API call to verify the credentials work. The result is displayed inline — a success message with details (e.g., tenant count) or an error with the failure reason.

### ENV_OVERRIDE mode

If `ENV_OVERRIDE=true` is set in the `.env` file, the settings page becomes read-only. A banner explains that environment variables are in control, and all input fields are disabled. This is useful for CI/CD or Docker-managed deployments where the `.env` file should be the single source of truth.

### Validation

Settings with validation rules (like numeric ranges or required formats) are checked client-side before saving. Invalid values show an error message below the field and the save button is disabled until corrected.

### Documentation links

Setting the **Documentation URL** enables contextual "Learn more" links throughout the admin UI. When configured, ChatPSA resolves relative documentation paths against this base URL to create direct links to the relevant section of your documentation.

To enable it, set the URL to the raw-browsable root of your repository. For a GitHub-hosted fork:

```
https://github.com/yourorg/ChatPSA/blob/main
```

This produces links like `https://github.com/yourorg/ChatPSA/blob/main/docs/authentication.md#setup`, which GitHub renders as formatted markdown with the page scrolled to the right heading.

Once configured, documentation links appear in these locations:

- **Settings fields** — an "ℹ️ Learn more" link appears next to settings that have associated documentation (e.g., Azure credentials, timezone, secret expiry)
- **Secret expiry banner** — the warning banner shown to admins when a client secret is expiring or has failed authentication includes a "Documentation" link alongside "Manage settings"
- **ENV_OVERRIDE banner** — the banner shown on the Settings page when ENV_OVERRIDE is active includes a "Learn more" link to the ENV_OVERRIDE documentation

If the Documentation URL is left blank, these links are simply hidden — no broken links or empty hrefs.

---

## Sync Status

Monitors data synchronisation across all connected services.

### Sync cards

Each data source gets a status card showing:

- **Last sync time** — when the most recent sync completed, displayed as a relative time (e.g., "12 minutes ago")
- **Row counts** — how many records are in each table for that source (e.g., tickets, time entries, companies)
- **Status indicator** — whether the sync is healthy, stale, or has never run

Cards only appear for services that have credentials configured. If no integrations are set up, the tab shows a message directing you to the Settings tab.

### Auto-refresh

The sync status polls the server automatically and updates in real time. A pulsing indicator shows when a refresh is in progress.

---

## Analytics

An embedded usage dashboard showing how the team uses ChatPSA. This is a compact version of the standalone [Analytics](features.md#analytics) page, focused on the metrics most relevant to administrators.

The admin analytics tab includes:

- **Daily activity chart** — 30-day bar chart of query volume
- **Hourly distribution** — which hours of the day see the most queries
- **Top users** — who's using the system the most
- **Recent queries** — a scrollable list of the latest queries with timestamps and user names

For the full analytics dashboard with response times, error rates, query themes, and page traffic, see the standalone Analytics page (also requires admin access).

---

## Customer Mappings

Links ConnectWise companies to their accounts in other connected services (CIPP/M365, Duo, Huntress, ThreatLocker). Correct mappings are essential for the Trends page to show cross-service anomalies on the right company cards.

### How it works

Each row represents a ConnectWise company. Columns appear for each configured integration, showing whether a mapping exists. The system uses fuzzy name matching to suggest likely matches, which you can accept or dismiss.

### Toolbar

- **Search** — filter companies by name
- **Status filters** — show All, Unmapped, Partial (some services mapped), Mapped (all services mapped), or Ignored companies
- **Service filters** — filter to companies with a specific service unmapped
- **Company count** — shows how many companies match the current filters

### Bulk actions

- **Auto-map Exact Matches** — automatically links companies where the ConnectWise name exactly matches the service account name (case-insensitive). Only appears when unmapped companies exist.
- **Accept All Visible** — accepts all suggested matches currently shown on screen
- **Dismiss All Visible** — dismisses all suggestions for visible companies

### Ignoring companies

Companies that don't need mappings (e.g., vendors, one-off clients) can be marked as ignored. Ignored companies are hidden from the default view and excluded from the "unmapped" count.

### When mappings matter

Mappings are used by the Trends page to correlate data across services. For example, if a ConnectWise company is mapped to a CIPP tenant, the Trends page can show unused M365 licences on that company's anomaly card alongside ticket spikes. Without the mapping, cross-service anomalies display separately with no company association.

### Tab visibility

The Mappings tab only appears when at least one integration besides ConnectWise is configured. If only ConnectWise is set up, there's nothing to map and the tab is hidden.

