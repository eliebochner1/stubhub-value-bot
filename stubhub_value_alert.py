import os
import re
import time
import json
import hashlib
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# =========================
# Logging helpers
# =========================

def log(msg: str) -> None:
    print(msg, flush=True)

def redact_url(u: str) -> str:
    return u.split("?")[0] + "?…" if "?" in u else u

def write_debug_file(path: str, content: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"[debug] wrote file: {path} ({len(content)} bytes)")
    except Exception as e:
        log(f"[debug] failed to write {path}: {e}")


# =========================
# Config
# =========================

load_dotenv()

EVENT_URL = os.getenv("STUBHUB_EVENT_URL", "").strip()
MIN_VALUE_SCORE = float(os.getenv("MIN_VALUE_SCORE", "9.5"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
MIN_TICKETS = int(os.getenv("MIN_TICKETS", "2"))

DIGEST_INTERVAL_SECONDS = int(os.getenv("DIGEST_INTERVAL_SECONDS", "3600"))
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "15"))

PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "seen_listings.json")

DEBUG_DUMP_HTML_ON_FAILURE = os.getenv("DEBUG_DUMP_HTML_ON_FAILURE", "1") == "1"
DEBUG_PRINT_SAMPLE_BLOCKS = int(os.getenv("DEBUG_PRINT_SAMPLE_BLOCKS", "2"))
DEBUG_MAX_SAMPLE_CHARS = int(os.getenv("DEBUG_MAX_SAMPLE_CHARS", "900"))

HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "10"))


# =========================
# Data model
# =========================

@dataclass
class Listing:
    section: str
    row: str
    qty: int
    price_incl_fees: str
    value_score: Optional[float]
    rating_word: Optional[str]   # e.g., "Amazing"
    tags: List[str]              # e.g., ["Best deal", "Fan favorite"]
    url: str


# =========================
# Persistence
# =========================

def load_seen() -> set:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen: set) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(list(seen)), f, indent=2)
    except Exception as e:
        log(f"[warn] failed to save state file: {e}")

def listing_fingerprint(l: Listing) -> str:
    raw = f"{l.section}|{l.row}|{l.qty}|{l.price_incl_fees}|{l.value_score}|{l.rating_word}|{','.join(l.tags)}|{l.url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# =========================
# Notifications
# =========================

def pushover_notify(title: str, message: str) -> None:
    if not (PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN):
        log("[alert] Pushover not configured; printing alert.")
        log(title)
        log(message)
        return

    resp = requests.post(
        "https://api.pushover.net/1/messages.json",
        data={"token": PUSHOVER_API_TOKEN, "user": PUSHOVER_USER_KEY, "title": title, "message": message},
        timeout=20,
    )
    resp.raise_for_status()


# =========================
# Parsing helpers
# =========================

def normalize_spaces(s: str) -> str:
    s = s.replace("\u200b", " ").replace("\ufeff", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def split_into_listing_chunks(block_text: str) -> List[str]:
    """
    Your 'listing block' contains multiple listings. Split by each 'Section <...>' occurrence.
    """
    t = normalize_spaces(block_text)

    # Stop at footer-ish markers if present
    t = re.split(r"\bShowing\s+\d+\s+of\s+\d+\b", t, maxsplit=1)[0].strip()

    # Split on "Section X" boundaries while keeping the word 'Section' in each chunk
    parts = re.split(r"(?=\bSection\s+[A-Za-z0-9]+\b)", t)
    chunks = [p.strip() for p in parts if p.strip().lower().startswith("section ")]
    return chunks

def extract_qty(chunk: str) -> int:
    m = re.search(r"(\d+)\s+tickets?", chunk, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    m = re.search(r"\b(qty|quantity)\b\D{0,6}(\d+)", chunk, re.IGNORECASE)
    if m:
        try:
            return int(m.group(2))
        except Exception:
            return 0
    return 0

def extract_section_row(chunk: str) -> Tuple[str, str]:
    section = "Unknown"
    row = "Unknown"
    m = re.search(r"\bSection\s+([A-Za-z0-9\-]+)\b", chunk, re.IGNORECASE)
    if m:
        section = m.group(1)
    m = re.search(r"\bRow\s+([A-Za-z0-9\-]+)\b", chunk, re.IGNORECASE)
    if m:
        row = m.group(1)
    return section, row

def extract_price_incl_fees(chunk: str) -> str:
    # Your sample: "$36 incl. fees"
    m = re.search(r"(\$\s?\d[\d,]*)\s*incl\.?\s*fees", chunk, re.IGNORECASE)
    if m:
        return m.group(1).replace(" ", "") + " incl. fees"

    # Fallback: first $ amount
    m = re.search(r"(\$\s?\d[\d,]*)", chunk)
    if m:
        return m.group(1).replace(" ", "")
    return "Unknown"

def extract_score_and_word(chunk: str) -> Tuple[Optional[float], Optional[str]]:
    """
    Your format: "... $36 incl. fees 9.9 Amazing"
    We detect a decimal score followed by a word like Amazing/Great/Good.
    """
    m = re.search(r"\b(\d{1,2}\.\d)\s+([A-Za-z]+)\b", chunk)
    if not m:
        return None, None
    try:
        return float(m.group(1)), m.group(2)
    except Exception:
        return None, None

def extract_tags(chunk: str) -> List[str]:
    """
    Tags in your sample: Best deal, Fan favorite, Best view, Best price, Price drops, etc.
    We'll capture a known set if present.
    """
    known = [
        "Best deal", "Fan favorite", "Best view", "Best price", "Price drops",
        "Recommended", "Popular", "Sponsored"
    ]
    low = chunk.lower()
    tags = [k for k in known if k.lower() in low]
    return tags

def format_listing(l: Listing) -> str:
    score = f"{l.value_score:.1f}" if l.value_score is not None else "NA"
    word = f" {l.rating_word}" if l.rating_word else ""
    tags = f" | {', '.join(l.tags)}" if l.tags else ""
    return f"Score {score}{word} | Section {l.section} Row {l.row} | Qty {l.qty} | {l.price_incl_fees}{tags}"

def qualifies(l: Listing) -> bool:
    if l.qty and l.qty < MIN_TICKETS:
        return False
    if l.value_score is None:
        return False
    return l.value_score >= MIN_VALUE_SCORE

def price_num(price_incl: str) -> float:
    m = re.sub(r"[^\d.]", "", price_incl or "")
    try:
        return float(m) if m else 1e18
    except Exception:
        return 1e18


# =========================
# Browser / DOM helpers
# =========================

def try_click_consent(page) -> None:
    patterns = [
        re.compile(r"accept|agree|i agree|allow all|got it", re.I),
        re.compile(r"continue", re.I),
    ]
    for pat in patterns:
        try:
            btn = page.get_by_role("button", name=pat)
            if btn.count() > 0:
                btn.first.click(timeout=2500)
                page.wait_for_timeout(1200)
                log("[debug] clicked consent button")
                return
        except Exception:
            pass

def scroll_aggressively(page) -> None:
    for _ in range(12):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(800)
    page.mouse.wheel(0, -99999)
    page.wait_for_timeout(900)

def find_results_root(page):
    anchor = page.locator("text=/View\\s+\\d+\\s+Listings/i").first
    if anchor.count() == 0:
        return None

    # Find an ancestor that contains many "Section" tokens (indicates results list)
    for depth in range(1, 10):
        try:
            cand = anchor.locator(f"xpath=ancestor::*[{depth}]")
            if cand.count() > 0:
                sec_count = cand.locator("text=/\\bSection\\b/i").count()
                if sec_count >= 10:
                    log(f"[debug] results root found at ancestor depth {depth} (Section tokens={sec_count})")
                    return cand
        except Exception:
            continue

    fallback = anchor.locator("xpath=ancestor-or-self::*[self::div or self::main][1]")
    return fallback if fallback.count() > 0 else None

def extract_listing_blocks_from_root(root, max_blocks=300) -> List[str]:
    """
    Extract candidate DOM nodes that likely represent listing rows/cards.
    Then we split each into per-listing chunks later.
    """
    candidates = root.locator("div, li").filter(has_text=re.compile(r"\bSection\b", re.I))
    total = candidates.count()
    log(f"[debug] candidates in results root: {total}")

    texts: List[str] = []
    n = min(total, max_blocks)
    for i in range(n):
        try:
            t = candidates.nth(i).inner_text(timeout=2200).strip()
            if not t:
                continue
            low = t.lower()

            # Must resemble listings, not filters
            if ("section" in low and "row" in low and "ticket" in low and "$" in t):
                if "number of tickets" in low and "reset filters" in low:
                    continue
                texts.append(t)
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    return texts

def summarize_for_log(t: str) -> str:
    s = normalize_spaces(t)
    if len(s) > DEBUG_MAX_SAMPLE_CHARS:
        s = s[:DEBUG_MAX_SAMPLE_CHARS] + "…"
    return s


# =========================
# Scrape
# =========================

def scrape_listings(event_url: str) -> List[Listing]:
    all_listings: List[Listing] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 820},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()

        log(f"[debug] navigating -> {redact_url(event_url)}")
        page.goto(event_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)

        try:
            log(f"[debug] title -> {page.title()}")
        except Exception:
            log("[debug] title -> (unavailable)")
        log(f"[debug] final url -> {redact_url(page.url)}")

        html = page.content()
        log(f"[debug] html size -> {len(html)}")

        try_click_consent(page)
        scroll_aggressively(page)
        page.wait_for_timeout(1200)

        root = find_results_root(page)
        if not root:
            log("[error] could not locate results root (missing 'View N Listings').")
            if DEBUG_DUMP_HTML_ON_FAILURE:
                write_debug_file("/tmp/stubhub_debug_no_results_root.html", html[:2_000_000])
            browser.close()
            return all_listings

        blocks = extract_listing_blocks_from_root(root, max_blocks=250)
        log(f"[debug] listing-like DOM blocks extracted -> {len(blocks)}")

        if DEBUG_PRINT_SAMPLE_BLOCKS > 0:
            for i, b in enumerate(blocks[:DEBUG_PRINT_SAMPLE_BLOCKS], start=1):
                log(f"[debug] sample BLOCK {i}: {summarize_for_log(b)}")

        # Now split each block into per-listing chunks and parse
        for b in blocks:
            chunks = split_into_listing_chunks(b)
            for c in chunks:
                qty = extract_qty(c)
                section, row = extract_section_row(c)
                price = extract_price_incl_fees(c)
                score, word = extract_score_and_word(c)
                tags = extract_tags(c)

                # plausibility
                if section == "Unknown" and row == "Unknown" and price == "Unknown":
                    continue

                all_listings.append(
                    Listing(
                        section=section,
                        row=row,
                        qty=qty,
                        price_incl_fees=price,
                        value_score=score,
                        rating_word=word,
                        tags=tags,
                        url=event_url,
                    )
                )

        browser.close()

    return all_listings


# =========================
# Heartbeat + main loop
# =========================

def start_heartbeat() -> None:
    def hb():
        while True:
            log("[heartbeat] process alive")
            time.sleep(HEARTBEAT_SECONDS)
    threading.Thread(target=hb, daemon=True).start()

def main() -> None:
    log("=== BOT STARTING ===")
    log(f"EVENT_URL present: {bool(EVENT_URL)}")
    log(f"MIN_VALUE_SCORE={MIN_VALUE_SCORE}")
    log(f"MIN_TICKETS={MIN_TICKETS}")
    log(f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log(f"DIGEST_INTERVAL_SECONDS={DIGEST_INTERVAL_SECONDS}")
    log(f"DIGEST_TOP_N={DIGEST_TOP_N}")

    start_heartbeat()

    if not EVENT_URL:
        log("[fatal] STUBHUB_EVENT_URL not set. Configure Railway Variable STUBHUB_EVENT_URL. Sleeping indefinitely.")
        while True:
            time.sleep(60)

    seen = load_seen()
    last_digest_ts = 0.0

    while True:
        try:
            listings = scrape_listings(EVENT_URL)

            # Enforce qty >= MIN_TICKETS (if qty parsed as 0, keep it but it won't qualify)
            qty_ok = [l for l in listings if (l.qty == 0 or l.qty >= MIN_TICKETS)]
            qualifying = [l for l in qty_ok if qualifies(l)]

            log(f"[cycle] parsed_listings={len(listings)} qty_ok={len(qty_ok)} qualifying={len(qualifying)} at {time.ctime()}")

            # NEW alerts
            new_hits: List[Tuple[str, Listing]] = []
            for l in qualifying:
                fp = listing_fingerprint(l)
                if fp not in seen:
                    new_hits.append((fp, l))

            if new_hits:
                # sort: highest score, then cheapest
                new_hits_sorted = sorted(new_hits, key=lambda x: (-(x[1].value_score or 0.0), price_num(x[1].price_incl_fees)))
                lines = [format_listing(l) for _, l in new_hits_sorted[:12]]
                msg = (
                    f"NEW qualifying listings (qty≥{MIN_TICKETS}, score≥{MIN_VALUE_SCORE}):\n"
                    + "\n".join(lines)
                    + f"\n\nEvent: {EVENT_URL}"
                )
                pushover_notify("NEW StubHub listings", msg)

                for fp, _ in new_hits:
                    seen.add(fp)
                save_seen(seen)
                log(f"[alert] new alerts sent={len(new_hits)}")
            else:
                log("[alert] no new qualifying listings")

            # DIGEST snapshot
            now = time.time()
            if (now - last_digest_ts) >= DIGEST_INTERVAL_SECONDS:
                last_digest_ts = now

                qualifying_sorted = sorted(
                    qualifying,
                    key=lambda l: (-(l.value_score or 0.0), price_num(l.price_incl_fees))
                )
                top_n = qualifying_sorted[:DIGEST_TOP_N]

                if top_n:
                    lines = [format_listing(l) for l in top_n]
                    msg = (
                        f"CUMULATIVE snapshot (top {len(top_n)}) (qty≥{MIN_TICKETS}, score≥{MIN_VALUE_SCORE}):\n"
                        + "\n".join(lines)
                        + f"\n\nTotal qualifying visible now: {len(qualifying_sorted)}"
                        + f"\nEvent: {EVENT_URL}"
                    )
                else:
                    msg = (
                        f"CUMULATIVE snapshot: no qualifying listings visible now "
                        f"(qty≥{MIN_TICKETS}, score≥{MIN_VALUE_SCORE}).\n"
                        f"Event: {EVENT_URL}"
                    )

                pushover_notify("DIGEST StubHub snapshot", msg)
                log("[digest] sent")

        except Exception as e:
            log(f"[error] cycle exception: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
