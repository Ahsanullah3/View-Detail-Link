import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

import gspread
import requests
from bs4 import BeautifulSoup
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
    "agent_name", "agent_slug", "agent_url",
    "agency_name", "agency_url",
    "sold_lead_agent", "median_sold_price",
    "median_days_on_market", "total_sales_all_suburbs",
    "source_url", "error",
]
END_COL_LETTER = gspread.utils.rowcol_to_a1(
    1, START_COL_INDEX + len(HEADERS) - 1
).rstrip("1")

BASE = "https://view.com.au"


# ---------- logging ----------
def ts():
    return datetime.utcnow().strftime("%H:%M:%S")

def log(m):     print(f"[{ts()}] {m}", flush=True)
def log_ok(m):  print(f"[{ts()}] ✅ {m}", flush=True)
def log_warn(m):print(f"[{ts()}] ⚠️  {m}", flush=True)
def log_err(m): print(f"[{ts()}] ❌ {m}", flush=True)

def log_progress(done, total, label=""):
    pct = (done / total * 100) if total else 0
    filled = int(30 * done / total) if total else 0
    bar = "█" * filled + "░" * (30 - filled)
    print(f"[{ts()}] [{bar}] {done}/{total} ({pct:.1f}%) {label}", flush=True)


# ---------- parsing ----------
def _text(node):
    return node.get_text(strip=True) if node else ""

def _abs(href):
    if not href:
        return ""
    return href if href.startswith("http") else BASE + href

def _int(s):
    m = re.search(r"[\d,]+", s or "")
    return int(m.group(0).replace(",", "")) if m else None

def _price(s):
    m = re.search(r"\$[\d,]+", s or "")
    return m.group(0) if m else None

def parse_agent_cards(html: str):
    """Extract every [data-testid='agent-card'] block."""
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-testid="agent-card"]')
    records = []
    for card in cards:
        # agency
        agency_a = card.select_one('[data-testid="agency-image"]')
        agency_url = _abs(agency_a["href"]) if agency_a and agency_a.has_attr("href") else ""
        agency_img = agency_a.find("img") if agency_a else None
        agency_name = agency_img.get("alt", "").strip() if agency_img else ""

        # agent link wraps the body
        agent_a = card.find("a", href=re.compile(r"^/agent-profile/"))
        agent_url = _abs(agent_a["href"]) if agent_a else ""
        agent_slug = agent_url.rstrip("/").split("/")[-1] if agent_url else ""

        # agent name = <h3>
        name_node = card.select_one("h3")
        agent_name = _text(name_node)

        # stats: each metric lives in a div with a <p class="text-at-xl ..."> value
        stats = {}
        for block in card.select("div.flex.flex-col.gap-2"):
            label = " ".join(_text(p) for p in block.find_all("p") if "text-at-xl" not in (p.get("class") or []))
            value_p = block.find("p", class_=lambda c: c and "text-at-xl" in c)
            value = _text(value_p)
            if "Median sold price" in label:
                stats["median_sold_price"] = _price(value) or value
            elif "Median days on market" in label:
                stats["median_days_on_market"] = _int(value)
            elif "Total sales" in label:
                stats["total_sales_all_suburbs"] = _int(value)
            elif label.startswith("Sold"):
                stats["sold_lead_agent"] = _int(value)

        records.append({
            "agent_name": agent_name,
            "agent_slug": agent_slug,
            "agent_url": agent_url,
            "agency_name": agency_name,
            "agency_url": agency_url,
            "sold_lead_agent": stats.get("sold_lead_agent"),
            "median_sold_price": stats.get("median_sold_price"),
            "median_days_on_market": stats.get("median_days_on_market"),
            "total_sales_all_suburbs": stats.get("total_sales_all_suburbs"),
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


async def scrape_one(session, url, sem, idx, total):
    async with sem:
        await asyncio.sleep(random.uniform(1.5, 4.0))
        log(f"[{idx}/{total}] FETCH {url}")
        try:
            resp = await session.get(url, impersonate="chrome124", timeout=30)
            if resp.status_code == 200 and not is_datadome_block(resp):
                recs = parse_agent_cards(resp.text)
                if recs:
                    log_ok(f"[{idx}/{total}] {len(recs)} cards from {url}")
                    return recs
                log_warn(f"[{idx}/{total}] 200 OK but 0 cards: {url}")
            else:
                log_warn(f"[{idx}/{total}] DATADOME/HTTP {resp.status_code}")

            log(f"[{idx}/{total}] Fallback -> stealthkit ...")
            from stealthkit import StealthSession
            sr = StealthSession(impersonate="chrome124")
            sr.solve_challenge(url)
            resp2 = sr.get(url)
            if resp2 and resp2.status_code == 200:
                recs = parse_agent_cards(resp2.text)
                if recs:
                    log_ok(f"[{idx}/{total}] {len(recs)} cards (fallback)")
                    return recs
            log_err(f"[{idx}/{total}] blocked_after_fallback: {url}")
            return [{"error": "blocked_after_fallback"}]
        except Exception as e:
            log_err(f"[{idx}/{total}] EXCEPTION {type(e).__name__}: {e}")
            return [{"error": f"{type(e).__name__}: {e}"}]


async def scrape_batch(session, urls, offset, total):
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [scrape_one(session, u, sem, offset + i + 1, total) for i, u in enumerate(urls)]
    return await asyncio.gather(*tasks)


def trigger_next_run(start_row):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"https://api.github.com/repos/{repo}/actions/workflows/scrape.yml/dispatches"
    payload = {"ref": "main", "inputs": {"start_row": str(start_row)}}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    log(f"Triggered next run from row {start_row}: HTTP {r.status_code}")


def get_or_create_target(sh):
    try:
        return sh.worksheet(TARGET_SHEET)
    except gspread.WorksheetNotFound:
        log_warn(f"Creating '{TARGET_SHEET}'...")
        return sh.add_worksheet(title=TARGET_SHEET, rows=1000, cols=30)


def get_gspread_client():
    return gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SA_JSON"]))


def main():
    log("=" * 60); log("SCRAPER START"); log("=" * 60)
    gc = get_gspread_client()
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    source = sh.worksheet(SOURCE_SHEET)
    target = get_or_create_target(sh)

    start_row = int(os.environ.get("START_ROW", str(START_ROW)))
    col_a = source.col_values(SOURCE_COL)
    all_rows = [(i, u.strip()) for i, u in enumerate(col_a, start=1)
                if i >= start_row and u and u.strip()]
    if not all_rows:
        log_warn("No URLs."); return

    batch = all_rows[:MAX_URLS_PER_RUN]
    remaining = len(all_rows) - len(batch)
    log(f"URLs available={len(all_rows)} processing={len(batch)} remaining={remaining}")

    target.update(f"{START_COL_LETTER}1:{END_COL_LETTER}1", [HEADERS])
    existing = target.col_values(START_COL_INDEX)
    next_write_row = max(len(existing) + 1, 2)
    log(f"Appending at {TARGET_SHEET}!{START_COL_LETTER}{next_write_row}")

    stats = {"done":0,"ok":0,"fail":0,"agents":0,"rows":0,"t0":time.time()}

    async def run():
        nonlocal next_write_row
        async with AsyncSession() as session:
            total = len(batch)
            for i in range(0, len(batch), 30):
                sub = batch[i:i+30]
                urls = [u for _, u in sub]
                log(""); log(f"--- BATCH {i//30+1} ({len(sub)} URLs) ---")
                results = await scrape_batch(session, urls, offset=i, total=total)

                flat = []
                for (_, url), recs in zip(sub, results):
                    for rec in recs:
                        rec["source_url"] = url
                        flat.append([rec.get(h, "") for h in HEADERS])
                    if recs and recs[0].get("error"):
                        stats["fail"] += 1
                    else:
                        stats["ok"] += 1
                    stats["done"] += 1
                    stats["agents"] += len(recs)

                if flat:
                    end_row = next_write_row + len(flat) - 1
                    target.update(
                        f"{START_COL_LETTER}{next_write_row}:{END_COL_LETTER}{end_row}",
                        flat, value_input_option="RAW",
                    )
                    stats["rows"] += len(flat)
                    log_ok(f"Wrote {len(flat)} rows -> H{next_write_row}:{END_COL_LETTER}{end_row}")
                    next_write_row = end_row + 1
                else:
                    log_warn("No rows to write")

                log_progress(stats["done"], total, "URLs done")
                log(f"  totals ok={stats['ok']} failed={stats['fail']} agents={stats['agents']} rows={stats['rows']}")

                if i + 30 < len(batch):
                    time.sleep(BATCH_PAUSE)

    asyncio.run(run())

    elapsed = time.time() - stats["t0"]
    log(""); log("=" * 60); log("RUN SUMMARY"); log("=" * 60)
    log(f"Elapsed: {elapsed:.1f}s")
    log(f"URLs: {stats['done']}/{len(batch)}  ok={stats['ok']} failed={stats['fail']}")
    log(f"Agents extracted: {stats['agents']}  rows written: {stats['rows']}")
    log(f"Next write row: {TARGET_SHEET}!{START_COL_LETTER}{next_write_row}")
    log(f"Remaining URLs: {remaining}")

    if remaining > 0:
        trigger_next_run(batch[-1][0] + 1)
    else:
        log_ok("All URLs processed.")
    log("=" * 60)


if __name__ == "__main__":
    main()
