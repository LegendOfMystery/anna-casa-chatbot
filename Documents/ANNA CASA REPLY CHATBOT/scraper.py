"""
Anna Casa product scraper — chạy định kỳ để cập nhật products.json
Usage: python scraper.py
"""

import re
import json
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup

BASE_URL = "https://annacasavn.com"
LISTING_URL = f"{BASE_URL}/tham"
WP_LISTING_URL = f"{BASE_URL}/giay-dan-tuong?q=collections:4004284&page=1&view=grid"
OUTPUT_FILE = Path(__file__).parent / "products.json"
WP_OUTPUT_FILE = Path(__file__).parent / "wallpaper_products.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SIZE_PATTERN = re.compile(
    r'(\d+[\.,]?\d*)\s*[xX×]\s*(\d+[\.,]?\d*)\s*(mm|cm|m)?',
    re.IGNORECASE
)

COLOR_KEYWORDS = [
    "trắng", "đen", "xám", "be", "kem", "nâu", "xanh", "vàng",
    "hồng", "đỏ", "cam", "tím", "bạc", "vàng gold", "ghi", "nude",
    "ivory", "sand", "taupe", "charcoal", "navy", "terracotta",
]

def get_product_links(listing_url: str, path_prefix: str) -> list[str]:
    links = set()
    page = 1
    while True:
        url = listing_url if page == 1 else f"{listing_url.split('?')[0]}?page={page}"
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(res.text, "html.parser")
            found = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if path_prefix in href and "?" not in href:
                    full = href if href.startswith("http") else BASE_URL + href
                    if full not in links:
                        links.add(full)
                        found += 1
            if found == 0:
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            print(f"[LISTING] Page {page} error: {e}")
            break
    return list(links)


def parse_size(text: str) -> str:
    m = SIZE_PATTERN.search(text)
    if not m:
        return ""
    w, h, unit = m.group(1), m.group(2), (m.group(3) or "mm").lower()
    # normalize to meters
    factor = {"mm": 1000, "cm": 100, "m": 1}.get(unit, 1000)
    try:
        wm = float(w.replace(",", ".")) / factor
        hm = float(h.replace(",", ".")) / factor
        return f"{wm:.1f}m x {hm:.1f}m".replace(".0m", "m")
    except:
        return f"{w}x{h}{unit}"


def extract_colors(text: str) -> list[str]:
    text_lower = text.lower()
    return [c for c in COLOR_KEYWORDS if c in text_lower]


def scrape_product(url: str) -> dict | None:
    try:
        res = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(res.text, "html.parser")

        name = ""
        for sel in ["h1", ".product-name", ".product-title"]:
            el = soup.select_one(sel)
            if el:
                name = el.get_text(strip=True)
                break

        price = ""
        for sel in [".price", ".product-price", "[class*='price']"]:
            el = soup.select_one(sel)
            if el:
                price = el.get_text(strip=True).split("\n")[0]
                break

        img = ""
        for sel in [".product-image img", ".main-image img", ".product__images img", "img.product-img"]:
            el = soup.select_one(sel)
            if el:
                img = el.get("data-src") or el.get("src") or ""
                if img and not img.startswith("http"):
                    img = BASE_URL + img
                break

        meta = soup.find("meta", attrs={"name": "description"})
        description = meta["content"].strip() if meta and meta.get("content") else ""

        if not description:
            desc_el = soup.select_one(".product-description, [class*='description']")
            if desc_el:
                description = desc_el.get_text(" ", strip=True)[:400]

        size = parse_size(name) or parse_size(description)
        colors = extract_colors(description) or extract_colors(name)

        if not name:
            return None

        return {
            "name": name,
            "price": price,
            "size": size,
            "colors": colors,
            "description": description[:300],
            "url": url,
            "img": img,
        }

    except Exception as e:
        print(f"[PRODUCT] Error {url}: {e}")
        return None


def run():
    # Scrape thảm
    print("[SCRAPER] Fetching rug links...")
    rug_links = get_product_links(LISTING_URL, "/tham-")
    print(f"[SCRAPER] Found {len(rug_links)} rugs")
    rugs = []
    for i, url in enumerate(rug_links, 1):
        print(f"[RUG {i}/{len(rug_links)}] {url.split('/')[-1]}")
        p = scrape_product(url)
        if p:
            rugs.append(p)
        time.sleep(0.3)
    OUTPUT_FILE.write_text(json.dumps(rugs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SCRAPER] Rugs done — {len(rugs)} saved")

    # Scrape giấy dán tường
    print("[SCRAPER] Fetching wallpaper links...")
    wp_links = get_product_links(WP_LISTING_URL, "/giay-dan-tuong-")
    print(f"[SCRAPER] Found {len(wp_links)} wallpapers")
    wallpapers = []
    for i, url in enumerate(wp_links, 1):
        print(f"[WP {i}/{len(wp_links)}] {url.split('/')[-1]}")
        p = scrape_product(url)
        if p:
            p["category"] = "giay_dan_tuong"
            wallpapers.append(p)
        time.sleep(0.3)
    WP_OUTPUT_FILE.write_text(json.dumps(wallpapers, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SCRAPER] Wallpapers done — {len(wallpapers)} saved")


if __name__ == "__main__":
    run()
