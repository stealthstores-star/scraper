#!/usr/bin/env python3
"""
AliExpress Product Scraper
==========================
Scrapes product listings from AliExpress search result pages and seller store
pages using Playwright (headed Chromium). Handles pagination, deduplication,
CAPTCHA pauses, and writes results to CSV in real time.

Setup:
    pip3 install -r requirements.txt
    python3 -m playwright install chromium

Usage:
    python3 scraper.py urls.txt
    python3 scraper.py urls.txt -o output.csv
    python3 scraper.py urls.txt --headless    # no browser window (may hit CAPTCHAs)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
BACKOFF_BASE = 3
MIN_DELAY, MAX_DELAY = 1, 3       # shorter delays between pages
PAGE_LOAD_TIMEOUT = 45_000
MAX_PAGES = 100
CAPTCHA_POLL_INTERVAL = 2         # seconds between CAPTCHA checks
CAPTCHA_MAX_WAIT = 300            # max 5 minutes to solve a CAPTCHA

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aliexpress_scraper")

# ---------------------------------------------------------------------------
# Stealth JS — hide automation signals
# ---------------------------------------------------------------------------

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {} };
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(p);
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
}
"""

# ---------------------------------------------------------------------------
# CSV writer — appends rows in real time
# ---------------------------------------------------------------------------

class LiveCSV:
    """Writes product rows to CSV as they are discovered (no buffering)."""

    FIELDS = ["product_title", "price", "product_url", "source_url"]

    def __init__(self, path: str):
        self.path = path
        self.seen: set[str] = set()
        self.count = 0
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.FIELDS)
        self._writer.writeheader()
        self._file.flush()

    def add(self, products: list[dict]):
        """Append new unique products and flush immediately."""
        for p in products:
            if p["product_url"] not in self.seen:
                self.seen.add(p["product_url"])
                self._writer.writerow(p)
                self.count += 1
        self._file.flush()

    def close(self):
        self._file.close()

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def classify_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/w/" in path or "wholesale" in path:
        return "search"
    if "/store/" in path:
        return "store"
    if re.search(r"/category/\d+", path):
        return "search"
    return "unknown"


def build_page_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["page"] = [str(page)]
    return urlunparse(parsed._replace(query=urlencode(qs, doseq=True)))

# ---------------------------------------------------------------------------
# CAPTCHA detection and waiting
# ---------------------------------------------------------------------------

def wait_for_captcha(page) -> bool:
    """
    Check if we're on a CAPTCHA page. If so, print a message and wait
    for the user to solve it manually. Returns True if CAPTCHA was detected.
    """
    captcha_indicators = [
        "text=We need to verify",
        "text=check if you are a robot",
        "text=verify you are human",
        "text=slide to verify",
        "text=Please verify",
        "iframe[src*='captcha']",
        "div[id*='captcha']",
        "div[class*='captcha']",
        "div[class*='baxia']",
    ]

    detected = False
    for sel in captcha_indicators:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                detected = True
                break
        except Exception:
            pass

    # Also check page title / body text
    if not detected:
        try:
            body = page.inner_text("body")
            if any(phrase in body.lower() for phrase in [
                "robot", "captcha", "verify you", "slide to verify",
                "check if you are", "unusual traffic",
            ]):
                detected = True
        except Exception:
            pass

    if not detected:
        return False

    log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
    waited = 0
    while waited < CAPTCHA_MAX_WAIT:
        time.sleep(CAPTCHA_POLL_INTERVAL)
        waited += CAPTCHA_POLL_INTERVAL

        # Check if CAPTCHA is gone (page has product links or no captcha text)
        try:
            body = page.inner_text("body")
            still_captcha = any(phrase in body.lower() for phrase in [
                "robot", "captcha", "verify you", "slide to verify",
                "check if you are",
            ])
            if not still_captcha:
                log.info(">>> CAPTCHA solved! Continuing... <<<")
                page.wait_for_timeout(2000)  # let page finish loading
                return True
        except Exception:
            pass

    log.error("CAPTCHA wait timed out after %ds", CAPTCHA_MAX_WAIT)
    return True

# ---------------------------------------------------------------------------
# Popup dismissal
# ---------------------------------------------------------------------------

def dismiss_popups(page):
    for sel in [
        "button[data-role='gdpr-accept']",
        "button[data-role='accept-all']",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Agree')",
        "div[class*='overlay'] button[class*='close']",
        "div[class*='modal'] button[class*='close']",
        "div[class*='popup'] button[class*='close']",
        ".next-dialog-close",
        ".comet-modal-close",
    ]:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                page.wait_for_timeout(300)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Product extraction
# ---------------------------------------------------------------------------

def extract_products(page) -> list[dict]:
    products = []
    seen_urls: set[str] = set()

    # Strategy 1: JSON embedded in page scripts (fastest)
    try:
        page_html = page.content()
        for pattern in [
            r'"items"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"itemList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"productList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
        ]:
            for m in re.finditer(pattern, page_html):
                try:
                    items = json.loads(m.group(1))
                    for item in items:
                        p = _parse_json_item(item)
                        if p and p["product_url"] not in seen_urls:
                            seen_urls.add(p["product_url"])
                            products.append(p)
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass

    if products:
        return products

    # Strategy 2: DOM selectors
    for selector in [
        "div[class*='search-item-card']",
        "a.search-card-item",
        "div[class*='product-card']",
        "div[class*='ProductCard']",
        "div[class*='product-container']",
        "a[href*='/item/'][class*='card']",
        "div[class*='card'] a[href*='/item/']",
        "a[href*='/item/']",
    ]:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue
        for el in elements:
            try:
                p = _parse_card(el)
                if p and p["product_url"] not in seen_urls:
                    seen_urls.add(p["product_url"])
                    products.append(p)
            except Exception:
                continue
        if products:
            break

    return products


def _parse_json_item(item: dict) -> dict | None:
    title = (
        item.get("title") or item.get("productTitle") or
        item.get("name") or item.get("subject") or ""
    )
    if not title:
        return None

    price = (
        item.get("price") or item.get("salePrice") or
        item.get("minPrice") or item.get("formattedPrice") or "N/A"
    )
    if isinstance(price, dict):
        price = price.get("formattedPrice") or price.get("minPrice") or "N/A"
    price = str(price)

    product_id = str(
        item.get("productId") or item.get("itemId") or
        item.get("id") or item.get("productDetailUrl") or ""
    )
    if product_id.startswith("http"):
        product_url = _clean_product_url(product_id)
    elif product_id.isdigit():
        product_url = f"https://www.aliexpress.com/item/{product_id}.html"
    else:
        return None

    return {"product_title": title.strip(), "price": price, "product_url": product_url}


def _parse_card(el) -> dict | None:
    href = el.get_attribute("href")
    if not href:
        link = el.query_selector("a[href*='/item/']")
        if link:
            href = link.get_attribute("href")
    if not href or "/item/" not in href:
        return None

    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.aliexpress.com" + href

    product_url = _clean_product_url(href)

    title = None
    for sel in ["h1", "h3", "h2", "[class*='title']", "[class*='Title']", "img"]:
        te = el.query_selector(sel)
        if te:
            if sel == "img":
                title = (te.get_attribute("alt") or "").strip()
            else:
                try:
                    title = te.inner_text().strip()
                except Exception:
                    pass
            if title:
                break
    if not title:
        try:
            title = el.inner_text().strip()[:200]
        except Exception:
            pass
    if not title:
        return None

    price = None
    for sel in [
        "[class*='price'] span", "[class*='Price'] span",
        "[class*='price']", "[class*='Price']",
    ]:
        pe = el.query_selector(sel)
        if pe:
            try:
                price = pe.inner_text().strip()
            except Exception:
                pass
            if price:
                break
    if not price:
        try:
            m = re.search(r"[\$€£¥₽]\s?\d[\d,\.]+", el.inner_text())
            if m:
                price = m.group(0).strip()
        except Exception:
            pass
    price = price or "N/A"

    return {"product_title": title, "price": price, "product_url": product_url}


def _clean_product_url(url: str) -> str:
    parsed = urlparse(url)
    m = re.search(r"(/item/\d+\.html)", parsed.path)
    if m:
        return f"https://www.aliexpress.com{m.group(1)}"
    return urlunparse(parsed._replace(query="", fragment=""))

# ---------------------------------------------------------------------------
# Page scraping with CAPTCHA handling and retries
# ---------------------------------------------------------------------------

def scrape_page(page, url: str) -> list[dict]:
    """Load URL, handle CAPTCHA, extract products. Retries on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_timeout(2000)

            # Check for CAPTCHA and wait for user to solve it
            wait_for_captcha(page)

            # Dismiss cookie popups
            dismiss_popups(page)

            # Wait briefly for product elements
            try:
                page.wait_for_selector(
                    "a[href*='/item/'], div[class*='product'], div[class*='card']",
                    timeout=8_000,
                )
            except PwTimeout:
                pass

            # Quick scroll to load lazy content (faster than before)
            _fast_scroll(page)

            products = extract_products(page)
            return products

        except PwTimeout:
            log.warning("Timeout (attempt %d/%d)", attempt, MAX_RETRIES)
        except Exception as exc:
            log.warning("Error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES:
            wait = BACKOFF_BASE * attempt
            log.info("Retrying in %ds…", wait)
            time.sleep(wait)

    log.error("Failed after %d attempts: %s", MAX_RETRIES, url)
    return []


def _fast_scroll(page, max_scrolls: int = 6):
    """Faster scroll — fewer steps, shorter pauses."""
    for _ in range(max_scrolls):
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def has_next_page(page, current_page: int) -> bool:
    for sel in [
        f"a[href*='page={current_page + 1}']",
        "a[class*='next']",
        "button[aria-label='Next']",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
        "li.next a",
        "nav[class*='pagination'] a:last-child",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False

# ---------------------------------------------------------------------------
# Scrape all pages for one URL
# ---------------------------------------------------------------------------

def scrape_url(browser, url: str, csv_writer: LiveCSV):
    """Scrape all pages for a URL, writing products to CSV in real time."""
    url_type = classify_url(url)
    if url_type == "unknown":
        log.warning("Unknown URL type, trying generic scrape: %s", url)

    ua = random.choice(USER_AGENTS)
    context = browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    context.add_init_script(STEALTH_JS)
    page = context.new_page()

    page_num = 1
    url_total = 0

    try:
        while page_num <= MAX_PAGES:
            page_url = build_page_url(url, page_num) if page_num > 1 else url
            log.info("  Page %d → %s", page_num, page_url[:100])

            products = scrape_page(page, page_url)

            if not products and page_num > 1:
                log.info("  No products on page %d — done with this URL.", page_num)
                break

            # Tag with source and write to CSV immediately
            for p in products:
                p["source_url"] = url
            csv_writer.add(products)
            url_total += len(products)

            log.info("  Page %d: %d products (URL total: %d, CSV total: %d)",
                     page_num, len(products), url_total, csv_writer.count)

            if not has_next_page(page, page_num):
                log.info("  No next page — done.")
                break

            page_num += 1
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    finally:
        context.close()

    return url_total

# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def read_urls(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.is_file():
        log.error("File not found: %s", filepath)
        sys.exit(1)
    urls = [
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        log.error("No URLs found in %s", filepath)
        sys.exit(1)
    log.info("Loaded %d URL(s) from %s", len(urls), filepath)
    return urls

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape AliExpress search/store pages to CSV.",
    )
    parser.add_argument("urls_file", help="Text file with AliExpress URLs.")
    parser.add_argument("-o", "--output", default=None, help="Output CSV filename.")
    parser.add_argument("--headless", action="store_true",
                        help="Run without a browser window (default is headed/visible).")
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = args.output or f"aliexpress_scrape_{timestamp}.csv"

    csv_writer = LiveCSV(output_path)
    log.info("Writing results live to: %s", output_path)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] Scraping: %s", i, len(urls), url)
            try:
                count = scrape_url(browser, url, csv_writer)
                log.info("[%d/%d] Got %d products (CSV total: %d)",
                         i, len(urls), count, csv_writer.count)
            except Exception as exc:
                log.error("[%d/%d] Failed: %s — %s", i, len(urls), url, exc)
            if i < len(urls):
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        browser.close()

    csv_writer.close()

    if csv_writer.count:
        log.info("Done! %d unique products saved to %s", csv_writer.count, output_path)
    else:
        log.warning("No products scraped.")


if __name__ == "__main__":
    main()
