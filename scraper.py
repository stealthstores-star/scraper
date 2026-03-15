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
    "id",
    "product_title",
    "product_price",
    "product_original_price",
    "product_discount",
    "product_url",
    "product_image",
    "product_rating",
    "store_name",
    "store_url",
    "store_id",
    "total_sales",
    "ship_from",
    "store_member_id",
    "trade_info",
    "shipping",
    "launch_time",
    "company_name",
    "source_url",
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
# Extract products from page
# ---------------------------------------------------------------------------

def _get(d, *keys):
    """Get first non-empty value from dict using multiple possible keys."""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return ""


def extract(page):
    products = []
    seen = set()

    # Use JavaScript to extract all product data fast — avoids slow DOM queries
    raw = page.evaluate("""
        () => {
            const results = [];
            // Find all links to /item/ pages
            const links = document.querySelectorAll('a[href*="/item/"]');
            const processed = new Set();

            for (const link of links) {
                const href = link.getAttribute('href') || '';
                const match = href.match(/\\/item\\/(\\d+)\\.html/);
                if (!match) continue;
                const pid = match[1];
                if (processed.has(pid)) continue;

                // Walk up to find the product card container
                let card = link;
                for (let i = 0; i < 5; i++) {
                    if (!card.parentElement) break;
                    card = card.parentElement;
                    // Stop at elements that look like product cards
                    const cls = card.className || '';
                    if (cls.includes('card') || cls.includes('Card') ||
                        cls.includes('item') || cls.includes('Item') ||
                        cls.includes('product') || cls.includes('Product')) break;
                }

                const text = card.innerText || '';

                processed.add(pid);

                // Get image
                const img = card.querySelector('img');
                let image = '';
                if (img) image = img.getAttribute('src') || img.getAttribute('data-src') || '';

                // Get title — first meaningful text or img alt
                let title = '';
                const titleEl = card.querySelector('h1,h2,h3,[class*="title"],[class*="Title"]');
                if (titleEl) title = titleEl.innerText.trim();
                if (!title && img) title = (img.getAttribute('alt') || '').trim();
                if (!title) {
                    // Get first line of text that's not a price
                    const lines = text.split('\\n').map(l => l.trim()).filter(l => l && !l.match(/^[\\$€£¥₽]/));
                    if (lines.length) title = lines[0];
                }

                // Get price
                let price = 'N/A';
                const priceMatch = text.match(/[\\$€£¥₽]\\s?[\\d,\\.]+/);
                if (priceMatch) price = priceMatch[0].trim();

                // Get sales info
                let sales = '';
                const salesMatch = text.match(/(\\d[\\d,\\.]*\\+?)\\s*sold/i);
                if (salesMatch) sales = salesMatch[0].trim();

                results.push({
                    id: pid,
                    title: title.substring(0, 300),
                    price: price,
                    image: image,
                    sales: sales,
                    href: href,
                });
            }
            return results;
        }
    """)

    for r in raw:
        pid = r["id"]
        href = r["href"]
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.aliexpress.com" + href
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
            "store_name": "",
            "store_url": "",
            "store_id": "",
            "total_sales": r.get("sales", ""),
            "ship_from": "",
            "store_member_id": "",
            "trade_info": r.get("sales", ""),
            "shipping": "",
            "launch_time": "",
            "company_name": "",
        })

    return products


def _merge_json(page, products, seen):
    """Try to merge extra fields from embedded JSON into existing products."""
    try:
        html = page.content()
        json_products = {}
        for pat in [
            r'"items"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"itemList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
            r'"productList"\s*:\s*(\[[\s\S]*?\])\s*[,}]',
        ]:
            for m in re.finditer(pat, html):
                try:
                    for item in json.loads(m.group(1)):
                        p = _from_json(item)
                        if p:
                            json_products[p["product_url"]] = p
                except Exception:
                    pass

        # Merge JSON fields into DOM products
        for product in products:
            jp = json_products.get(product["product_url"])
            if jp:
                for k, v in jp.items():
                    if v and v != "N/A" and (not product.get(k) or product[k] == "" or product[k] == "N/A"):
                        product[k] = v

        # Add any JSON-only products not found in DOM
        for url, jp in json_products.items():
            if url not in seen:
                seen.add(url)
                products.append(jp)
    except Exception:
        pass


def _from_json(item):
    """Extract all available fields from a JSON product object."""
    title = _get(item, "title", "productTitle", "name", "subject")
    if not title:
        return None

    # Product ID and URL
    pid = str(_get(item, "productId", "itemId", "id", "productDetailUrl") or "")
    if pid.startswith("http"):
        product_url = _clean_url(pid)
    elif pid.isdigit():
        product_url = f"https://www.aliexpress.com/item/{pid}.html"
    else:
        return None

    # Price fields
    sale_price = _get(item, "price", "salePrice", "minPrice", "formattedPrice")
    if isinstance(sale_price, dict):
        sale_price = _get(sale_price, "formattedPrice", "minPrice", "value")
    sale_price = str(sale_price) if sale_price else "N/A"

    orig_price = _get(item, "originalPrice", "oriMinPrice", "oriMaxPrice")
    if isinstance(orig_price, dict):
        orig_price = _get(orig_price, "formattedPrice", "minPrice", "value")
    orig_price = str(orig_price) if orig_price else ""

    discount = _get(item, "discount", "discountRate", "discountRatio", "salePercent")
    discount = str(discount) if discount else ""

    # Image
    image = _get(item, "image", "imageUrl", "imgUrl", "productImage", "pic")
    if isinstance(image, dict):
        image = _get(image, "imgUrl", "imageUrl", "url")
    image = str(image) if image else ""
    if image and image.startswith("//"):
        image = "https:" + image

    # Rating
    rating = _get(item, "averageStar", "averageStarRate", "starRating",
                  "evaluation", "evaluationScore", "rating")
    rating = str(rating) if rating else ""

    # Store info
    store = item.get("store") or {}
    if isinstance(store, dict):
        store_name = _get(store, "storeName", "name", "aliMemberId") or _get(item, "storeName", "shopName")
        store_id = str(_get(store, "storeId", "id", "shopId") or _get(item, "storeId", "shopId") or "")
        store_member_id = str(_get(store, "aliMemberId", "memberId") or _get(item, "sellerMemberId", "aliMemberId") or "")
        company_name = _get(store, "companyName", "company") or _get(item, "companyName") or ""
    else:
        store_name = _get(item, "storeName", "shopName") or ""
        store_id = str(_get(item, "storeId", "shopId") or "")
        store_member_id = str(_get(item, "sellerMemberId", "aliMemberId") or "")
        company_name = _get(item, "companyName") or ""

    store_url = ""
    if store_id:
        store_url = f"https://www.aliexpress.com/store/{store_id}"

    # Sales
    total_sales = _get(item, "totalSales", "sold", "totalOrders", "tradeCount",
                       "totalTradCount", "orderCount")
    total_sales = str(total_sales) if total_sales else ""

    # Trade info (e.g. "500+ sold")
    trade_info = _get(item, "tradeDesc", "trade", "tradeInfo", "salesInfo")
    if isinstance(trade_info, dict):
        trade_info = _get(trade_info, "tradeDesc", "text", "value")
    trade_info = str(trade_info) if trade_info else ""

    # Shipping
    shipping = _get(item, "shippingInfo", "shipping", "logisticsDesc", "freeShipping")
    if isinstance(shipping, dict):
        shipping = _get(shipping, "desc", "text", "value", "logisticsDesc")
    if isinstance(shipping, bool):
        shipping = "Free Shipping" if shipping else ""
    shipping = str(shipping) if shipping else ""

    # Ship from
    ship_from = _get(item, "shipFrom", "shipFromCountry", "originCountry")
    ship_from = str(ship_from) if ship_from else ""

    # Launch time
    launch_time = _get(item, "launchTime", "createTime", "gmtCreate")
    launch_time = str(launch_time) if launch_time else ""

    return {
        "id": pid if pid.isdigit() else "",
        "product_title": str(title).strip(),
        "product_price": sale_price,
        "product_original_price": str(orig_price),
        "product_discount": str(discount),
        "product_url": product_url,
        "product_image": str(image),
        "product_rating": str(rating),
        "store_name": str(store_name) if store_name else "",
        "store_url": store_url,
        "store_id": store_id,
        "total_sales": str(total_sales),
        "ship_from": str(ship_from),
        "store_member_id": store_member_id,
        "trade_info": str(trade_info),
        "shipping": str(shipping),
        "launch_time": str(launch_time),
        "company_name": str(company_name),
    }


def _from_link(el):
    """Fallback: extract what we can from DOM elements."""
    href = el.get_attribute("href") or ""
    if "/item/" not in href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    elif href.startswith("/"):
        href = "https://www.aliexpress.com" + href
    product_url = _clean_url(href)

    # Extract product ID from URL
    pid_match = re.search(r"/item/(\d+)\.html", product_url)
    pid = pid_match.group(1) if pid_match else ""

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

    # Image
    image = ""
    img_el = el.query_selector("img")
    if img_el:
        image = img_el.get_attribute("src") or ""
        if image.startswith("//"):
            image = "https:" + image

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

    return {
        "id": pid,
        "product_title": title,
        "product_price": price or "N/A",
        "product_original_price": "",
        "product_discount": "",
        "product_url": product_url,
        "product_image": image,
        "product_rating": "",
        "store_name": "",
        "store_url": "",
        "store_id": "",
        "total_sales": "",
        "ship_from": "",
        "store_member_id": "",
        "trade_info": "",
        "shipping": "",
        "launch_time": "",
        "company_name": "",
    }


def _clean_url(url):
    m = re.search(r"(/item/\d+\.html)", urlparse(url).path)
    if m:
        return f"https://www.aliexpress.com{m.group(1)}"
    return url.split("?")[0]

# ---------------------------------------------------------------------------
# Scroll page to load lazy content
# ---------------------------------------------------------------------------

def scroll(page):
    # Scroll to bottom in chunks, waiting for new content to load
    page.evaluate("""
        async () => {
            const delay = ms => new Promise(r => setTimeout(r, ms));
            let lastHeight = 0;
            for (let i = 0; i < 20; i++) {
                window.scrollTo(0, document.body.scrollHeight);
                await delay(300);
                const newHeight = document.body.scrollHeight;
                if (newHeight === lastHeight) break;
                lastHeight = newHeight;
            }
            window.scrollTo(0, 0);
        }
    """)
    page.wait_for_timeout(300)

# ---------------------------------------------------------------------------
# Check for next page
# ---------------------------------------------------------------------------

def has_next(page, current):
    next_page = current + 1
    selectors = [
        f"a[href*='page={next_page}']",
        f"a[href*='page%3D{next_page}']",
        "a[class*='next']",
        "button[aria-label='Next']",
        ".comet-pagination-next:not(.comet-pagination-disabled)",
        # Pagination number links — check if next page number exists
        f"button:has-text('{next_page}')",
        f"a:has-text('{next_page}')",
        "li.next a",
        "[class*='pagination'] [class*='next']",
    ]
    for sel in selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                if el.is_visible():
                    # For text-based selectors, verify it's actually the page number
                    if sel.startswith("a:has-text") or sel.startswith("button:has-text"):
                        txt = el.inner_text().strip()
                        if txt == str(next_page):
                            return True
                    else:
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

        # Block heavy assets — but keep stylesheets (needed for layout/pagination)
        def block_heavy(route):
            rt = route.request.resource_type
            if rt in ("image", "font", "media"):
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

                try:
                    tab.goto(target, wait_until="commit", timeout=15000)
                except Exception as e:
                    log.warning("  Load error: %s", e)
                    break

                # Wait for product content to appear (fast) instead of networkidle (slow)
                try:
                    tab.wait_for_selector(
                        "a[href*='/item/'], script:has-text('items'), script:has-text('productList')",
                        timeout=6000,
                    )
                except Exception:
                    pass

                # Check if redirected to login
                current = tab.url.lower()
                if "login" in current or "passport" in current or "member" in current:
                    log.warning(">>> Redirected to login! Log in in the browser window. <<<")
                    print("\a", flush=True)
                    while True:
                        tab.wait_for_timeout(2000)
                        current = tab.url.lower()
                        if "login" not in current and "passport" not in current and "member" not in current:
                            log.info(">>> Login complete! Continuing... <<<")
                            tab.wait_for_timeout(2000)
                            break
                    try:
                        tab.goto(target, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    tab.wait_for_timeout(3000)

                # Dismiss popups
                for sel in ["button:has-text('Accept')", "button:has-text('OK')",
                            "button:has-text('Got it')", ".comet-modal-close"]:
                    try:
                        btn = tab.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            tab.wait_for_timeout(100)
                    except Exception:
                        pass

                # Extract + scroll loop — scroll until no new products appear
                products = extract(tab)
                prev_count = len(products)
                stale = 0
                while stale < 3:
                    try:
                        tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                        tab.wait_for_timeout(800)
                    except Exception:
                        break
                    products = extract(tab)
                    if len(products) > prev_count:
                        prev_count = len(products)
                        stale = 0
                    else:
                        stale += 1

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
