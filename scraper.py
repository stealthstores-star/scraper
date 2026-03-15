#!/usr/bin/env python3
"""
AliExpress Product Scraper
==========================
Scrapes product listings from AliExpress search result pages and seller store
pages using Playwright (headless Chromium). Handles pagination, deduplication,
and exports results to a timestamped CSV.

Setup:
    pip install -r requirements.txt
    playwright install chromium

Usage:
    python scraper.py urls.txt
    python scraper.py urls.txt -o output.csv
    python scraper.py urls.txt --headed      # run with visible browser
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
BACKOFF_BASE = 4            # seconds — retry waits: 4, 8, 16
MIN_DELAY, MAX_DELAY = 2, 5  # random delay range between page loads
PAGE_LOAD_TIMEOUT = 60_000   # ms — max wait for network idle
MAX_PAGES = 100              # safety cap to avoid infinite pagination

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aliexpress_scraper")

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def classify_url(url: str) -> str:
    """Return 'search', 'store', or 'unknown'."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/w/" in path or "wholesale" in path or "SearchText" in parse_qs(parsed.query):
        return "search"
    if "/store/" in path:
        return "store"
    # Fallback: treat category-style pages as search
    if re.search(r"/category/\d+", path):
        return "search"
    return "unknown"


def build_page_url(url: str, page: int, url_type: str) -> str:
    """Return *url* modified to request the given page number."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)

    if url_type == "store":
        # Store pages use ?SearchText=&page=N  or  ?sortType=...&page=N
        qs["page"] = [str(page)]
    else:
        # Search pages use &page=N
        qs["page"] = [str(page)]

    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

# ---------------------------------------------------------------------------
# Scraping logic
# ---------------------------------------------------------------------------

def random_delay():
    """Sleep for a random interval to look more human."""
    delay = random.uniform(MIN_DELAY, MAX_DELAY)
    time.sleep(delay)


def extract_products(page) -> list[dict]:
    """
    Extract product cards from the current page DOM.

    AliExpress uses several card layouts; we try multiple selector strategies
    and merge results.
    """
    products = []
    seen_urls = set()

    # Strategy: find all product link+title+price groupings.
    # AliExpress renders cards as <a> wrappers or as divs with child <a> tags.
    # We look for common selectors across layouts.

    selectors = [
        # Global search results (2024-2026 layout)
        "div.search-item-card-wrapper-gallery",
        "div[class*='SearchResult'] a[href*='/item/']",
        # Unified card wrapper used on many pages
        "a.search-card-item",
        "a[href*='/item/'][class*='card']",
        # Store page cards
        "div.product-container",
        "div[class*='product-card']",
        # Very generic fallback: any link whose href points to an item page
        "a[href*='/item/']",
    ]

    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue

        for el in elements:
            try:
                product = _parse_card(page, el)
                if product and product["product_url"] not in seen_urls:
                    seen_urls.add(product["product_url"])
                    products.append(product)
            except Exception:
                continue

    return products


def _parse_card(page, el) -> dict | None:
    """Extract title, price, and URL from a single card element."""
    # --- URL ---
    href = el.get_attribute("href")
    if not href:
        # The element might be a wrapper div; look for the first child <a>
        link = el.query_selector("a[href*='/item/']")
        if link:
            href = link.get_attribute("href")
    if not href or "/item/" not in href:
        return None

    # Normalise to absolute URL
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.aliexpress.com" + href

    # Strip tracking query params but keep the item id
    product_url = _clean_product_url(href)

    # --- Title ---
    title = None
    for sel in ["h1", "h3", "h2", "[class*='title']", "[class*='Title']", "img"]:
        title_el = el.query_selector(sel)
        if title_el:
            title = title_el.inner_text().strip() if sel != "img" else title_el.get_attribute("alt")
            if title:
                break
    if not title:
        title = el.inner_text().strip()[:200]
    if not title:
        return None

    # --- Price ---
    price = None
    for sel in [
        "[class*='price'] span",
        "[class*='Price'] span",
        "[class*='price']",
        "[class*='Price']",
    ]:
        price_el = el.query_selector(sel)
        if price_el:
            price = price_el.inner_text().strip()
            if price:
                break
    # Sometimes the price is embedded in the card text — try regex fallback
    if not price:
        card_text = el.inner_text()
        m = re.search(r"[\$€£¥₽][\s]?\d[\d,\.]+", card_text)
        if m:
            price = m.group(0).strip()
    price = price or "N/A"

    return {"product_title": title, "price": price, "product_url": product_url}


def _clean_product_url(url: str) -> str:
    """Keep only the canonical /item/<id>.html path."""
    parsed = urlparse(url)
    m = re.search(r"(/item/\d+\.html)", parsed.path)
    if m:
        return f"https://www.aliexpress.com{m.group(1)}"
    # If the URL doesn't match the pattern, return it with query params stripped
    return urlunparse(parsed._replace(query="", fragment=""))

# ---------------------------------------------------------------------------
# Page-level scraping with retries
# ---------------------------------------------------------------------------

def scrape_page_with_retries(page, url: str) -> list[dict]:
    """Load a URL in *page* and extract products, retrying on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            # Give JS extra time to render product cards
            page.wait_for_timeout(3000)
            # Scroll down to trigger lazy-loaded cards
            _auto_scroll(page)
            products = extract_products(page)
            return products
        except PwTimeout:
            log.warning("Timeout on %s (attempt %d/%d)", url, attempt, MAX_RETRIES)
        except Exception as exc:
            log.warning("Error on %s (attempt %d/%d): %s", url, attempt, MAX_RETRIES, exc)
        if attempt < MAX_RETRIES:
            wait = BACKOFF_BASE * (2 ** (attempt - 1))
            log.info("Retrying in %ds …", wait)
            time.sleep(wait)
    log.error("Failed to scrape %s after %d attempts", url, MAX_RETRIES)
    return []


def _auto_scroll(page, pause: float = 0.8, max_scrolls: int = 12):
    """Scroll the page incrementally to trigger lazy-loaded content."""
    for _ in range(max_scrolls):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(int(pause * 1000))
    # Scroll back to top (some layouts reveal a "next page" button at the top)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def has_next_page(page, current_page: int) -> bool:
    """Detect whether a 'next page' element is present and clickable."""
    # Check for next-page buttons or pagination links
    for sel in [
        "button.next-pagination-item",          # older layout
        "a[class*='next']",                      # generic
        f"a[href*='page={current_page + 1}']",   # link-based pagination
        "button[aria-label='Next']",
        "li.next a",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
    ]:
        el = page.query_selector(sel)
        if el and el.is_visible():
            return True
    return False

# ---------------------------------------------------------------------------
# Main scraper orchestration
# ---------------------------------------------------------------------------

def scrape_url(browser, url: str) -> list[dict]:
    """
    Scrape all pages for a single AliExpress URL.
    Returns a list of product dicts (including 'source_url').
    """
    url_type = classify_url(url)
    if url_type == "unknown":
        log.warning("Unrecognised URL type — will attempt generic scrape: %s", url)
        url_type = "search"  # best-effort

    ua = random.choice(USER_AGENTS)
    context = browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    page = context.new_page()

    all_products: list[dict] = []
    current_page = 1

    try:
        while current_page <= MAX_PAGES:
            page_url = build_page_url(url, current_page, url_type) if current_page > 1 else url
            log.info("  Page %d → %s", current_page, page_url)

            products = scrape_page_with_retries(page, page_url)
            if not products and current_page > 1:
                log.info("  No products found on page %d — assuming end of results.", current_page)
                break

            for p in products:
                p["source_url"] = url
            all_products.extend(products)
            log.info("  Found %d products on page %d (total so far: %d)",
                     len(products), current_page, len(all_products))

            # Check for next page
            if not has_next_page(page, current_page):
                log.info("  No next page detected — done with this URL.")
                break

            current_page += 1
            random_delay()
    finally:
        context.close()

    return all_products

# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

def write_csv(products: list[dict], output_path: str):
    """Write deduplicated products to a CSV file."""
    seen = set()
    unique = []
    for p in products:
        if p["product_url"] not in seen:
            seen.add(p["product_url"])
            unique.append(p)

    fieldnames = ["product_title", "price", "product_url", "source_url"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    log.info("Wrote %d unique products to %s", len(unique), output_path)

# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def read_urls(filepath: str) -> list[str]:
    """Read URLs from a text file, skipping blanks and comments."""
    path = Path(filepath)
    if not path.is_file():
        log.error("File not found: %s", filepath)
        sys.exit(1)

    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)

    if not urls:
        log.error("No URLs found in %s", filepath)
        sys.exit(1)

    log.info("Loaded %d URL(s) from %s", len(urls), filepath)
    return urls

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Scrape AliExpress search/store pages and export to CSV.",
    )
    parser.add_argument(
        "urls_file",
        help="Path to a text file containing AliExpress URLs (one per line).",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output CSV filename. Default: aliexpress_scrape_<timestamp>.csv",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run with a visible browser window (useful for debugging).",
    )
    args = parser.parse_args()

    urls = read_urls(args.urls_file)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = args.output or f"aliexpress_scrape_{timestamp}.csv"

    all_products: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] Scraping: %s", i, len(urls), url)
            try:
                products = scrape_url(browser, url)
                all_products.extend(products)
                log.info("[%d/%d] Collected %d products from this URL (running total: %d)",
                         i, len(urls), len(products), len(all_products))
            except Exception as exc:
                log.error("[%d/%d] Failed to scrape %s: %s", i, len(urls), url, exc)
            random_delay()

        browser.close()

    if all_products:
        write_csv(all_products, output_path)
    else:
        log.warning("No products were scraped. CSV not created.")


if __name__ == "__main__":
    main()
