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
DONE_COL_INDEX = 8

HEADERS = [
    "agent_name", "agent_slug", "agent_url",
    "agency_name", "agency_url",
    "sold_lead_agent", "median_sold_price",
    "median_days_on_market", "total_sales_all_suburbs",
    "source_url", "scraped_at", "error",
]
END_COL_LETTER = gspread.utils.rowcol_to_a1(
    1, START_COL_INDEX + len(HEADERS) - 1
).rstrip("1")

BASE = "https://view.com.au"


def ts(): return datetime.utcnow().strftime("%H:%M:%S")
def log(m):      print(f"[{ts()}] {m}", flush=True)
def log_ok(m):   print(f"[{ts()}] ✅ {m}", flush=True)
def log_warn(m): print(f"[{ts()}] ⚠️  {m}", flush=True)
def log_err(m):  print(f"[{ts()}] ❌ {m}", flush=True)

def log_progress(done, total, label=""):
    pct = (done / total * 100) if total else 0
    filled = int(30 * done / total) if total else 0
    bar = "█" * filled + "░" * (30 - filled)
    print(f"[{ts()}] [{bar}] {done}/{total} ({pct:.1f}%) {label}", flush=True)


def _text(node): return node.get_text(strip=True) if node else ""
def _abs(href):
    if not href: return ""
    return href if href.startswith("http") else BASE + href
def _int(s):
    m = re.search(r"[\d,]+", s or "")
    return int(m.group(0).replace(",", "")) if m else None
def _price(s):
    m = re.search(r"\$[\d,]+", s or "")
    return m.group(0) if m else None


def parse_agent_cards(html: str):
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select('[data-testid="agent-card"]')
    records = []
    for card in cards:
        agency_a = card.select_one('[data-testid="agency-image"]')
        agency_url = _abs(agency_a["href"]) if agency_a and agency_a.has_attr("href") else ""
        agency_img = agency_a.find("img") if agency_a else None
        agency_name = agency_img.get("alt", "").strip() if agency_img else ""

        agent_a = card.find("a", href=re.compile(r"^/agent-profile/"))
        agent_url = _abs(agent_a["href"]) if agent_a else ""
        agent_slug = agent_url.rstrip("/").split("/")[-1] if agent_url else ""

        name_node = card.select_one("h3")
        agent_name = _text(name_node)

        stats = {}
        for block in card.select("div.flex.flex-col.gap-2"):
            label = " ".join(
                _text(p) for p in block.find_all("p")
                if "text-at-xl" not in (p.get("class") or [])
            )
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


def get_gspread_client():
    return gspread.service_account_from_dict(json.loads(os.environ["GOOGLE_SA_JSON"]))


def get_or_create_target(sh):
    try:
        return sh.worksheet(TARGET_SHEET)
    except gspread.WorksheetNotFound:
        log_warn(f"Creating '{TARGET_SHEET}'...")
        return sh.add_worksheet(title=TARGET_SHEET, rows=2000, cols=30)


def ensure_rows(target, needed: int):
    if needed > target.row_count:
        new_rows = max(needed + 500, target.row_count * 2)
        log_warn(f"Resizing {TARGET_SHEET}: {target.row_count} -> {new_rows} rows")
        target.resize(rows=new_rows)


def load_done_urls(target):
    try:
        col_h = target.col_values(DONE_COL_INDEX)
    except Exception as e:
        log_warn(f"Could not read done URLs: {e}")
        return set()
    return {u.strip() for u in col_h[1:] if u and u.strip()}


def _write_github_output(has_more: bool, next_start_row: str):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"has_more={'true' if has_more else 'false'}\n")
        if next_start_row:
            f.write(f"next_start_row={next_start_row}\n")
    log(f"Wrote GITHUB_OUTPUT: has_more={has_more} next_start_row={next_start_row or '-'}")


def main():
    log("=" * 60); log("SCRAPER START"); log("=" * 60)

    gc = get_gspread_client()
    sh = gc.open_by_key(os.environ["SHEET_ID"])
    source = sh.worksheet(SOURCE_SHEET)
    target = get_or_create_target(sh)

    # Ensure header row fits before touching anything
    ensure_rows(target, 2)
    if target.col_count < START_COL_INDEX + len(HEADERS):
        target.resize(rows=target.row_count, cols=START_COL_INDEX + len(HEADERS))

    start_row = int(os.environ.get("START_ROW", str(START_ROW)))
    log(f"START_ROW = {start_row}")

    done_urls = load_done_urls(target)
    log(f"Already-scraped URLs in {TARGET_SHEET} col H: {len(done_urls)}")

    col_a = source.col_values(SOURCE_COL)
    log(f"Read {len(col_a)} cells from {SOURCE_SHEET}!A")

    pending, skipped = [], 0
    for i, u in enumerate(col_a, start=1):
        if i < start_row: continue
        u = (u or "").strip()
        if not u: continue
        if u in done_urls:
            skipped += 1
            continue
        pending.append((i, u))

    log(f"Pending URLs: {len(pending)} | Skipped (already done): {skipped}")

    if not pending:
        log_ok("Nothing to scrape — all URLs already done.")
        _write_github_output(has_more=False, next_start_row="")
        return

    batch = pending[:MAX_URLS_PER_RUN]
    remaining = len(pending) - len(batch)
    log(f"Processing this run: {len(batch)} | Remaining after: {remaining}")

    # Ensure header exists
    ensure_rows(target, 1)
    target.update(f"{START_COL_LETTER}1:{END_COL_LETTER}1", [HEADERS])
    log_ok(f"Headers written to {TARGET_SHEET}!{START_COL_LETTER}1:{END_COL_LETTER}1")

    stats = {"done": 0, "ok": 0, "fail": 0, "agents": 0, "rows": 0, "t0": time.time()}

    async def run():
        async with AsyncSession() as session:
            total = len(batch)
            for i in range(0, len(batch), 30):
                sub = batch[i:i + 30]
                urls = [u for _, u in sub]
                log("")
                log(f"--- BATCH {i // 30 + 1} ({len(sub)} URLs) ---")
                results = await scrape_batch(session, urls, offset=i, total=total)

                flat = []
                now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                for (_, url), recs in zip(sub, results):
                    for rec in recs:
                        rec["source_url"] = url
                        rec["scraped_at"] = now
                        flat.append([rec.get(h, "") for h in HEADERS])
                    if recs and recs[0].get("error"):
                        stats["fail"] += 1
                    else:
                        stats["ok"] += 1
                    stats["done"] += 1
                    stats["agents"] += len(recs)

                if flat:
                    # append_rows auto-grows the sheet — no manual sizing needed
                    target.append_rows(flat, value_input_option="RAW")
                    stats["rows"] += len(flat)
                    log_ok(f"Appended {len(flat)} rows to {TARGET_SHEET}")
                else:
                    log_warn("No rows to write for this batch")

                log_progress(stats["done"], total, "URLs done")
                log(f"  totals ok={stats['ok']} failed={stats['fail']} "
                    f"agents={stats['agents']} rows={stats['rows']}")

                if i + 30 < len(batch):
                    log(f"Sleeping {BATCH_PAUSE}s...")
                    time.sleep(BATCH_PAUSE)

    asyncio.run(run())

    elapsed = time.time() - stats["t0"]
    log(""); log("=" * 60); log("RUN SUMMARY"); log("=" * 60)
    log(f"Elapsed: {elapsed:.1f}s")
    log(f"URLs: {stats['done']}/{len(batch)}  ok={stats['ok']} failed={stats['fail']}")
    log(f"Agents extracted: {stats['agents']}  rows written: {stats['rows']}")
    log(f"Remaining URLs: {remaining}")

    if remaining > 0:
        next_start_row = batch[-1][0] + 1
        log(f"More work remaining -> next_start_row={next_start_row}")
        _write_github_output(has_more=True, next_start_row=str(next_start_row))
    else:
        log_ok("All pending URLs processed. No further trigger needed.")
        _write_github_output(has_more=False, next_start_row="")

    log("=" * 60)


if __name__ == "__main__":
    main()
