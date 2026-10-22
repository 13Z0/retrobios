#!/usr/bin/env python3
"""Download the platform logos and system icons the site shows, once.

The site served these from other hosts: GitHub avatars and files on the
``master`` branch of upstream repositories. A rename upstream broke an image
in silence, and every page view hit third-party hosts. This tool copies them
into ``docs_assets/logos/`` and ``docs_assets/system-icons/``, which are
committed, and records where each came from in a ``sources.json`` beside
them. The site build only reads those copies (``sitediagrams.localize_images``).

Raster images are normalised to a square PNG (96 px for logos, 48 px for
system icons) so every tile has the same weight, and SVG is kept as published.
Normalising needs Pillow. This is a maintenance tool run by hand when a
logo changes, not a build step: the build stays on the standard library
and PyYAML.

    python scripts/fetch_site_images.py              # logos and icons
    python scripts/fetch_site_images.py --only logos
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

LOGO_DIR = ROOT / "docs_assets" / "logos"
ICON_DIR = ROOT / "docs_assets" / "system-icons"
LOGO_SIZE = 96
ICON_SIZE = 48
AVATAR_HOST = "avatars.githubusercontent.com"
USER_AGENT = "retrobios-site-images"

# Who owns each mark. Logos are shown to name the platform they belong to;
# the note says so, and gives the licence of the repository the file was
# taken from when that repository states one.
OWNERS = {
    "retroarch": ("libretro", "repository licence GPL-3.0"),
    "lakka": ("Lakka (libretro)", "file from libretro/retroarch-assets, CC-BY-4.0"),
    "batocera": ("Batocera", "repository licence MIT"),
    "recalbox": ("Recalbox", "file from homarr-labs/dashboard-icons, Apache-2.0"),
    "retrobat": ("RetroBat", "repository states no licence"),
    "emudeck": ("EmuDeck", "repository licence GPL-3.0"),
    "retrodeck": ("RetroDECK", "repository licence GPL-3.0"),
    "romm": ("RomM", "GitHub organisation avatar of rommapp"),
    "rocknix": ("ROCKNIX", "GitHub organisation avatar of ROCKNIX"),
    "amberelec": ("AmberELEC", "GitHub organisation avatar of AmberELEC"),
    "emuelec": ("EmuELEC", "GitHub organisation avatar of EmuELEC"),
    "knulli": ("KNULLI", "GitHub organisation avatar of knulli-cfw"),
    "retropie": ("RetroPie", "GitHub organisation avatar of RetroPie"),
    "bizhawk": ("TASVideos", "GitHub organisation avatar of TASEmulators"),
    "misterfpga": ("MiSTer FPGA", "GitHub organisation avatar of MiSTer-devel"),
    "minui": ("Shaun Inman", "GitHub avatar of shauninman, the author of MinUI"),
    "nextui": ("LoveRetro", "GitHub organisation avatar of LoveRetro"),
}


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def square_png(data: bytes, size: int) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit("Pillow is needed to normalise raster images: pip install Pillow") from exc
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    image.thumbnail((size, size), Image.LANCZOS)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(image, ((size - image.width) // 2, (size - image.height) // 2), image)
    out = io.BytesIO()
    tile.save(out, "PNG", optimize=True)
    return out.getvalue()


def is_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<?xml") or head.startswith(b"<svg")


def safe_svg(data: bytes, url: str) -> bytes:
    lowered = data.lower()
    for token in (b"<script", b"javascript:", b"onload=", b"<foreignobject"):
        if token in lowered:
            raise SystemExit(f"{url}: SVG carries {token.decode()}, not copied")
    return data


def write_manifest(directory: Path, note: str, entries: list[dict]) -> None:
    entries.sort(key=lambda entry: entry["file"])
    document = {"note": note, "images": entries}
    (directory / "sources.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def fetch_logos() -> None:
    with open(ROOT / "platforms" / "_registry.yml", encoding="utf-8") as handle:
        platforms = yaml.safe_load(handle)["platforms"]
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, entry in sorted(platforms.items()):
        url = entry.get("logo")
        if not url:
            continue
        download = f"{url}?s={LOGO_SIZE * 2}" if AVATAR_HOST in url else url
        data = fetch(download)
        if is_svg(data):
            file_name, payload = f"{name}.svg", safe_svg(data, url)
        else:
            file_name, payload = f"{name}.png", square_png(data, LOGO_SIZE)
        for stale in LOGO_DIR.glob(f"{name}.*"):
            if stale.name != file_name:
                stale.unlink()
        (LOGO_DIR / file_name).write_bytes(payload)
        owner, licence = OWNERS.get(name, (name, "ownership not recorded"))
        entries.append({
            "file": file_name, "platform": name, "source": url,
            "owner": owner, "licence": licence,
        })
        print(f"  {file_name} from {url}")
    write_manifest(
        LOGO_DIR,
        "Each platform's own logo, owned by its project and shown only to name that "
        "platform. Copied once by scripts/fetch_site_images.py.",
        entries,
    )


def fetch_icons() -> None:
    from generate_site import _group_by_manufacturer, load_database
    from siterender import SYSTEM_ICON_BASE, _icon_name

    db = load_database(str(ROOT / "database.json"))
    names = sorted({
        _icon_name(manufacturer, console)
        for manufacturer, consoles in _group_by_manufacturer(db).items()
        for console in consoles
    })
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in names:
        url = f"{SYSTEM_ICON_BASE}/{urllib.parse.quote(name)}.png"
        try:
            data = fetch(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        file_name = f"{name}.png"
        (ICON_DIR / file_name).write_bytes(square_png(data, ICON_SIZE))
        entries.append({"file": file_name, "source": url})
        print(f"  {file_name}")
    write_manifest(
        ICON_DIR,
        "System icons from libretro/retroarch-assets (xmb/systematic), "
        "licensed CC-BY-4.0 by the libretro team. Resized to 48 px.",
        entries,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=("logos", "icons"), help="fetch one kind only")
    args = parser.parse_args()
    if args.only in (None, "logos"):
        print("Platform logos:")
        fetch_logos()
    if args.only in (None, "icons"):
        print("System icons:")
        fetch_icons()


if __name__ == "__main__":
    main()
