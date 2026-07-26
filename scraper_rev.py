import json
import re
from urllib.parse import urljoin

import requests_cache
from bs4 import BeautifulSoup

BASE_URL = "https://www.revrobotics.com"

session = requests_cache.CachedSession(
    "gobilda_cache",
    expire_after=None,  # cache forever
)

SKIP_TITLES = {
    "New Products",
    "Merch",
    "Gift Certificates",
    "Stock Updates",
    "Kits & Bundles",
    "REV ION FRC Starter Bot",
    "REV DUO FTC Starter Bot",
    "FRC Everybot",
    "Competition",
    "Tech Resources",
    "Education",
    "Shop All",
    "Support",
}

BANNED_TITLE_KEYWORDS = {
    "Bundle",
    "Kit",
    "Pack",
    "Curriculum", # random courses offered by rev
    "Repair Service",
}


def parse_menu_item(li):
    """Parse a <li> into a tree node."""

    # Find the actual navigation link (ignore toggle buttons)
    link = li.select_one(
        ":scope > a.navPages-action,"
        ":scope > a.navPage-subMenu-action,"
        ":scope > a.navPage-childList-action"
    )

    if link is None:
        return None

    title = link.get_text(strip=True)

    for banned_title in SKIP_TITLES:
        if banned_title in title:
            return None

    node = {
        "title": title,
        "url": urljoin(BASE_URL, link["href"]),
        "children": [],
    }

    # Look for an immediate child UL
    child_ul = li.select_one(
        ":scope > div > .navPage-subMenu-middle > ul,"
        ":scope > ul.navPage-childList"
    )

    if child_ul:
        for child_li in child_ul.find_all("li", recursive=False):
            child = parse_menu_item(child_li)
            if child:
                node["children"].append(child)

    return node


def get_pagetree():
    page = session.get(BASE_URL)
    soup = BeautifulSoup(page.content, "html.parser")

    nav = soup.find("nav", class_="navPages")
    root_ul = nav.find("ul", class_="navPages-list")

    tree = []
    for li in root_ul.find_all("li", recursive=False):
        node = parse_menu_item(li)
        if node:
            tree.append(node)

    return tree


def scrape_product_list(url):
    print(f"Scraping {url}")

    page = session.get(url)
    soup = BeautifulSoup(page.content, "html.parser")

    product_cards = soup.select("article.card")

    output = {
        "title": soup.select_one(".page-heading").get_text(strip=True) if soup.select_one(
            ".page-heading") else "Untitled",
        "url": url,
        "children": [],
    }

    for card in product_cards:
        product_link = card.select_one("a[href]")
        img = card.select_one("img")

        product_url = urljoin(page.url, product_link["href"])
        image_url = urljoin(page.url, img["src"]) if img else None

        title = card.select_one(".card-title a").get_text(strip=True)

        title_lower = title.casefold()
        if any(keyword.casefold() in title_lower for keyword in BANNED_TITLE_KEYWORDS):
            continue

        button = card.select_one(".card-buttons a")
        button_label = button.get_text(strip=True) if button else None

        subproducts = scrape_product_page(product_url)
        if len(subproducts) == 1:
            if subproducts[0]["sku"] is None:
                continue  # Skip if no SKU
            # No subproducts, just this
            output["children"].append({
                "title": title,
                "listing_title": title,
                "sku": subproducts[0]["sku"],
                "url": product_url,
                "image_url": image_url,
                "step_file": subproducts[0]["step_file_url"],
                "commonly_used": False
            })
            continue
        elif len(subproducts) >= 2:
            children = []
            for subproduct in subproducts:
                if subproduct["sku"] is None:
                    continue  # Skip if no SKU
                children.append({
                    "title": title,
                    "listing_title": title,
                    "sku": subproduct["sku"],
                    "url": product_url,
                    "image_url": image_url,
                    "step_file": subproduct["step_file_url"],
                    "commonly_used": False
                })
            output["children"].append({
                "url": product_url,
                "title": title,
                "children": children
            })
            continue

    return output

def scrape_product_page(url):
    print(f"Scraping {url}")

    response = session.get(url)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    products = []

    # ==========================================================
    # CASE 1: Product Options tables (STEP links exist in HTML)
    # ==========================================================
    for table in soup.find_all("table"):

        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        for row in rows[1:]:

            cols = row.find_all("td")
            if len(cols) < 3:
                continue

            sku = cols[0].get_text(" ", strip=True)
            product_name = cols[1].get_text(" ", strip=True)

            if not sku.startswith("REV-"):
                continue

            step_url = None

            for a in cols[2].find_all("a", href=True):
                href = urljoin(response.url, a["href"])

                if href.lower().endswith(".step"):
                    step_url = href
                    break

            if step_url:
                products.append(
                    {
                        "sku": sku,
                        "product_name": product_name,
                        "step_file_url": step_url,
                    }
                )

    if products:
        return products

    # ==========================================================
    # CASE 2: Single product pages (currentProduct JS object)
    # ==========================================================
    html = response.text

    match = re.search(
        r'currentProduct\s*=\s*JSON\.parse\("((?:\\.|[^"])*)"\)',
        html,
        re.DOTALL,
    )

    if match:
        try:
            json_text = bytes(match.group(1), "utf-8").decode("unicode_escape")
            product = json.loads(json_text)

            sku = product.get("sku")
            title = product.get("title")

            if sku:
                return [
                    {
                        "sku": sku,
                        "product_name": title,
                        "step_file_url": f"https://www.revrobotics.com/content/cad/{sku}.STEP",
                    }
                ]

        except Exception as e:
            print(f"Failed to parse currentProduct on {url}: {e}")

    return []


def build_product_tree(page_node):
    """
    Convert a page tree node into a product tree node.

    Category
      ├── products
      └── subcategories
    """

    # Scrape products in this category
    scraped = scrape_product_list(page_node["url"])

    node = {
        "title": page_node["title"],
        "url": page_node["url"],
        "children": scraped["children"],  # products/groups
    }

    # Recursively append subcategories
    for child_page in page_node["children"]:
        child_node = build_product_tree(child_page)

        # Skip empty categories if desired
        if child_node["children"]:
            node["children"].append(child_node)

    return node


if __name__ == "__main__":
    page_tree = get_pagetree()

    output = []

    for category in page_tree:
        output.append(build_product_tree(category))

    combined_tree = {
        "url": BASE_URL + "/",
        "title": "REV",
        "children": output,
    }

    with open("structure_rev.json", "w", encoding="utf-8") as f:
        json.dump(combined_tree, f, indent=2, ensure_ascii=False)

