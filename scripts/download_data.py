"""Download NHTSA recall flat file and field layout into data/raw/."""

import sys
import zipfile
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

FLAT_FILE_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/FLAT_RCL_POST_2010.zip"
LAYOUT_URL = "https://static.nhtsa.gov/odi/ffdd/rcl/RCL.txt"


def download_file(url: str, dest: Path) -> None:
    """Stream-download a URL to a local file."""
    print(f"Downloading {url} ...")
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            f.writelines(resp.iter_bytes(chunk_size=8192))
    print(f"  Saved to {dest} ({dest.stat().st_size:,} bytes)")


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Download field layout
    layout_path = DATA_DIR / "RCL.txt"
    if layout_path.exists():
        print(f"Layout already exists: {layout_path}")
    else:
        download_file(LAYOUT_URL, layout_path)

    # Download and unzip flat file
    zip_path = DATA_DIR / "FLAT_RCL_POST_2010.zip"
    if zip_path.exists():
        print(f"Zip already exists: {zip_path}")
    else:
        download_file(FLAT_FILE_URL, zip_path)

    # Unzip
    print("Extracting zip ...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(DATA_DIR)
        extracted = zf.namelist()
    print(f"  Extracted: {extracted}")

    # Find the flat file and count rows
    flat_files = list(DATA_DIR.glob("FLAT_RCL*.txt"))
    if not flat_files:
        print("ERROR: No FLAT_RCL*.txt found after extraction.", file=sys.stderr)
        sys.exit(1)

    flat_path = flat_files[0]
    row_count = 0
    with open(flat_path, encoding="latin-1") as f:
        for _ in f:
            row_count += 1
    print(f"\nFlat file: {flat_path.name}")
    print(f"Total rows: {row_count:,}")


if __name__ == "__main__":
    main()
