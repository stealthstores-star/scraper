#!/usr/bin/env python3
"""
AliExpress Product Scraper
==========================
Scrapes product listings from AliExpress search result pages and seller store
pages using Playwright with your real Chrome browser. Handles pagination,
deduplication, CAPTCHA pauses, and writes results to CSV in real time.

Setup:
    pip3 install -r requirements.txt
    python3 -m playwright install chromium

Usage:
    python3 scraper.py urls.txt
    python3 scraper.py urls.txt -o output.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import random
import re
import subprocess
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
MIN_DELAY, MAX_DELAY = 1, 3
PAGE_LOAD_TIMEOUT = 45_000
MAX_PAGES = 100
CAPTCHA_POLL_INTERVAL = 2
CAPTCHA_MAX_WAIT = 300

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aliexpress_scraper")

# ---------------------------------------------------------------------------
# Find real Chrome on the system
# ---------------------------------------------------------------------------

def find_chrome() -> str | None:
    """Find the real Chrome/Chromium executable on this machine."""
    system = platform.system()

    if system == "Darwin":  # macOS
        candidates = [
            # Edge first since it's the user's main browser
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    elif system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        progfiles = os.environ.get("PROGRAMFILES", "C:\\Program Files")
        progfiles86 = os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")
        candidates = [
            os.path.join(progfiles86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(progfiles, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(progfiles, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(progfiles86, "Google", "Chrome", "Application", "chrome.exe"),
        ]
    else:  # Linux
        candidates = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
        ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Try finding via `which`
    for name in ["microsoft-edge", "google-chrome", "google-chrome-stable", "chromium"]:
        try:
            result = subprocess.run(["which", name], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass

    return None

# ---------------------------------------------------------------------------
# Stealth JS — fallback for when using Playwright's bundled Chromium
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
# CAPTCHA detection — only opens visible window if needed
# ---------------------------------------------------------------------------

def check_for_captcha(page) -> bool:
    """Return True if the current page is a CAPTCHA challenge page."""
    try:
        # Check page title first — CAPTCHA pages have distinct titles
        title = page.title().lower()
        if any(word in title for word in ["captcha", "verify", "robot", "security"]):
            return True

        # Check for CAPTCHA-specific elements
        for sel in [
            "iframe[src*='captcha']",
            "div[id*='captcha']",
            "div[class*='captcha']",
            "div[class*='baxia']",
        ]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass

        # Check body text — but only match full phrases, not stray words
        body = page.inner_text("body").lower()
        # Only flag as CAPTCHA if the page has very little content (a real
        # product page has lots of text, a CAPTCHA page has almost none)
        if len(body) < 500 and any(phrase in body for phrase in [
            "check if you are a robot",
            "verify you are human",
            "slide to verify",
            "unusual traffic",
            "security check",
            "we need to verify",
        ]):
            return True

        return False
    except Exception:
        return False

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

    # Strategy 1: JSON from page source (fastest, most reliable)
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
# Page scraping
# ---------------------------------------------------------------------------

def scrape_page(page, url: str) -> list[dict]:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            page.wait_for_timeout(2000)

            # CAPTCHA check — wait for user to solve it in the visible window
            if check_for_captcha(page):
                _notify_captcha()
                log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
                waited = 0
                while waited < CAPTCHA_MAX_WAIT:
                    time.sleep(CAPTCHA_POLL_INTERVAL)
                    waited += CAPTCHA_POLL_INTERVAL
                    if not check_for_captcha(page):
                        log.info(">>> CAPTCHA solved! Continuing... <<<")
                        page.wait_for_timeout(2000)
                        break
                else:
                    log.error("CAPTCHA wait timed out after %ds", CAPTCHA_MAX_WAIT)
                    return []

            dismiss_popups(page)

            try:
                page.wait_for_selector(
                    "a[href*='/item/'], div[class*='product'], div[class*='card']",
                    timeout=8_000,
                )
            except PwTimeout:
                pass

            _fast_scroll(page)
            return extract_products(page)

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

def _make_context(browser):
    """Create a new browser context with stealth settings."""
    ctx = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    ctx.add_init_script(STEALTH_JS)
    return ctx


def _notify_captcha():
    """Send a macOS notification and beep to alert the user."""
    try:
        subprocess.run([
            "osascript", "-e",
            'display notification "CAPTCHA detected — solve it in the browser window!" '
            'with title "AliExpress Scraper" sound name "Glass"'
        ], capture_output=True, timeout=5)
    except Exception:
        pass
    # Also beep the terminal
    print("\a", end="", flush=True)


def solve_captcha_headed(pw, url: str, chrome_path: str | None):
    """
    Open a visible browser window on the CAPTCHA page, wait for the user
    to solve it, then close the window and return.
    """
    log.warning(">>> CAPTCHA detected! Opening browser window — solve it there. <<<")
    _notify_captcha()

    # Use Playwright's bundled Chromium for the CAPTCHA window — the user's
    # real browser (Edge) may already be running and macOS won't allow a
    # second instance. Playwright Chromium is a separate binary so it always works.
    headed_browser = pw.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--window-position=100,100",
            "--window-size=1200,900",
        ],
    )
    ctx = _make_context(headed_browser)
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)

    # Bring window to front
    page.bring_to_front()

    # Wait for CAPTCHA to be solved (up to 5 minutes)
    waited = 0
    while waited < CAPTCHA_MAX_WAIT:
        time.sleep(CAPTCHA_POLL_INTERVAL)
        waited += CAPTCHA_POLL_INTERVAL
        if not check_for_captcha(page):
            log.info(">>> CAPTCHA solved! Closing window and resuming... <<<")
            page.wait_for_timeout(1000)
            ctx.close()
            headed_browser.close()
            return

    log.error("CAPTCHA wait timed out after %ds", CAPTCHA_MAX_WAIT)
    ctx.close()
    headed_browser.close()


def scrape_url(browser, url: str, csv_writer: LiveCSV):
    url_type = classify_url(url)
    if url_type == "unknown":
        log.warning("Unknown URL type, trying generic scrape: %s", url)

    context = _make_context(browser)
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
    args = parser.parse_args()

    urls = read_urls(args.urls_file)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    output_path = args.output or f"aliexpress_scrape_{timestamp}.csv"

    csv_writer = LiveCSV(output_path)
    log.info("Writing results live to: %s", output_path)

    # Use real Chrome if available — avoids CAPTCHAs entirely
    chrome_path = find_chrome()

    with sync_playwright() as pw:
        launch_args = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        }
        if chrome_path:
            log.info("Using real browser: %s", chrome_path)
            launch_args["executable_path"] = chrome_path
        else:
            log.warning("Real browser not found — using Playwright Chromium")

        browser = pw.chromium.launch(**launch_args)

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
