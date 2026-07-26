import json
import re
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

JSON_FILE = "structure.json"
OUTPUT_ROOT = Path("goBILDA")

STEP_URL = "https://www.gobilda.com/content/step_files/{}.zip"

MAX_WORKERS = 24

# -----------------------------------------------------------------------------
# Thread-local HTTP session
# -----------------------------------------------------------------------------

thread_local = threading.local()


def get_session():
    if not hasattr(thread_local, "session"):
        session = requests.Session()
        session.headers.update({
            "User-Agent": "goBILDA STEP Downloader"
        })
        thread_local.session = session
    return thread_local.session


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def already_downloaded(sku: str, destination: Path) -> bool:
    if not sku:
        return False

    return (
        (destination / f"{sku}.step").exists()
        or (destination / f"{sku}.stp").exists()
        or (destination / sku).is_dir()
    )


# -----------------------------------------------------------------------------
# Collect download jobs
# -----------------------------------------------------------------------------

jobs = []


def process_node(node: dict, current_folder: Path):
    """
    Recursively walks the JSON tree.

    Category nodes create folders.
    Valid SKU nodes become download jobs.
    """

    sku = node.get("sku")

    # Valid part
    if isinstance(sku, str) and sku.strip():
        if not already_downloaded(sku, current_folder):
            jobs.append((sku, current_folder))
        return

    # Recurse if there are children
    children = node.get("children")
    if not children:
        return

    folder_name = sanitize_filename(node.get("title", "Untitled"))
    folder = current_folder / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    for child in children:
        process_node(child, folder)


# -----------------------------------------------------------------------------
# Download one STEP file
# -----------------------------------------------------------------------------

def download_step(sku: str, destination: Path):
    if already_downloaded(sku, destination):
        return None

    session = get_session()

    try:
        response = session.get(STEP_URL.format(sku), timeout=60)

        if response.status_code != 200:
            return f"[Missing] {sku}"

        with tempfile.TemporaryDirectory() as tmpdir:

            tmpdir = Path(tmpdir)

            zip_path = tmpdir / f"{sku}.zip"
            zip_path.write_bytes(response.content)

            try:
                with zipfile.ZipFile(zip_path) as z:
                    z.extractall(tmpdir)
            except zipfile.BadZipFile:
                return f"[Bad ZIP] {sku}"

            step_files = []

            for pattern in (
                "*.step",
                "*.STEP",
                "*.stp",
                "*.STP",
            ):
                step_files.extend(tmpdir.rglob(pattern))

            if not step_files:
                return f"[No STEP] {sku}"

            if len(step_files) == 1:
                src = step_files[0]
                dst = destination / f"{sku}{src.suffix.lower()}"
                shutil.copy2(src, dst)

            else:
                part_folder = destination / sku
                part_folder.mkdir(exist_ok=True)

                for src in step_files:
                    shutil.copy2(src, part_folder / src.name)

        return None

    except Exception as e:
        return f"[Error] {sku}: {e}"


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    print("Loading JSON...")

    with open(JSON_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    OUTPUT_ROOT.mkdir(exist_ok=True)

    print("Building folder structure...")

    # Don't create a duplicate root folder if the JSON root is already "goBILDA"
    for child in data.get("children", []):
        process_node(child, OUTPUT_ROOT)

    print(f"Need to download {len(jobs):,} parts.")
    print(f"Using {MAX_WORKERS} threads.\n")

    errors = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

        futures = [
            executor.submit(download_step, sku, folder)
            for sku, folder in jobs
        ]

        for future in tqdm(as_completed(futures), total=len(futures), unit="part"):
            result = future.result()
            if result:
                errors.append(result)

    print("\nFinished.")

    if errors:
        with open("download_errors.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(errors))

        print(f"{len(errors):,} warnings/errors written to download_errors.txt")
    else:
        print("No errors.")


if __name__ == "__main__":
    main()