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
OUTPUT_FILE = Path(__file__).parent / "products.json"

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

def get_product_links() -> list[str]:
    links = set()
    page = 1
    while True:
        url = LISTING_URL if page == 1 else f"{LISTING_URL}?page={page}"
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            soup = BeautifulSoup(res.text, "html.parser")
            found = 0
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/tham-" in href and "?" not in href:
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
    print("[SCRAPER] Fetching product links...")
    links = get_product_links()
    print(f"[SCRAPER] Found {len(links)} products")

    products = []
    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] {url.split('/')[-1]}")
        p = scrape_product(url)
        if p:
            products.append(p)
        time.sleep(0.3)

    OUTPUT_FILE.write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[SCRAPER] Done — {len(products)} products saved to {OUTPUT_FILE}")
    return products


if __name__ == "__main__":
    run()
