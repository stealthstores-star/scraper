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
        self.seen = set()
        self.count = 0
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=FIELDS, extrasaction="ignore")
        self._w.writeheader()
        self._f.flush()

    def add(self, rows, source_url):
        for r in rows:
            key = r.get("product_url", "")
            if key and key not in self.seen:
                self.seen.add(key)
                r["source_url"] = source_url
                self._w.writerow(r)
                self.count += 1
        self._f.flush()

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

        // Walk up to find product card
        let card = link;
        for (let i = 0; i < 5; i++) {
            if (!card.parentElement) break;
            card = card.parentElement;
            const cls = card.className || '';
            if (cls.includes('card') || cls.includes('Card') ||
                cls.includes('item') || cls.includes('Item') ||
                cls.includes('product') || cls.includes('Product')) break;
        }

        const text = card.innerText || '';

        // Image
        const img = card.querySelector('img');
        let image = '';
        if (img) image = img.getAttribute('src') || img.getAttribute('data-src') || '';

        // Title
        let title = '';
        const titleEl = card.querySelector('h1,h2,h3,[class*="title"],[class*="Title"]');
        if (titleEl) title = titleEl.innerText.trim();
        if (!title && img) title = (img.getAttribute('alt') || '').trim();
        if (!title) {
            const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 3);
            if (lines.length) title = lines[0];
        }

        // Price
        let price = 'N/A';
        const pm = text.match(/[\\$€£¥₽]\\s?[\\d,\\.]+/);
        if (pm) price = pm[0].trim();

        // Sales
        let sales = '';
        const sm = text.match(/(\\d[\\d,\\.]*\\+?)\\s*sold/i);
        if (sm) sales = sm[0].trim();

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

def wait_ready(tab, target):
    """Wait for page to load, handle login redirects. Returns True if ready."""
    # Wait for first product link to appear
    try:
        tab.wait_for_selector("a[href*='/item/']", timeout=10000)
    except Exception:
        pass

    # Check for login redirect
    for _ in range(2):  # check twice in case of delayed redirect
        current = tab.url.lower()
        if "login" in current or "passport" in current:
            log.warning(">>> Login required! Log in in the browser window. <<<")
            print("\a", flush=True)
            # Wait for user to log in
            while True:
                tab.wait_for_timeout(2000)
                current = tab.url.lower()
                if "login" not in current and "passport" not in current:
                    log.info(">>> Login complete! Reloading target... <<<")
                    break
            # Reload original target
            try:
                tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                tab.wait_for_selector("a[href*='/item/']", timeout=10000)
            except Exception:
                pass
            return True
        tab.wait_for_timeout(500)

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


def scroll_and_extract(tab):
    """Scroll through the page, extract all products."""
    # First extraction before scrolling
    products = extract(tab)
    prev = len(products)

    # Scroll down the page in viewport steps to trigger lazy loaders
    try:
        height = tab.evaluate("document.body.scrollHeight")
        step = tab.evaluate("window.innerHeight")
        y = 0
        while y < height:
            y += step
            tab.evaluate(f"window.scrollTo(0, {y})")
            tab.wait_for_timeout(200)
        # Hit absolute bottom
        tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        tab.wait_for_timeout(500)
    except Exception:
        pass

    # Re-extract after full scroll
    products = extract(tab)

    # If we got more, keep scrolling (infinite scroll pages)
    if len(products) > prev:
        stale = 0
        while stale < 3:
            try:
                tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                tab.wait_for_timeout(1000)
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
        tab.wait_for_timeout(300)
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

        # Block fonts/media for speed — keep images (needed for CAPTCHA), JS and CSS
        def block_heavy(route):
            if route.request.resource_type in ("font", "media"):
                route.abort()
            else:
                route.continue_()
        tab.route("**/*", block_heavy)

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
                    log.warning("  Load error: %s", e)
                    break

                # Wait for content, handle login
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
                time.sleep(random.uniform(0.5, 1.5))

        context.close()
        browser.close()

    csv_out.close()
    log.info("Done! %d products → %s", csv_out.count, out)


if __name__ == "__main__":
    main()
