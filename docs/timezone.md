# Timezone Configuration

ChatPSA uses IANA timezone names (e.g. `Australia/Brisbane`, `America/New_York`) to handle date and time throughout the application — in chat queries, analytics, trend detection, and sync scheduling.

## Setting Your Timezone

### Option A: Admin UI (recommended)

1. Go to **Admin → Settings**
2. Find the **Timezone** field
3. Enter your IANA timezone name (e.g. `Australia/Brisbane`)
4. Click **Save**

Changes take effect after the containers are restarted (`docker compose down && docker compose up -d`).

### Option B: Environment file

Set `APP_TZ_NAME` in your `.env` file:

```env
APP_TZ_NAME=Australia/Brisbane
```

## Common Timezone Names

| Region | IANA Name | UTC Offset |
|--------|-----------|------------|
| Australia Eastern | `Australia/Brisbane` | UTC+10 |
| Australia Eastern (DST) | `Australia/Sydney` | UTC+10 / UTC+11 |
| US Eastern | `America/New_York` | UTC-5 / UTC-4 |
| US Central | `America/Chicago` | UTC-6 / UTC-5 |
| US Pacific | `America/Los_Angeles` | UTC-8 / UTC-7 |
| UK | `Europe/London` | UTC+0 / UTC+1 |
| Central Europe | `Europe/Berlin` | UTC+1 / UTC+2 |
| New Zealand | `Pacific/Auckland` | UTC+12 / UTC+13 |
| India | `Asia/Kolkata` | UTC+5:30 |
| UTC (no DST) | `UTC` | UTC+0 |

A full list of IANA timezone names is available at [Wikipedia: List of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones).

## Daylight Saving Time (DST)

If your timezone observes DST (e.g. `Australia/Sydney`, `America/New_York`), ChatPSA automatically adjusts the UTC offset at each DST transition. This means:

- **Queries**: "tickets opened today" always uses the correct local date, even during DST transitions
- **Analytics**: hourly and daily charts reflect the real local time
- **Trends**: anomaly detection windows align with local business hours

**If your timezone does NOT observe DST** (e.g. `Australia/Brisbane`, `UTC`), the offset is constant year-round and you won't notice any difference.

### DST Edge Cases

During DST transitions, queries that span the changeover boundary may count a small number of records in the "wrong" day. For example, if clocks spring forward at 2:00 AM, a ticket logged at 1:30 AM UTC might appear under a slightly different local date depending on whether the offset was computed before or after the transition. In practice this affects at most 1–2 records per transition and is unlikely to change any business conclusions.

## Legacy: Numeric UTC Offset

Older deployments may use `APP_TZ_OFFSET` (a numeric UTC offset like `10` for UTC+10). This is still supported as a fallback:

```env
# Legacy — does NOT handle DST
APP_TZ_OFFSET=10
```

If both `APP_TZ_NAME` and `APP_TZ_OFFSET` are set, `APP_TZ_NAME` takes priority. If neither is set, the default is `Australia/Brisbane`.

We recommend migrating to `APP_TZ_NAME` for accurate DST handling.
