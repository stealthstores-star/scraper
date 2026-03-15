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

class LiveCSV:
    FIELDS = ["product_title", "price", "product_url", "source_url"]

    def __init__(self, path):
        self.path = path
        self.seen = set()
        self.count = 0
        self._f = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.DictWriter(self._f, fieldnames=self.FIELDS)
        self._w.writeheader()
        self._f.flush()

    def add(self, rows, source_url):
        for r in rows:
            if r["product_url"] not in self.seen:
                self.seen.add(r["product_url"])
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
# Extract products from page
# ---------------------------------------------------------------------------

def extract(page):
    products = []
    seen = set()

    # Try JSON first (embedded in page source)
    try:
        html = page.content()
        for pat in [
            r'"items"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"itemList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"productList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
        ]:
            for m in re.finditer(pat, html):
                try:
                    for item in json.loads(m.group(1)):
                        p = _from_json(item)
                        if p and p["product_url"] not in seen:
                            seen.add(p["product_url"])
                            products.append(p)
                except Exception:
                    pass
    except Exception:
        pass

    if products:
        return products

    # Fallback: DOM links
    try:
        links = page.query_selector_all("a[href*='/item/']")
        for el in links:
            try:
                p = _from_link(el)
                if p and p["product_url"] not in seen:
                    seen.add(p["product_url"])
                    products.append(p)
            except Exception:
                pass
    except Exception:
        pass

    return products


def _from_json(item):
    title = item.get("title") or item.get("productTitle") or item.get("name") or ""
    if not title:
        return None
    price = item.get("price") or item.get("salePrice") or item.get("minPrice") or item.get("formattedPrice") or "N/A"
    if isinstance(price, dict):
        price = price.get("formattedPrice") or price.get("minPrice") or "N/A"
    pid = str(item.get("productId") or item.get("itemId") or item.get("id") or item.get("productDetailUrl") or "")
    if pid.startswith("http"):
        url = _clean_url(pid)
    elif pid.isdigit():
        url = f"https://www.aliexpress.com/item/{pid}.html"
    else:
        return None
    return {"product_title": title.strip(), "price": str(price), "product_url": url}


def _from_link(el):
    href = el.get_attribute("href") or ""
    if "/item/" not in href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.aliexpress.com" + href
    url = _clean_url(href)
    title = None
    for sel in ["h1", "h3", "h2", "[class*='title']", "[class*='Title']"]:
        te = el.query_selector(sel)
        if te:
            try:
                title = te.inner_text().strip()
            except Exception:
                pass
            if title:
                break
    if not title:
        img = el.query_selector("img")
        if img:
            title = (img.get_attribute("alt") or "").strip()
    if not title:
        try:
            title = el.inner_text().strip()[:200]
        except Exception:
            pass
    if not title:
        return None
    price = None
    for sel in ["[class*='price']", "[class*='Price']"]:
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
    return {"product_title": title, "price": price or "N/A", "product_url": url}


def _clean_url(url):
    m = re.search(r"(/item/\d+\.html)", urlparse(url).path)
    if m:
        return f"https://www.aliexpress.com{m.group(1)}"
    return url.split("?")[0]

# ---------------------------------------------------------------------------
# Scroll page to load lazy content
# ---------------------------------------------------------------------------

def scroll(page):
    for _ in range(6):
        page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
        page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)

# ---------------------------------------------------------------------------
# Check for next page
# ---------------------------------------------------------------------------

def has_next(page, current):
    for sel in [
        f"a[href*='page={current + 1}']",
        "a[class*='next']",
        "button[aria-label='Next']",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
    ]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    return False

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("urls_file")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()

    # Read URLs
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
        # Use Playwright's own Chromium, headed, with stealth flag
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        # Hide webdriver flag
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

                try:
                    tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    log.warning("  Load error: %s", e)
                    break

                # Wait for page content
                tab.wait_for_timeout(3000)

                # Dismiss popups
                for sel in ["button:has-text('Accept')", "button:has-text('OK')",
                            "button:has-text('Got it')", ".comet-modal-close"]:
                    try:
                        btn = tab.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            tab.wait_for_timeout(300)
                    except Exception:
                        pass

                # Scroll to load lazy content
                scroll(tab)

                # Extract
                products = extract(tab)

                if not products and pg > 1:
                    log.info("  No products on page %d — done.", pg)
                    break

                csv_out.add(products, url)
                log.info("  Page %d: %d products (total: %d)", pg, len(products), csv_out.count)

                if not has_next(tab, pg):
                    log.info("  No next page — done.")
                    break

                pg += 1
                time.sleep(random.uniform(1, 3))

        context.close()
        browser.close()

    csv_out.close()
    log.info("Done! %d products → %s", csv_out.count, out)


if __name__ == "__main__":
    main()
