import json
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from fnmatch import fnmatch

import requests
import requests_cache
from bs4 import BeautifulSoup

BASE_URL = "https://www.gobilda.com"

session = requests_cache.CachedSession(
    "gobilda_cache",
    expire_after=None,  # cache forever
)
seen_products = set()

progress_lock = threading.Lock()
products_completed = 0
products_total = 0

commonly_used = [
    "1120.field2<=12",  # U channel up to 12 holes
    "1121.field2<=12",  # U channel low-side up to 12 holes
    "1143.field2<=12",  # U channel mini up to 12 holes
    "5203-2402*",  # Yellow Jacket planetary gearbox motors (most common ratios and sizes, 24mm REX shaft)
    "2800-0004*",  # M4 Socket head screws
    "2802-0004*",  # M4 Button head screws
    "2804-0004*",  # M4 Low profile screws,
    "2000-0025-0002",  # Torque servo
    "2000-0025-0003",  # Speed servo
    "2000-0025-0004",  # Super speed servo
    "1522-0010*",  # 8mm ID x 10mm OD Spacers
    "1516-4008-*",  # 8mm REX Standoffs
    "2812-0004-0007",  # M4 Nylock nut
    "2811-0004-0007",  # M4 Hex nut
    "1611-0514-4008",  # REX Flanged Bearing
    "1201-0043-0002",  # Quad block mount
    "1205-0001-0005",  # Dual block mount
    "3625-0202-0104",  # Mecanum wheels
    "1309-0016-4008", # Sonic hub
    "2106-4008*",    # REX Shafting stainless steel
    "3217-0001-2501", # Compact ServoBlock
    "1802-0043-0001", #1802 Series Servo Frame
    "1908-0025-0032", # 1908 Series Servo Hub
]

MATERIAL_TABLE = {
    # Plastics
    "ABS Plastic": "ABS",
    "Acetal Plastic": "Acetal",
    "Plastic": "ABS",  # Generic fallback
    "Polycarbonate Plastic": "Polycarbonate",

    # Metals
    "Aluminum": "6061 Alloy",
    "Brass": "Brass",
    "Bronze": "Bronze",
    "Steel": "Plain Carbon Steel",
    "Stainless Steel": "AISI 304",

    # Finishes (ignore coating, use base material)
    "Clear Anodized": "6061 Alloy",
    "Zinc-Plated": "Plain Carbon Steel",

    # Composites (closest available)
    "Fiber-Reinforced Plastic": "Glass Fiber Reinforced Plastic",
    "Fiber-Reinforced Acetal": "Acetal",

    # Assemblies (choose dominant body material)
    "Plastic with Brass Inserts": "ABS",
    "Plastic with Brass Threaded Inserts": "ABS",
    "Plastic with High-Carbon Steel Bearings": "ABS",
    "Plastic with Stainless Steel Bearings": "ABS",
    "Fiber-Reinforced Plastic Hinge Blocks, Zinc-Plated Steel Hardware": "Glass Fiber Reinforced Plastic",
    "Polycarbonate U-Wheel with High-Carbon Steel Bearings": "Polycarbonate",
}

FINISH_TABLE = {
    "Black Oxide":      r"plastic\High Gloss\black high gloss plastic.p2m",
    "Black Zinc-Plated":r"plastic\High Gloss\black high gloss plastic.p2m",
    "Clear Anodized":   r"metal\aluminum\satin finish aluminum.p2m",
    "Steel":            r"metal\steel\polished steel.p2m",
    "Titanium Nitride": r"metal\zinc\satin finish zinc.p2m",
    "Zinc Plated":      r"metal\zinc\satin finish zinc.p2m",
    "Zinc-Plated":      r"metal\zinc\satin finish zinc.p2m",
}

def is_commonly_used(sku):
    if sku is None:
        return False

    for pattern in commonly_used:
        if "*" in pattern:
            if fnmatch(sku, pattern):
                return True

        elif ".field2<=" in pattern:
            prefix, limit = pattern.split(".field2<=")
            limit = int(limit)

            parts = sku.split("-")
            if len(parts) >= 2 and parts[0] == prefix:
                if int(parts[1]) <= limit:
                    return True

    return False


def scrape_page(url):
    print(f"Scraping {url}")

    response = session.get(url, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.content, "lxml")

    heading = soup.select_one(".page-heading")
    if heading is None:
        return None

    title = heading.get_text(strip=True)

    products = []
    child_links = []

    product_tables = soup.select("table.productTable")

    for product_table in product_tables:
        for row in product_table.select("tr.productTable-row"):
            # First link in the row
            link_el = row.select_one("a[href]")
            if not link_el:
                continue

            link = link_el["href"]
            if not link.startswith("http"):
                link = BASE_URL + link

            # Prefer data-sku, then link text
            sku = (
                    link_el.get("data-sku")
                    or link_el.get_text(strip=True)
            )

            key = sku or link

            if str(key) in seen_products:
                continue

            seen_products.add(str(key))

            products.append({
                "title": link_el.get("title") or sku,
                "product_title": None,
                "link": link,
                "parent": url,
                "parent_title": title,
                "sku": sku,
                "image_url": None,
                "step_file": None,
                "skip": False,
                "commonly_used": is_commonly_used(sku),
                "weight": None,
                "material": None,
                "finish": None,
            })
    else:
        for product in soup.select(".product"):
            title_el = product.select_one(".card-title")
            sku_el = product.select_one("pre")
            link_el = product.find("a")
            img_el = product.select_one("img")

            if not link_el:
                continue

            link = link_el["href"]
            if not link.startswith("http"):
                link = BASE_URL + link

            image_url = None
            if img_el:
                image_url = img_el.get("src") or img_el.get("data-src") or img_el.get("data-lazy-src")
                if image_url and not image_url.startswith("http"):
                    image_url = BASE_URL + image_url

            title_text = title_el.get_text(strip=True) if title_el else None

            # Prefer sku from <pre> when present. Some product pages (e.g. the nylock nut)
            # don't include a SKU in the link; treat them as products if their name
            # matches the known product title "M4 Lock Nut".
            sku = sku_el.get_text(strip=True) if sku_el else None
            if sku is None and title_text == "M4 Lock Nut":
                # set sku to title_text so downstream logic treats it as a product
                sku = "2812-0004-0007"  # Nylock nut is weird :/

            key = sku if sku else link

            if str(key) in seen_products:
                continue
            seen_products.add(str(key))

            products.append({
                "title": title_text,
                "product_title": None,
                "link": link,
                "parent": url,
                "parent_title": title,
                "sku": sku,
                "image_url": image_url,
                "step_file": None,
                "skip": False,
                "commonly_used": is_commonly_used(sku),
                "weight": None,
                "material": None,
                "finish": None
            })

            # Only treat the link as a child/category when we don't consider it a product.
            if sku is None:
                child_links.append(link)

    return {"url": url, "title": title, "products": products, "child_links": child_links}


def fetch_product_details(product):
    global products_completed

    try:
        response = session.get(product["link"], timeout=30)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, "lxml")

        step_link = None
        for a in soup.select("a.product-downloadsList-listItem-link[href$='.zip']"):
            if "STEP" in a.get_text(" ", strip=True).upper():
                step_link = a
                break

        if step_link is None:
            product["skip"] = True
            return

        href = step_link.get("href")
        if href and not href.startswith("http"):
            href = BASE_URL + href
        product["step_file"] = href

        title = soup.select_one(".productView-title")
        if title:
            product["product_title"] = title.get_text(strip=True)

        image = soup.select_one(".imageGallery-medium-link.link-1")
        if image:
            image_url = image.get("href")
            if image_url and not image_url.startswith("http"):
                image_url = BASE_URL + image_url
            product["image_url"] = image_url

        specs = {}

        table = soup.select_one("table.product-specsTable")
        if table:
            for row in table.select("tr.product-specsTable-row"):
                key = row.select_one("th.product-specsTable-headerCell")
                value = row.select_one("td.product-specsTable-cell")

                if key and value:
                    specs[key.get_text(strip=True)] = value.get_text(strip=True)

        product["weight"] = specs.get("Weight")
        if not product["weight"] is None:
            product["weight"] = product["weight"].replace(" Each", "")
            product["weight"] = product["weight"].replace(" each", "")
            product["weight"] = product["weight"].replace(" each with included hardware", "")

        product["material"] = specs.get("Material")
        if not product["material"] is None:
            product["material"] = MATERIAL_TABLE[specs.get("Material")]

        product["finish"] = specs.get("Finish")
        if not product["finish"] is None:
            product["finish"] = FINISH_TABLE[specs.get("Finish")]
    except Exception as e:
        print(f"\nFailed: {product['link']} - {e}")

    finally:
        with progress_lock:
            products_completed += 1
            print(f"\rFetched {products_completed}/{products_total} product pages", end="", flush=True)


def crawl(root_url, max_workers=20):
    global products_completed, products_total
    visited = set()
    queue = deque([root_url])
    pages = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while queue:
            level = []
            while queue:
                u = queue.popleft()
                if u not in visited:
                    visited.add(u)
                    level.append(u)

            for page in executor.map(scrape_page, level):
                if page is None:
                    continue
                pages[page["url"]] = page
                for child in page["child_links"]:
                    if child not in visited:
                        queue.append(child)

    all_products = [p for page in pages.values() for p in page["products"] if p["sku"] is not None]
    products_total = len(all_products)
    products_completed = 0

    print(f"Fetching {products_total} product pages...")
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        list(executor.map(fetch_product_details, all_products))
    print()

    return pages


def build_tree(url, pages, visited=None):
    if visited is None:
        visited = set()
    if url in visited:
        return None
    visited.add(url)

    page = pages[url]
    node = {"url": page["url"], "title": page["title"], "children": []}

    for product in page["products"]:
        if product.get("skip"):
            continue

        if product["sku"] is None and product["link"] in pages:
            child = build_tree(product["link"], pages, visited)
            if child:
                node["children"].append(child)
        else:
            node["children"].append({
                "title": product["product_title"] or product["title"],
                "listing_title": product["title"],
                "sku": product["sku"],
                "url": product["link"],
                "image_url": product["image_url"],
                "step_file": product["step_file"],
                "commonly_used": product["commonly_used"],
                "weight": product["weight"],
                "material": product["material"],
                "finish": product["finish"],
            })

    return node


def prune_empty_categories(node):
    """
    Removes categories that don't contain any products.
    Returns None if the node should be removed.
    """

    # Product node
    if "sku" in node:
        return node if node.get("step_file") else None

    pruned_children = []
    for child in node.get("children", []):
        pruned = prune_empty_categories(child)
        if pruned is not None:
            pruned_children.append(pruned)

    node["children"] = pruned_children

    # Remove empty categories
    if not pruned_children:
        return None

    return node


roots = [
    ("structure", "/structure"),
    ("motion", "/motion"),
    ("electronics", "/electronics"),
    ("hardware", "/hardware"),
]

children = []
for name, path in roots:
    print(f"\n=== {name.title()} ===")
    pages = crawl(BASE_URL + path)

    tree = build_tree(BASE_URL + path, pages)
    tree = prune_empty_categories(tree)

    if tree is not None:
        children.append(tree)

combined_tree = {
    "url": BASE_URL + "/",
    "title": "goBILDA",
    "children": children,
}

with open("structure.json", "w", encoding="utf-8") as f:
    json.dump(combined_tree, f, indent=2, ensure_ascii=False)

print("Done!")
