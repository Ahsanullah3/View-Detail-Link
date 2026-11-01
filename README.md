# View.com.au Agent Scraper

Scrapes all agent + agency records from listing pages in `Sheet1!A2:A`
and writes them sequentially to `Sheet2` starting at `H2`.

## Setup

1. **Google Service Account**
   - Create a service account in Google Cloud Console.
   - Enable Google Sheets API.
   - Download the JSON key.
   - Share your Google Sheet with the service account email (Editor).

2. **GitHub Secrets** (Settings → Secrets → Actions):
   | Secret | Value |
   |--------|-------|
   | `GOOGLE_SA_JSON` | Full JSON key content (single line) |
   | `SHEET_ID` | The Google Sheet ID from its URL |
   | `GH_PAT` | Personal Access Token with `actions:write` + `contents:read` |

3. **Sheet layout**
   - `Sheet1!A2:A` — one listing URL per row.
   - `Sheet2` — auto-created if missing; headers written to `H1:W1`; data appended from `H2`.

## Run

- **Manual:** Actions tab → *Scrape Agents* → *Run workflow* (optionally set `start_row`).
- **Scheduled:** every 6 hours.
- **Chained:** each run processes up to 90 URLs, then dispatches the next run with the next `start_row`.

## Output columns (Sheet2, H→W)

`agent_id, first_name, last_name, position, email, mobile, phone,
average_sold_price, sold_listings, agency_id, agency_name,
agency_address, agency_phone, agency_website, source_url, error`

## Tuning

Edit constants at the top of `scraper.py`:
- `MAX_URLS_PER_RUN` (default 90)
- `CONCURRENCY` (default 3)
- `BATCH_PAUSE` (default 8s)

## Troubleshooting

- **All rows show `blocked_after_fallback`** → DataDome is blocking; verify `stealthkit` is installed, or add residential proxies.
- **Empty `agent_id` column** → page structure changed; run once and print `list(props.keys())` to find the correct array key.
- **Rows overwritten** → ensure `Sheet2` column H has no stale data below the intended write point; the script resumes from `len(col_H)+1`.
