import json
from playwright.sync_api import sync_playwright

with open('products.json', 'r') as f:
    data = json.load(f)

print(f"Scraping images for {len(data)} rugs...")

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page()

    for product in data:
        try:
            page.goto(product['url'], wait_until='networkidle', timeout=20000)
            page.wait_for_timeout(2000)
            page.evaluate("window.scrollTo(0, 400)")
            page.wait_for_timeout(1500)

            img = page.evaluate("""() => {
                const imgs = document.querySelectorAll('img');
                for (const img of imgs) {
                    const src = img.src || '';
                    const w = img.naturalWidth || 0;
                    if (src && src.includes('bizweb.dktcdn.net') && src.includes('/products/')
                        && (src.includes('1024x1024') || w > 300)
                        && !src.includes('.svg')) return src;
                }
                return '';
            }""")

            if img:
                product['img'] = img
                print(f"[OK] {product['name'][:50]}: {img[:60]}")
            else:
                print(f"[MISS] {product['name'][:50]}")
        except Exception as e:
            print(f"[ERR] {product['url']}: {e}")

    browser.close()

with open('products.json', 'w') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("Done. products.json updated.")
