#!/usr/bin/env python3
"""
AliExpress Product Scraper
==========================
Scrapes product listings from AliExpress search result pages and seller store
pages using Playwright (headless Chromium). Handles pagination, deduplication,
and exports results to a timestamped CSV.

Setup:
    pip3 install -r requirements.txt
    python3 -m playwright install chromium

Usage:
    python3 scraper.py urls.txt
    python3 scraper.py urls.txt -o output.csv
    python3 scraper.py urls.txt --headed      # run with visible browser
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

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_RETRIES = 3
BACKOFF_BASE = 4            # seconds — retry waits: 4, 8, 16
MIN_DELAY, MAX_DELAY = 2, 5  # random delay range between page loads
PAGE_LOAD_TIMEOUT = 60_000   # ms — max wait for page load
MAX_PAGES = 100              # safety cap to avoid infinite pagination

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aliexpress_scraper")

# ---------------------------------------------------------------------------
# Stealth JavaScript — patches navigator properties that betray automation
# ---------------------------------------------------------------------------

STEALTH_JS = """
() => {
    // Overwrite the 'webdriver' property on navigator
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Overwrite chrome runtime to look like a real browser
    window.chrome = { runtime: {} };

    // Overwrite permissions query
    const originalQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters);

    // Overwrite plugins to look non-empty
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Overwrite languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
}
"""

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def classify_url(url: str) -> str:
    """Return 'search', 'store', or 'unknown'."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/w/" in path or "wholesale" in path:
        return "search"
    if "/store/" in path:
        return "store"
    if re.search(r"/category/\d+", path):
        return "search"
    return "unknown"


def build_page_url(url: str, page: int, url_type: str) -> str:
    """Return *url* modified to request the given page number."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
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


def dismiss_popups(page):
    """Try to close cookie consent banners and other popups."""
    popup_selectors = [
        # Cookie consent buttons
        "button[data-role='gdpr-accept']",
        "button[data-role='accept-all']",
        "button:has-text('Accept')",
        "button:has-text('Accept All')",
        "button:has-text('Accept Cookies')",
        "button:has-text('OK')",
        "button:has-text('Got it')",
        "button:has-text('Agree')",
        # Close buttons on overlay modals
        "div[class*='overlay'] button[class*='close']",
        "div[class*='modal'] button[class*='close']",
        "div[class*='popup'] button[class*='close']",
        "[class*='cookie'] button",
        # AliExpress-specific close buttons
        ".next-dialog-close",
        ".comet-modal-close",
    ]
    for sel in popup_selectors:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                log.info("    Dismissed popup: %s", sel)
                page.wait_for_timeout(500)
        except Exception:
            pass


def extract_products(page) -> list[dict]:
    """
    Extract product cards from the current page DOM.
    Uses multiple strategies to find product data.
    """
    products = []
    seen_urls = set()

    # ---- Strategy 1: Parse structured data from page scripts ----
    # AliExpress often embeds product JSON in script tags
    try:
        scripts = page.query_selector_all("script")
        for script in scripts:
            text = script.inner_text()
            if not text:
                continue
            # Look for JSON arrays with item data
            for pattern in [
                r'"items"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
                r'"itemList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
                r'"productList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            ]:
                m = re.search(pattern, text)
                if m:
                    try:
                        items = json.loads(m.group(1))
                        for item in items:
                            product = _parse_json_item(item)
                            if product and product["product_url"] not in seen_urls:
                                seen_urls.add(product["product_url"])
                                products.append(product)
                    except (json.JSONDecodeError, TypeError):
                        pass
    except Exception:
        pass

    if products:
        log.info("    Extracted %d products from embedded JSON", len(products))
        return products

    # ---- Strategy 2: DOM-based extraction with broad selectors ----
    selectors = [
        # Search results card wrappers (2024-2026 layouts)
        "div[class*='search-item-card']",
        "div[class*='SearchResult'] a[href*='/item/']",
        "a.search-card-item",
        # Store page cards
        "div[class*='product-card']",
        "div[class*='product-container']",
        "div[class*='ProductCard']",
        # Generic card selectors
        "a[href*='/item/'][class*='card']",
        "div[class*='card'] a[href*='/item/']",
        # Very generic fallback: any link to an item page
        "a[href*='/item/']",
    ]

    for selector in selectors:
        try:
            elements = page.query_selector_all(selector)
        except Exception:
            continue

        for el in elements:
            try:
                product = _parse_card(el)
                if product and product["product_url"] not in seen_urls:
                    seen_urls.add(product["product_url"])
                    products.append(product)
            except Exception:
                continue

        # If we found products with a specific selector, stop — avoid duplicates
        # from more generic selectors
        if products:
            break

    return products


def _parse_json_item(item: dict) -> dict | None:
    """Extract product info from a JSON item object."""
    # AliExpress JSON uses various key names
    title = (
        item.get("title") or item.get("productTitle") or
        item.get("name") or item.get("subject") or ""
    )
    if not title:
        return None

    # Price
    price = (
        item.get("price") or item.get("salePrice") or
        item.get("minPrice") or item.get("formattedPrice") or "N/A"
    )
    if isinstance(price, dict):
        price = price.get("formattedPrice") or price.get("minPrice") or "N/A"
    price = str(price)

    # URL
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
    """Extract title, price, and URL from a single DOM card element."""
    # --- URL ---
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

    # --- Title ---
    title = None
    for sel in ["h1", "h3", "h2", "[class*='title']", "[class*='Title']", "img"]:
        title_el = el.query_selector(sel)
        if title_el:
            if sel == "img":
                title = (title_el.get_attribute("alt") or "").strip()
            else:
                try:
                    title = title_el.inner_text().strip()
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
            try:
                price = price_el.inner_text().strip()
            except Exception:
                pass
            if price:
                break
    if not price:
        try:
            card_text = el.inner_text()
            m = re.search(r"[\$€£¥₽]\s?\d[\d,\.]+", card_text)
            if m:
                price = m.group(0).strip()
        except Exception:
            pass
    price = price or "N/A"

    return {"product_title": title, "price": price, "product_url": product_url}


def _clean_product_url(url: str) -> str:
    """Keep only the canonical /item/<id>.html path."""
    parsed = urlparse(url)
    m = re.search(r"(/item/\d+\.html)", parsed.path)
    if m:
        return f"https://www.aliexpress.com{m.group(1)}"
    return urlunparse(parsed._replace(query="", fragment=""))

# ---------------------------------------------------------------------------
# Page-level scraping with retries
# ---------------------------------------------------------------------------

def scrape_page_with_retries(page, url: str, debug_dir: str | None = None) -> list[dict]:
    """Load a URL in *page* and extract products, retrying on failure."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

            # Wait for content to render
            page.wait_for_timeout(3000)

            # Dismiss cookie/consent popups
            dismiss_popups(page)

            # Try to wait for product elements to appear
            try:
                page.wait_for_selector(
                    "a[href*='/item/'], div[class*='product'], div[class*='card']",
                    timeout=10_000,
                )
            except PwTimeout:
                log.info("    No product selectors found, will try JSON extraction")

            # Scroll down to trigger lazy-loaded cards
            _auto_scroll(page)

            products = extract_products(page)

            # Debug: save screenshot + HTML when no products found
            if not products and debug_dir:
                _save_debug(page, url, debug_dir)

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


def _save_debug(page, url: str, debug_dir: str):
    """Save a screenshot and HTML dump for debugging."""
    Path(debug_dir).mkdir(exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    slug = re.sub(r"[^\w]", "_", urlparse(url).path)[:40]

    screenshot_path = f"{debug_dir}/debug_{ts}_{slug}.png"
    html_path = f"{debug_dir}/debug_{ts}_{slug}.html"

    try:
        page.screenshot(path=screenshot_path, full_page=True)
        log.info("    Debug screenshot saved: %s", screenshot_path)
    except Exception as exc:
        log.warning("    Could not save screenshot: %s", exc)

    try:
        html = page.content()
        Path(html_path).write_text(html, encoding="utf-8")
        log.info("    Debug HTML saved: %s", html_path)
    except Exception as exc:
        log.warning("    Could not save HTML: %s", exc)


def _auto_scroll(page, pause: float = 0.8, max_scrolls: int = 12):
    """Scroll the page incrementally to trigger lazy-loaded content."""
    for _ in range(max_scrolls):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(int(pause * 1000))
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)


def has_next_page(page, current_page: int) -> bool:
    """Detect whether a 'next page' element is present and clickable."""
    for sel in [
        "button.next-pagination-item",
        "a[class*='next']",
        f"a[href*='page={current_page + 1}']",
        "button[aria-label='Next']",
        "li.next a",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
        "nav[class*='pagination'] a:last-child",
        "ul[class*='pagination'] li:last-child a",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False

# ---------------------------------------------------------------------------
# Main scraper orchestration
# ---------------------------------------------------------------------------

def scrape_url(browser, url: str, debug_dir: str) -> list[dict]:
    """
    Scrape all pages for a single AliExpress URL.
    Returns a list of product dicts (including 'source_url').
    """
    url_type = classify_url(url)
    if url_type == "unknown":
        log.warning("Unrecognised URL type — will attempt generic scrape: %s", url)
        url_type = "search"

    ua = random.choice(USER_AGENTS)
    context = browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        # Pretend to be a real user with timezone & geolocation
        timezone_id="America/New_York",
    )
    # Inject stealth scripts before any page loads
    context.add_init_script(STEALTH_JS)

    page = context.new_page()

    all_products: list[dict] = []
    current_page = 1

    try:
        while current_page <= MAX_PAGES:
            page_url = build_page_url(url, current_page, url_type) if current_page > 1 else url
            log.info("  Page %d → %s", current_page, page_url)

            products = scrape_page_with_retries(page, page_url, debug_dir)
            if not products and current_page > 1:
                log.info("  No products found on page %d — assuming end of results.", current_page)
                break

            for p in products:
                p["source_url"] = url
            all_products.extend(products)
            log.info("  Found %d products on page %d (total so far: %d)",
                     len(products), current_page, len(all_products))

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
    parser.add_argument(
        "--debug-dir",
        default="debug_output",
        help="Directory for debug screenshots/HTML when 0 products found.",
    )
    args = parser.parse_args()

    urls = read_urls(args.urls_file)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = args.output or f"aliexpress_scrape_{timestamp}.csv"

    all_products: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        for i, url in enumerate(urls, 1):
            log.info("[%d/%d] Scraping: %s", i, len(urls), url)
            try:
                products = scrape_url(browser, url, args.debug_dir)
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
        log.warning("Check the '%s' folder for screenshots showing what the browser saw.", args.debug_dir)


if __name__ == "__main__":
    main()
