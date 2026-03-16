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
import math
import os
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

CLICK_NEXT_JS = """
(targetPage) => {
    // Helper: scroll element into view and click it
    function clickEl(el) {
        el.scrollIntoView({block: 'center'});
        el.click();
        return true;
    }

    // Strategy 1: Find a "next" button/link in any pagination-like container
    const paginationEls = document.querySelectorAll(
        '[class*="pagination"], [class*="Pagination"], nav[aria-label*="page"], [class*="comet-pagination"]'
    );
    for (const pg of paginationEls) {
        // Look for next button
        const nextBtns = pg.querySelectorAll(
            '[class*="next"]:not([class*="disabled"]):not([disabled]), ' +
            'button[aria-label="Next"], a[rel="next"], ' +
            '[aria-label*="Next"], button[aria-label="Next page"]'
        );
        for (const btn of nextBtns) {
            if (btn.offsetParent !== null) return clickEl(btn);
        }
        // Look for specific page number
        const allBtns = pg.querySelectorAll('a, button, span[role="button"], li');
        for (const btn of allBtns) {
            const txt = (btn.innerText || '').trim();
            if (txt === String(targetPage) && btn.offsetParent !== null) {
                return clickEl(btn);
            }
        }
    }

    // Strategy 2: Look anywhere on page for next-page controls
    const globalNextSels = [
        '.comet-pagination-next:not(.comet-pagination-disabled)',
        'button[class*="next"]:not([disabled])',
        'a[class*="next"]',
        '[aria-label*="Next"]',
        'a[rel="next"]',
    ];
    for (const sel of globalNextSels) {
        const els = document.querySelectorAll(sel);
        for (const el of els) {
            if (el.offsetParent !== null) return clickEl(el);
        }
    }

    // Strategy 3: Find any element with the target page number near bottom of page
    const allEls = document.querySelectorAll('a, button, span[role="button"]');
    const pageH = document.body.scrollHeight;
    for (const el of allEls) {
        const txt = (el.innerText || '').trim();
        const rect = el.getBoundingClientRect();
        // Must be near bottom half of viewport, text matches page number, and is visible
        if (txt === String(targetPage) && el.offsetParent !== null && rect.top > 200) {
            // Verify it looks like pagination (small element, not a product card)
            if (rect.width < 200 && rect.height < 100) {
                return clickEl(el);
            }
        }
    }

    return false;
}
"""


def click_next(tab, current):
    """Try to click the next page button using in-browser JS. Returns True if clicked."""
    nxt = current + 1
    try:
        # Scroll to bottom first so pagination elements are rendered
        tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        tab.wait_for_timeout(500)
        result = tab.evaluate(CLICK_NEXT_JS, nxt)
        if result:
            tab.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False

# ---------------------------------------------------------------------------
# Wait for page to be ready, handle login/captcha
# ---------------------------------------------------------------------------

def is_captcha(tab):
    """Check if the current page is showing a CAPTCHA (including AliExpress slider)."""
    try:
        url = tab.url.lower()
        if "captcha" in url or "punch" in url or "sec.aliexpress" in url:
            return True
        # Check for common CAPTCHA / slider elements
        for sel in [
            "#captcha", "[class*='captcha']", "[class*='Captcha']",
            "#nc_1_n1z", ".nc-container", "#baxia-dialog",
            # AliExpress slider CAPTCHA
            "#nc_1_wrapper", "#nc_1__scale_text", ".nc_wrapper",
            ".nc-outer", "#nocaptcha", "[class*='nocaptcha']",
            ".J_MIDDLEWARE_FRAME_WIDGET", "#J_suf498498498",
            "iframe[src*='captcha']", "iframe[src*='punch']",
            "iframe[src*='nocaptcha']", "iframe[src*='sec.aliexpress']",
            # Generic slider
            "[class*='slider-verify']", "[class*='SliderCaptcha']",
            "[class*='slide-verify']", "[class*='smartCaptcha']",
            "[id*='alicaptcha']", "[class*='alicaptcha']",
        ]:
            try:
                el = tab.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
        # Also check iframes — slider CAPTCHA often loads in an iframe
        for frame in tab.frames:
            try:
                frame_url = frame.url.lower()
                if any(w in frame_url for w in ["captcha", "punch", "nocaptcha", "sec.aliexpress"]):
                    return True
            except Exception:
                pass
        # Check page content for CAPTCHA indicators (short pages only)
        body = tab.query_selector("body")
        if body:
            text = (body.inner_text() or "").strip()
            if len(text) < 500:
                low = text.lower()
                if any(w in low for w in ["captcha", "verify you are human", "robot",
                                          "slide to verify", "puzzle", "drag the slider"]):
                    return True
    except Exception:
        pass
    return False


def is_login(tab):
    """Check if page is showing a login/sign-in page or overlay."""
    try:
        url = tab.url.lower()
        if "login" in url or "passport" in url or "signin" in url:
            return True
        # Check for login overlay/modal on page
        for sel in [
            "input[type='password']",
            "[class*='login-dialog']", "[class*='LoginDialog']",
            "[class*='sign-in']", "[class*='SignIn']",
            "form[action*='login']", "form[action*='signin']",
        ]:
            try:
                el = tab.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
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

    # Loop to handle CAPTCHA → login → CAPTCHA chains
    for _ in range(5):
        # Check for login
        if is_login(tab):
            log.warning(">>> Sign in required! Log in in the browser window. <<<")
            print("\a", flush=True)
            while is_login(tab):
                tab.wait_for_timeout(1000)
            log.info(">>> Login complete! Reloading target... <<<")
            try:
                tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                tab.wait_for_selector("a[href*='/item/']", timeout=5000)
            except Exception:
                pass
            continue  # Re-check in case CAPTCHA appears after login

        # Check for CAPTCHA
        if is_captcha(tab):
            log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
            print("\a", flush=True)
            while is_captcha(tab):
                tab.wait_for_timeout(2000)
            log.info(">>> CAPTCHA solved! Reloading target... <<<")
            try:
                tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                tab.wait_for_selector("a[href*='/item/']", timeout=5000)
            except Exception:
                pass
            continue  # Re-check in case login appears after CAPTCHA

        # Neither login nor CAPTCHA — good to go
        break

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
        browser = None
        context = None
        tab = None

        def ensure_browser():
            nonlocal browser, context, tab
            try:
                # Test if page is still alive
                if tab:
                    tab.url
                    return
            except Exception:
                pass
            # (Re)create browser
            log.info("Opening browser...")
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
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

            # Make sure browser is alive
            ensure_browser()

            # Load first page
            try:
                tab.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                err = str(e).lower()
                if "closed" in err or "crashed" in err:
                    log.warning("  Browser closed — reopening...")
                    tab = None
                    ensure_browser()
                    try:
                        tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as e2:
                        log.warning("  Load error: %s", e2)
                        continue
                else:
                    # Check for CAPTCHA
                    try:
                        if is_captcha(tab):
                            log.warning(">>> CAPTCHA detected! Solve it in the browser window. <<<")
                            print("\a", flush=True)
                            while is_captcha(tab):
                                tab.wait_for_timeout(2000)
                            log.info(">>> CAPTCHA solved! Reloading... <<<")
                            try:
                                tab.goto(url, wait_until="domcontentloaded", timeout=30000)
                            except Exception:
                                log.warning("  Load error after CAPTCHA: %s", e)
                                continue
                        else:
                            log.warning("  Load error: %s", e)
                            continue
                    except Exception:
                        log.warning("  Load error: %s", e)
                        continue

            pg = 1
            while pg <= 100:
                log.info("  Page %d", pg)

                # Wait for content, handle login/captcha
                wait_ready(tab, url)

                # Dismiss popups
                dismiss_popups(tab)

                # Scroll and extract all products
                products = scroll_and_extract(tab)

                if not products and pg > 1:
                    log.info("  No products on page %d — done.", pg)
                    break

                prev_total = csv_out.count
                csv_out.add(products, url)
                new_count = csv_out.count - prev_total
                log.info("  Page %d: %d products, %d new (total: %d)", pg, len(products), new_count, csv_out.count)

                # If page had zero new products, pagination is looping — stop
                if new_count == 0 and pg > 1:
                    log.info("  No new products — done with this URL.")
                    break

                # Try clicking next page button
                if not click_next(tab, pg):
                    log.info("  No next page — done.")
                    break

                # Wait for new page to load
                try:
                    tab.wait_for_selector("a[href*='/item/']", timeout=5000)
                except Exception:
                    pass

                pg += 1
                time.sleep(random.uniform(0.2, 0.5))

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    csv_out.close()
    log.info("Done! %d products → %s", csv_out.count, out)

    # -----------------------------------------------------------------------
    # POST-PROCESSING PIPELINE
    # -----------------------------------------------------------------------
    post_process(out)


# ---------------------------------------------------------------------------
# Post-processing: dedup titles → filter → split SAFE links
# ---------------------------------------------------------------------------

def post_process(csv_path):
    """Remove duplicate titles, run eBay safety filter, split SAFE into 1500-line link CSVs."""
    log.info("=" * 60)
    log.info("POST-PROCESSING PIPELINE")
    log.info("=" * 60)

    # --- Step 1: Remove duplicate titles ---
    log.info("Step 1: Removing duplicate titles...")
    base = os.path.splitext(csv_path)[0]
    deduped_path = f"{base}_deduped.csv"

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    original_count = len(rows)
    seen_titles = set()
    unique_rows = []
    for row in rows:
        title = row.get("product_title", "").strip().lower()
        if title and title not in seen_titles:
            seen_titles.add(title)
            unique_rows.append(row)
        elif not title:
            unique_rows.append(row)

    with open(deduped_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(unique_rows)

    dupes_removed = original_count - len(unique_rows)
    log.info("  %d products → %d unique (%d duplicates removed)",
             original_count, len(unique_rows), dupes_removed)
    log.info("  Saved: %s", deduped_path)

    # --- Step 2: Run eBay safety filter ---
    log.info("Step 2: Running eBay UK safety filter...")
    try:
        # Import the filter from same directory
        filter_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, filter_dir)
        import ebay_filter

        stats, report = ebay_filter.process_csv(deduped_path)
        t = max(stats["total"], 1)
        log.info("  Total: %d", stats["total"])
        log.info("  SAFE: %d (%.1f%%)", stats["safe"], stats["safe"] / t * 100)
        log.info("  GREYLIST: %d (%.1f%%)", stats.get("greylist", 0), stats.get("greylist", 0) / t * 100)
        log.info("  BLOCKED: %d (%.1f%%)", stats["blocked"], stats["blocked"] / t * 100)
    except ImportError:
        log.error("  ebay_filter.py not found in %s — skipping filter step.", filter_dir)
        return
    except Exception as e:
        log.error("  Filter error: %s", e)
        return

    # --- Step 3: Split SAFE output into 1500-line link-only CSVs ---
    log.info("Step 3: Splitting SAFE links into 1500-line CSV files...")
    safe_csv = f"{base}_deduped_SAFE.csv"

    if not os.path.exists(safe_csv):
        log.warning("  SAFE CSV not found: %s", safe_csv)
        return

    # Read SAFE CSV and extract product_url column
    with open(safe_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        sf = list(reader.fieldnames) if reader.fieldnames else []
        url_col = None
        for col in sf:
            if "url" in col.lower() and "image" not in col.lower() and "store" not in col.lower() and "source" not in col.lower():
                url_col = col
                break
        if not url_col and "product_url" in sf:
            url_col = "product_url"
        if not url_col:
            url_col = sf[0] if sf else None

        links = []
        f.seek(0)
        reader = csv.DictReader(f)
        for row in reader:
            link = row.get(url_col, "").strip()
            if link:
                links.append(link)

    if not links:
        log.info("  No SAFE links to split.")
        return

    chunk_size = 1500
    num_chunks = math.ceil(len(links) / chunk_size)
    output_dir = os.path.dirname(csv_path) or "."

    for i in range(num_chunks):
        chunk = links[i * chunk_size : (i + 1) * chunk_size]
        chunk_path = os.path.join(output_dir, f"{base}_SAFE_links_part{i + 1}.csv")
        with open(chunk_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["product_url"])
            for link in chunk:
                w.writerow([link])
        log.info("  Part %d: %d links → %s", i + 1, len(chunk), chunk_path)

    log.info("Split %d SAFE links into %d files of up to %d each.", len(links), num_chunks, chunk_size)
    log.info("=" * 60)
    log.info("POST-PROCESSING COMPLETE")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
