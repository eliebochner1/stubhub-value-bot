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
    if "?" in u:
        return u.split("?")[0] + "?…"
    return u

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

# Enforce quantity (e.g., 2 tickets)
MIN_TICKETS = int(os.getenv("MIN_TICKETS", "2"))

# Digest settings
DIGEST_INTERVAL_SECONDS = int(os.getenv("DIGEST_INTERVAL_SECONDS", "3600"))
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "15"))

# Alerts via Pushover (recommended). If not set, prints to logs.
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "seen_listings.json")

# Optional fallback if numeric scores not visible:
# Example: "best deal,great deal,amazing deal"
DEAL_LABELS_TRIGGER = [s.strip().lower() for s in os.getenv("DEAL_LABELS_TRIGGER", "").split(",") if s.strip()]

# Debug controls
DEBUG_DUMP_HTML_ON_FAILURE = os.getenv("DEBUG_DUMP_HTML_ON_FAILURE", "1") == "1"
DEBUG_PRINT_SAMPLE_BLOCKS = int(os.getenv("DEBUG_PRINT_SAMPLE_BLOCKS", "3"))
DEBUG_MAX_SAMPLE_CHARS = int(os.getenv("DEBUG_MAX_SAMPLE_CHARS", "700"))

# Heartbeat
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "10"))

# =========================
# Data model
# =========================

@dataclass
class Listing:
    section: str
    row: str
    qty: int
    price: str
    fees_or_all_in: str
    value_score: Optional[float]
    deal_label: Optional[str]
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
    raw = f"{l.section}|{l.row}|{l.qty}|{l.price}|{l.fees_or_all_in}|{l.value_score}|{l.deal_label}|{l.url}"
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
        data={
            "token": PUSHOVER_API_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
        },
        timeout=20,
    )
    resp.raise_for_status()

# =========================
# Parsing helpers
# =========================

def extract_value_score(text: str) -> Optional[float]:
    """
    Only accept numbers explicitly tied to deal/value score wording.
    Avoids false positives like '2 tickets' or '$73'.
    """
    m = re.search(r"(deal|value)\s*(score)?\s*[:\-]?\s*(\d+(\.\d+)?)", text, re.IGNORECASE)
    if m:
        try:
            return float(m.group(3))
        except Exception:
            return None
    return None

def extract_deal_label(text: str) -> Optional[str]:
    labels = [
        "best deal",
        "great deal",
        "amazing deal",
        "good deal",
        "best value",
        "good value",
        "fair deal",
    ]
    low = text.lower()
    for lab in labels:
        if lab in low:
            return lab.title()
    return None

def extract_qty(text: str) -> int:
    m = re.search(r"(\d+)\s+tickets?", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    m = re.search(r"\b(qty|quantity)\b\D{0,6}(\d+)", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(2))
        except Exception:
            return 0
    return 0

def extract_section_row_price_fees(text: str) -> Tuple[str, str, str, str]:
    section = "Unknown"
    row = "Unknown"
    price = "Unknown"
    fees = ""

    m = re.search(r"Section\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
    if m:
        section = m.group(1)

    m = re.search(r"Row\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
    if m:
        row = m.group(1)

    dollars = re.findall(r"\$\s?\d[\d,]*", text)
    if dollars:
        price = dollars[0].replace(" ", "")

    m = re.search(r"(All[-\s]?in.*?(\$\s?\d[\d,]*))", text, re.IGNORECASE)
    if m:
        fees = m.group(1)

    return section, row, price, fees

def format_listing(l: Listing) -> str:
    score = f"{l.value_score:.1f}" if l.value_score is not None else "NA"
    label = f" | {l.deal_label}" if l.deal_label else ""
    fees = f" | {l.fees_or_all_in}" if l.fees_or_all_in else ""
    return f"Score {score}{label} | {l.section}/{l.row} | Qty {l.qty} | {l.price}{fees}"

def detect_flags(html: str) -> List[str]:
    flags = [
        "captcha",
        "verify you are human",
        "unusual traffic",
        "access denied",
        "cloudflare",
        "consent",
        "cookie",
    ]
    low = html.lower()
    return [f for f in flags if f in low]

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
    # Force virtualized results to mount
    for _ in range(12):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(800)
    page.mouse.wheel(0, -99999)
    page.wait_for_timeout(900)

def find_results_root(page):
    """
    Anchor to 'View <N> Listings' and climb to a container that likely holds results.
    """
    anchor = page.locator("text=/View\\s+\\d+\\s+Listings/i").first
    if anchor.count() == 0:
        return None

    # Climb to a container. We try a few ancestor depths to be robust.
    # Using XPath to find a reasonable DIV/MAIN ancestor.
    for depth in range(1, 8):
        try:
            cand = anchor.locator(f"xpath=ancestor::*[{depth}]")
            if cand.count() > 0:
                # sanity: does this ancestor contain multiple "Section" tokens?
                sec_count = cand.locator("text=/\\bSection\\b/i").count()
                if sec_count >= 5:
                    log(f"[debug] results root found at ancestor depth {depth} (Section tokens={sec_count})")
                    return cand
        except Exception:
            continue

    # fallback: closest div/main
    fallback = anchor.locator("xpath=ancestor-or-self::*[self::div or self::main][1]")
    return fallback if fallback.count() > 0 else None

def extract_listing_blocks_from_root(root, max_blocks=300) -> List[str]:
    """
    Extract candidate blocks that look like real listing rows/cards.
    Hard gating eliminates filters and seat map.
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

            # Hard gates for real listing-like content
            must_have = ("section" in low) and ("row" in low) and ("ticket" in low) and ("$" in t)
            if not must_have:
                continue

            # Exclude obvious filter panels (these contain huge lists like "Number of tickets Any 1 ticket 2 tickets...")
            if "number of tickets" in low and "reset filters" in low:
                continue

            # Exclude seat-map section index blobs (tons of bare numbers / "FLR 1 FLR 7...")
            if re.search(r"\bFLR\b", t) and len(t) > 800:
                continue

            texts.append(t)
        except PlaywrightTimeoutError:
            continue
        except Exception:
            continue

    return texts

def summarize_text_for_log(t: str) -> str:
    s = re.sub(r"\s+", " ", t).strip()
    if len(s) > DEBUG_MAX_SAMPLE_CHARS:
        s = s[:DEBUG_MAX_SAMPLE_CHARS] + "…"
    return s

# =========================
# Scrape
# =========================

def scrape_listings(event_url: str) -> List[Listing]:
    listings: List[Listing] = []

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
        flags = detect_flags(html)
        log(f"[debug] flags -> {flags}")

        try_click_consent(page)
        scroll_aggressively(page)
        page.wait_for_timeout(1200)

        root = find_results_root(page)
        if not root:
            log("[error] could not locate results root (missing 'View N Listings' anchor).")
            if DEBUG_DUMP_HTML_ON_FAILURE:
                write_debug_file("/tmp/stubhub_debug_no_results_root.html", html[:2_000_000])
            browser.close()
            return listings

        texts = extract_listing_blocks_from_root(root, max_blocks=350)
        log(f"[debug] listing-like blocks extracted -> {len(texts)}")

        if DEBUG_PRINT_SAMPLE_BLOCKS > 0:
            for i, t in enumerate(texts[:DEBUG_PRINT_SAMPLE_BLOCKS], start=1):
                log(f"[debug] sample LISTING block {i}: {summarize_text_for_log(t)}")

        # Parse listing blocks
        for t in texts:
            qty = extract_qty(t)
            section, row, price, fees = extract_section_row_price_fees(t)
            score = extract_value_score(t)
            deal_label = extract_deal_label(t)

            # basic plausibility
            if section == "Unknown" and price == "Unknown" and qty == 0:
                continue

            listings.append(
                Listing(
                    section=section,
                    row=row,
                    qty=qty,
                    price=price,
                    fees_or_all_in=fees,
                    value_score=score,
                    deal_label=deal_label,
                    url=event_url,
                )
            )

        if len(listings) == 0 and DEBUG_DUMP_HTML_ON_FAILURE:
            html2 = page.content()
            write_debug_file("/tmp/stubhub_debug_zero_listings.html", html2[:2_000_000])

        browser.close()

    return listings

# =========================
# Qualification rules
# =========================

def qualifies(l: Listing) -> bool:
    # enforce minimum tickets
    if l.qty and l.qty < MIN_TICKETS:
        return False

    # numeric score path
    if l.value_score is not None:
        return l.value_score >= MIN_VALUE_SCORE

    # optional label fallback
    if DEAL_LABELS_TRIGGER and l.deal_label:
        return l.deal_label.strip().lower() in DEAL_LABELS_TRIGGER

    return False

def price_num(l: Listing) -> float:
    m = re.sub(r"[^\d.]", "", l.price or "")
    try:
        return float(m) if m else 1e18
    except Exception:
        return 1e18

# =========================
# Heartbeat thread
# =========================

def start_heartbeat() -> None:
    def hb():
        while True:
            log("[heartbeat] process alive")
            time.sleep(HEARTBEAT_SECONDS)
    threading.Thread(target=hb, daemon=True).start()

# =========================
# Main loop
# =========================

def main() -> None:
    log("=== BOT STARTING ===")
    log(f"EVENT_URL present: {bool(EVENT_URL)}")
    log(f"MIN_VALUE_SCORE={MIN_VALUE_SCORE}")
    log(f"MIN_TICKETS={MIN_TICKETS}")
    log(f"CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}")
    log(f"DIGEST_INTERVAL_SECONDS={DIGEST_INTERVAL_SECONDS}")
    log(f"DIGEST_TOP_N={DIGEST_TOP_N}")
    log(f"DEAL_LABELS_TRIGGER={DEAL_LABELS_TRIGGER}")

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

            # Filter for minimum tickets first (more deterministic)
            listings_qty = [l for l in listings if (l.qty == 0 or l.qty >= MIN_TICKETS)]
            qualifying = [l for l in listings_qty if qualifies(l)]

            log(f"[cycle] scraped={len(listings)} qty_ok={len(listings_qty)} qualifying={len(qualifying)} at {time.ctime()}")

            # NEW alerts
            new_hits: List[Tuple[str, Listing]] = []
            for l in qualifying:
                fp = listing_fingerprint(l)
                if fp not in seen:
                    new_hits.append((fp, l))

            if new_hits:
                lines = [format_listing(l) for _, l in new_hits[:12]]
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
                    key=lambda l: (-(l.value_score or 0.0), price_num(l))
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
