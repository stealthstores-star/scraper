#!/usr/bin/env python3
"""
AliExpress Product Scraper
==========================
Setup:
    pip3 install -r requirements.txt
    python3 -m playwright install chromium

Usage:
    python3 scraper.py urls.txt
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scraper")

# ---------------------------------------------------------------------------
# CSV — writes rows live
# ---------------------------------------------------------------------------

FIELDS = [
    "id", "product_title", "product_price", "product_original_price",
    "product_discount", "product_url", "product_image", "product_rating",
    "store_name", "store_url", "store_id", "total_sales", "ship_from",
    "store_member_id", "trade_info", "shipping", "launch_time",
    "company_name", "source_url",
]


class LiveCSV:
    def __init__(self, path):
        self.path = path
        self.count = 0
        self._seen = set()
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=FIELDS, extrasaction="ignore")
        self._w.writeheader()
        self._f.flush()

    def add(self, rows, source_url):
        dupes = 0
        for r in rows:
            pid = r.get("id", "")
            if pid in self._seen:
                dupes += 1
                continue
            self._seen.add(pid)
            r["source_url"] = source_url
            self._w.writerow(r)
            self.count += 1
        self._f.flush()
        if dupes:
            log.info("  Skipped %d duplicate products", dupes)

    def close(self):
        self._f.close()

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def page_url(url, page_num):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page_num)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

# ---------------------------------------------------------------------------
# Extract products — fast JS, runs entirely in browser
# ---------------------------------------------------------------------------

EXTRACT_JS = """
() => {
    const results = [];
    const links = document.querySelectorAll('a[href*="/item/"]');
    const processed = new Set();

    for (const link of links) {
        const href = link.getAttribute('href') || '';
        const match = href.match(/\\/item\\/(\\d+)\\.html/);
        if (!match) continue;
        const pid = match[1];
        if (processed.has(pid)) continue;
        processed.add(pid);

        // Walk up max 3 levels to find the closest small card container
        let card = link;
        for (let i = 0; i < 3; i++) {
            if (!card.parentElement) break;
            const p = card.parentElement;
            // Stop if parent is too big (likely a section container)
            if (p.querySelectorAll('a[href*="/item/"]').length > 1) break;
            card = p;
        }

        // Image — prefer img inside the link or card
        let image = '';
        const img = link.querySelector('img') || card.querySelector('img');
        if (img) {
            image = img.getAttribute('src') || img.getAttribute('data-src') || '';
            if (image.includes('48x48') || image.includes('placeholder')) image = '';
        }

        // Title — try multiple strategies
        let title = '';
        // 1. Alt text on image (usually the best on AliExpress)
        if (img) title = (img.getAttribute('alt') || '').trim();
        // 2. Title/aria-label on the link itself
        if (!title) title = (link.getAttribute('title') || '').trim();
        if (!title) title = (link.getAttribute('aria-label') || '').trim();
        // 3. Look for title-like elements inside card
        if (!title) {
            const titleEl = card.querySelector('[class*="title"],[class*="Title"],h1,h2,h3');
            if (titleEl) {
                const t = titleEl.innerText.trim();
                // Skip section headers
                if (t.length > 5 && !['New arrivals','Hot deals','Related Searches','More to love'].includes(t)) {
                    title = t;
                }
            }
        }
        // 4. Fallback: longest text line in card
        if (!title) {
            const text = card.innerText || '';
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 10);
            if (lines.length) title = lines.reduce((a, b) => a.length > b.length ? a : b);
        }

        // Price — look for currency + number patterns
        let price = 'N/A';
        const cardText = card.innerText || '';
        // Match patterns like $5.99, US $12.30, € 8,99
        const pm = cardText.match(/(?:US\\s*)?[\\$€£¥₽]\\s*[\\d,]+\\.?\\d*/);
        if (pm) {
            price = pm[0].trim();
        } else {
            // Try matching just numbers near currency symbols on the page
            const pm2 = cardText.match(/\\d+[,.]\\d{2}/);
            if (pm2) price = '$' + pm2[0];
        }

        // Sales
        let sales = '';
        const sm = cardText.match(/(\\d[\\d,\\.]*\\+?)\\s*[Ss]old/);
        if (sm) sales = sm[0].trim();

        // Skip items with generic/section titles
        if (['New arrivals','Hot deals','Related Searches','More to love',''].includes(title)) continue;

        results.push({ id: pid, title: title.substring(0, 300), price, image, sales, href });
    }
    return results;
}
"""


def extract(page):
    try:
        raw = page.evaluate(EXTRACT_JS)
    except Exception:
        return []

    products = []
    seen = set()
    for r in raw:
        pid = r["id"]
        url = f"https://www.aliexpress.com/item/{pid}.html"
        if url in seen:
            continue
        seen.add(url)
        img = r.get("image", "")
        if img.startswith("//"):
            img = "https:" + img
        products.append({
            "id": pid,
            "product_title": r["title"],
            "product_price": r["price"],
            "product_original_price": "",
            "product_discount": "",
            "product_url": url,
            "product_image": img,
            "product_rating": "",
            "store_name": "", "store_url": "", "store_id": "",
            "total_sales": r.get("sales", ""),
            "ship_from": "", "store_member_id": "",
            "trade_info": r.get("sales", ""),
            "shipping": "", "launch_time": "", "company_name": "",
        })
    return products

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------

def has_next(page, current):
    nxt = current + 1
    for sel in [
        f"a[href*='page={nxt}']",
        "a[class*='next']",
        "button[aria-label='Next']",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
        "li.next a",
        "[class*='pagination'] [class*='next']",
    ]:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                if el.is_visible():
                    return True
        except Exception:
            pass
    # Check for page number link
    try:
        els = page.query_selector_all(f"a:has-text('{nxt}'), button:has-text('{nxt}')")
        for el in els:
            if el.is_visible() and el.inner_text().strip() == str(nxt):
                return True
    except Exception:
        pass
    return False

# ---------------------------------------------------------------------------
# Wait for page to be ready, handle login/captcha
# ---------------------------------------------------------------------------

def is_captcha(tab):
    """Check if the current page is showing a CAPTCHA."""
    try:
        url = tab.url.lower()
        if "captcha" in url or "punch" in url or "sec.aliexpress" in url:
            return True
        # Check page content for CAPTCHA indicators
        body = tab.query_selector("body")
        if not body:
            return False
        text = (body.inner_text() or "").strip()
        # Only check short pages (CAPTCHAs have minimal text)
        if len(text) < 500:
            low = text.lower()
            if any(w in low for w in ["captcha", "verify", "robot", "slider", "puzzle", "human"]):
                return True
        # Check for common CAPTCHA elements
        for sel in ["#captcha", "[class*='captcha']", "[class*='Captcha']",
                     "#nc_1_n1z", ".nc-container", ".slider", "#baxia-dialog"]:
            el = tab.query_selector(sel)
            if el and el.is_visible():
                return True
    except Exception:
        pass
    return False


def wait_ready(tab, target):
    """Wait for page to load, handle login redirects and CAPTCHAs."""
    # Wait for first product link to appear
    try:
        tab.wait_for_selector("a[href*='/item/']", timeout=5000)
    except Exception:
        pass

    # Check for login redirect
    current = tab.url.lower()
    if "login" in current or "passport" in current:
        log.warning(">>> Login required! Log in in the browser window. <<<")
        print("\a", flush=True)
        while True:
            tab.wait_for_timeout(1000)
            current = tab.url.lower()
            if "login" not in current and "passport" not in current:
                log.info(">>> Login complete! Reloading target... <<<")
                break
        try:
            tab.goto(target, wait_until="domcontentloaded", timeout=30000)
            tab.wait_for_selector("a[href*='/item/']", timeout=5000)
        except Exception:
            pass
        return True

    # Check for CAPTCHA — wait for user to solve it
    if is_captcha(tab):
        log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
        print("\a", flush=True)
        while is_captcha(tab):
            tab.wait_for_timeout(2000)
        log.info(">>> CAPTCHA solved! Reloading target... <<<")
        # Reload original target after CAPTCHA
        try:
            tab.goto(target, wait_until="domcontentloaded", timeout=30000)
            tab.wait_for_selector("a[href*='/item/']", timeout=5000)
        except Exception:
            pass

    return True


def dismiss_popups(tab):
    for sel in ["button:has-text('Accept')", "button:has-text('OK')",
                "button:has-text('Got it')", ".comet-modal-close"]:
        try:
            btn = tab.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                tab.wait_for_timeout(100)
        except Exception:
            pass


SCROLL_JS = """
async () => {
    const step = window.innerHeight;
    const delay = ms => new Promise(r => setTimeout(r, ms));
    let h = document.body.scrollHeight;
    let y = 0;
    while (y < h) {
        y += step;
        window.scrollTo(0, y);
        await delay(150);
        h = document.body.scrollHeight;
    }
    window.scrollTo(0, document.body.scrollHeight);
    await delay(300);
}
"""


def scroll_and_extract(tab):
    """Scroll through the page, extract all products."""
    # Single JS call scrolls entire page — much faster than round-trips
    try:
        tab.evaluate(SCROLL_JS)
    except Exception:
        pass

    products = extract(tab)
    prev = len(products)

    # Infinite scroll — keep going if we're finding more
    stale = 0
    while stale < 2:
        try:
            tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            tab.wait_for_timeout(500)
        except Exception:
            break
        new = extract(tab)
        if len(new) > len(products):
            products = new
            stale = 0
        else:
            stale += 1

    # Scroll back to top for pagination detection
    try:
        tab.evaluate("window.scrollTo(0, 0)")
        tab.wait_for_timeout(100)
    except Exception:
        pass

    return products

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("urls_file")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    lines = Path(args.urls_file).read_text().splitlines()
    urls = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not urls:
        print("No URLs found.")
        sys.exit(1)

    ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    out = args.output or f"aliexpress_scrape_{ts}.csv"
    csv_out = LiveCSV(out)
    log.info("Output: %s", out)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)
        tab = context.new_page()

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] %s", i, len(urls), url)
            pg = 1

            while pg <= 100:
                target = page_url(url, pg) if pg > 1 else url
                log.info("  Page %d", pg)

                # Load page
                try:
                    tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    # Check if we landed on a CAPTCHA or login page
                    if is_captcha(tab):
                        log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
                        print("\a", flush=True)
                        while is_captcha(tab):
                            tab.wait_for_timeout(2000)
                        log.info(">>> CAPTCHA solved! Reloading... <<<")
                        try:
                            tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            log.warning("  Load error after CAPTCHA: %s", e)
                            break
                    else:
                        log.warning("  Load error: %s", e)
                        break

                # Wait for content, handle login/captcha
                wait_ready(tab, target)

                # Dismiss popups
                dismiss_popups(tab)

                # Scroll and extract all products
                products = scroll_and_extract(tab)

                if not products and pg > 1:
                    log.info("  No products on page %d — done.", pg)
                    break

                csv_out.add(products, url)
                log.info("  Page %d: %d products (total: %d)", pg, len(products), csv_out.count)

                if not has_next(tab, pg):
                    log.info("  No next page — done.")
                    break

                pg += 1
                time.sleep(random.uniform(0.2, 0.5))

        context.close()
        browser.close()

    csv_out.close()
    log.info("Done! %d products → %s", csv_out.count, out)


if __name__ == "__main__":
    main()
