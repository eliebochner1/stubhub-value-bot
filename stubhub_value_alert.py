import os
import re
import time
import json
import hashlib
from dataclasses import dataclass
from typing import List, Optional

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

EVENT_URL = os.getenv("STUBHUB_EVENT_URL", "").strip()
MIN_VALUE_SCORE = float(os.getenv("MIN_VALUE_SCORE", "9.5"))
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

# Alerts via Pushover (recommended). If not set, prints to logs.
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "").strip()
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN", "").strip()

STATE_FILE = os.getenv("STATE_FILE", "seen_listings.json")


@dataclass
class Listing:
    section: str
    row: str
    qty: str
    price: str
    fees_or_all_in: str
    value_score: Optional[float]
    url: str


def load_seen() -> set:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen: set) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(seen)), f, indent=2)


def listing_fingerprint(l: Listing) -> str:
    raw = f"{l.section}|{l.row}|{l.qty}|{l.price}|{l.value_score}|{l.url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def pushover_notify(title: str, message: str) -> None:
    if not (PUSHOVER_USER_KEY and PUSHOVER_API_TOKEN):
        print("Pushover not configured; printing alert instead.")
        print(title)
        print(message)
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


def parse_float_maybe(s: str) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"(\d+(\.\d+)?)", s)
    return float(m.group(1)) if m else None


def scrape_listings(event_url: str) -> List[Listing]:
    """
    Loads StubHub event page and extracts listing cards from rendered DOM.

    NOTE: StubHub's HTML can change. If the bot stops finding cards,
    update the selectors below after inspecting the page HTML.
    """
    listings: List[Listing] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(event_url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        possible_card_selectors = [
            "[data-testid*='listing']",
            "[class*='Listing']",
            "[class*='listing']",
        ]

        sel = None
        for s in possible_card_selectors:
            try:
                page.wait_for_selector(s, timeout=15000)
                sel = s
                break
            except Exception:
                continue

        if not sel:
            browser.close()
            return listings

        cards = page.query_selector_all(sel)
        for c in cards:
            text = (c.inner_text() or "").strip()
            if not text:
                continue

            # Heuristic: many listing cards include a "Deal Score" / "Value" score;
            # if present, it often appears as a decimal like 9.6
            value_score = parse_float_maybe(text)

            section = ""
            row = ""
            qty = ""
            price = ""
            fees = ""

            m = re.search(r"Section\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
            if m:
                section = m.group(1)

            m = re.search(r"Row\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
            if m:
                row = m.group(1)

            m = re.search(r"(\d+)\s+tickets?", text, re.IGNORECASE)
            if m:
                qty = m.group(1)

            m = re.search(r"(\$\s?\d[\d,]*)", text)
            if m:
                price = m.group(1).replace(" ", "")

            m = re.search(r"(All[-\s]?in.*?(\$\s?\d[\d,]*))", text, re.IGNORECASE)
            if m:
                fees = m.group(1)

            listings.append(
                Listing(
                    section=section or "Unknown",
                    row=row or "Unknown",
                    qty=qty or "Unknown",
                    price=price or "Unknown",
                    fees_or_all_in=fees or "",
                    value_score=value_score,
                    url=event_url,
                )
            )

        browser.close()

    return listings


def main():
    if not EVENT_URL:
        raise SystemExit("Set STUBHUB_EVENT_URL in your environment variables.")

    seen = load_seen()

    while True:
        try:
            listings = scrape_listings(EVENT_URL)

            new_hits = []
            for l in listings:
                if l.value_score is None:
                    continue
                if l.value_score >= MIN_VALUE_SCORE:
                    fp = listing_fingerprint(l)
                    if fp not in seen:
                        new_hits.append((fp, l))

            if new_hits:
                lines = []
                for _, l in new_hits[:12]:
                    lines.append(
                        f"Score {l.value_score:.1f} | {l.section}/{l.row} | Qty {l.qty} | {l.price}"
                        + (f" | {l.fees_or_all_in}" if l.fees_or_all_in else "")
                    )
                msg = "\n".join(lines) + f"\n\nEvent: {EVENT_URL}"
                pushover_notify(f"StubHub Value ≥ {MIN_VALUE_SCORE}", msg)

                for fp, _ in new_hits:
                    seen.add(fp)
                save_seen(seen)
            else:
                print(f"No new listings ≥ {MIN_VALUE_SCORE} at {time.ctime()}")

        except Exception as e:
            print(f"Error: {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
