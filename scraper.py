import asyncio
import json
import os
import random
import re
import time

import gspread
import requests
from curl_cffi.requests import AsyncSession

MAX_URLS_PER_RUN = 90
CONCURRENCY = 3
BATCH_PAUSE = 8

SOURCE_SHEET = "Sheet1"
SOURCE_COL = 1
START_ROW = 2

TARGET_SHEET = "Sheet2"
START_COL_LETTER = "H"
START_COL_INDEX = 8

HEADERS = [
    "agent_id", "first_name", "last_name", "position", "email",
    "mobile", "phone", "average_sold_price", "sold_listings",
    "agency_id", "agency_name", "agency_address", "agency_phone",
    "agency_website", "source_url", "error",
]
END_COL_LETTER = gspread.utils.rowcol_to_a1(
    1, START_COL_INDEX + len(HEADERS) - 1
).rstrip("1")


def get_gspread_client():
    return gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SA_JSON"]))


def parse_all_agents(html: str):
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        return []
    data = json.loads(match.group(1))
    props = data.get("props", {}).get("pageProps", {})

    agents = (
        props.get("agents")
        or props.get("agentList")
        or props.get("results")
        or props.get("agentResults")
        or props.get("searchResults", {}).get("agents")
        or props.get("searchResults", {}).get("results")
        or []
    )
    if not agents and props.get("agentProfile"):
        agents = [props["agentProfile"]]
    if isinstance(agents, dict):
        agents = [agents]

    records = []
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        agency = (
            agent.get("agencyDetail")
            or agent.get("agency")
            or agent.get("agencyProfile")
            or {}
        )
        records.append({
            "agent_id": agent.get("id") or agent.get("agentId"),
            "first_name": agent.get("firstName") or agent.get("first_name"),
            "last_name": agent.get("lastName") or agent.get("last_name"),
            "position": agent.get("position") or agent.get("jobTitle"),
            "email": agent.get("email"),
            "mobile": agent.get("mobile"),
            "phone": agent.get("phone"),
            "average_sold_price": agent.get("averageSoldPrice") or agent.get("medianSoldPrice"),
            "sold_listings": agent.get("numberOfSoldListings") or agent.get("soldListings"),
            "agency_id": agency.get("id") or agency.get("agencyId"),
            "agency_name": agency.get("name") or agency.get("agencyName"),
            "agency_address": agency.get("address"),
            "agency_phone": agency.get("phone"),
            "agency_website": agency.get("website"),
        })
    return records


def is_datadome_block(resp) -> bool:
    headers = {k.lower(): v for k, v in resp.headers.items()}
    if headers.get("x-datadome", "").lower() == "protected":
        return True
    if "datadome=" in headers.get("set-cookie", "").lower():
        return True
    body = resp.text or ""
    return "js.datadome.co/tags.js" in body or "captcha-delivery.com" in body


async def scrape_one(session: AsyncSession, url: str, sem: asyncio.Semaphore):
    async with sem:
        await asyncio.sleep(random.uniform(1.5, 4.0))
        try:
            resp = await session.get(url, impersonate="chrome124", timeout=30)
            if resp.status_code == 200 and not is_datadome_block(resp):
                recs = parse_all_agents(resp.text)
                if recs:
                    return recs

            from stealthkit import StealthSession
            sr = StealthSession(impersonate="chrome124")
            sr.solve_challenge(url)
            resp2 = sr.get(url)
            if resp2 and resp2.status_code == 200:
                recs = parse_all_agents(resp2.text)
                if recs:
                    return recs
            return [{"error": "blocked_after_fallback"}]
        except Exception as e:
            return [{"error": str(e)}]


async def scrape_batch(session: AsyncSession, urls: list[str]):
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [scrape_one(session, u, sem) for u in urls]
    return await asyncio.gather(*tasks)


def trigger_next_run(start_row: int):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/scrape.yml/dispatches"
    payload = {"ref": "main", "inputs": {"start_row": str(start_row)}}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    print(f"Triggered next run from row {start_row}: HTTP {r.status_code}")
    if r.status_code != 204:
        print(f"Trigger failed: {r.text}")


def get_or_create_target(sh):
    try:
        return sh.worksheet(TARGET_SHEET)
    except gspread.WorksheetNotFound:
        print(f"Creating '{TARGET_SHEET}'...")
        return sh.add_worksheet(title=TARGET_SHEET, rows=1000, cols=30)


def main():
    gc = get_gspread_client()
    sh = gc.open_by_key(os.environ["SHEET_ID"])

    source = sh.worksheet(SOURCE_SHEET)
    target = get_or_create_target(sh)

    start_row = int(os.environ.get("START_ROW", str(START_ROW)))
    col_a = source.col_values(SOURCE_COL)

    all_rows = [
        (idx, u.strip())
        for idx, u in enumerate(col_a, start=1)
        if idx >= start_row and u and u.strip()
    ]

    if not all_rows:
        print(f"No URLs from row {start_row}. All done.")
        return

    batch = all_rows[:MAX_URLS_PER_RUN]
    remaining = len(all_rows) - len(batch)
    print(f"Start row {start_row}: {len(batch)} URLs, {remaining} remaining after.")

    target.update(f"{START_COL_LETTER}1:{END_COL_LETTER}1", [HEADERS])

    existing = target.col_values(START_COL_INDEX)
    next_write_row = max(len(existing) + 1, 2)
    print(f"Writing to {TARGET_SHEET}!{START_COL_LETTER}{next_write_row}")

    async def run():
        nonlocal next_write_row
        async with AsyncSession() as session:
            for i in range(0, len(batch), 30):
                sub = batch[i:i + 30]
                urls = [u for _, u in sub]
                results = await scrape_batch(session, urls)

                flat_values = []
                for (_, url), recs in zip(sub, results):
                    for rec in recs:
                        rec = dict(rec)
                        rec["source_url"] = url
                        flat_values.append([rec.get(h, "") for h in HEADERS])

                if not flat_values:
                    continue

                end_row = next_write_row + len(flat_values) - 1
                target.update(
                    f"{START_COL_LETTER}{next_write_row}:{END_COL_LETTER}{end_row}",
                    flat_values,
                    value_input_option="RAW",
                )
                print(f"  Wrote {len(flat_values)} rows -> rows {next_write_row}-{end_row}")
                next_write_row = end_row + 1

                if i + 30 < len(batch):
                    time.sleep(BATCH_PAUSE)

    asyncio.run(run())

    if remaining > 0:
        next_start_row = batch[-1][0] + 1
        print(f"Triggering next run from row {next_start_row}")
        trigger_next_run(next_start_row)
    else:
        print("All URLs processed.")


if __name__ == "__main__":
    main()
