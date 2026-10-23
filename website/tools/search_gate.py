#!/usr/bin/env python3
"""The search gate: queries a visitor types, and the pages that answer them.

Runs measure.mjs against a served build, through the site's own search box,
and checks each query's expected pages against the rank it must reach: the
first result, or the first three.

    python website/tools/search_gate.py http://127.0.0.1:4321/retrobios/
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# Query: (pages that answer it, as path prefixes under the site, and the rank one
# of them must reach). An emulator name must find the emulator itself first,
# not its launcher or a frozen copy.
QUERIES: dict[str, tuple[tuple[str, ...], int]] = {
    "batocera folder": (("platforms/batocera/",), 1),
    "missing bios": (("wiki/troubleshooting/", "wiki/what-are-bios-files/", "wiki/faq/"), 1),
    "neogeo.zip": (("emulators/fbneo", "emulators/mame", "systems/snk/", "systems/arcade"), 1),
    "retroarch system folder": (("platforms/retroarch/",), 1),
    "installer": (("wiki/installer/", "wiki/first-install/"), 1),
    # The file's card on its system page, where it is downloaded. "systems/sony"
    # holds the manufacturer page and the console's own (systems/sony-playstation/).
    "scph5501": (("systems/sony",), 3),
    "md5": (("wiki/verification-modes/",), 1),
    "where do bios files go": (("which-pack/", "wiki/first-install/"), 1),
    "dolphin": (("emulators/dolphin/",), 1),
    "duckstation": (("emulators/duckstation/",), 1),
    # A console and "bios": the console's page, before any setup's list for it.
    "playstation bios": (("systems/sony",), 3),
    "dreamcast bios": (("systems/sega",), 3),
    "neo geo bios": (("systems/snk",), 3),
    "wonderswan color": (("systems/bandai",), 3),
    # The platforms' own names for a console (psx, dc, gba), a search field
    # of its page.
    "psx bios": (("systems/sony",), 3),
    "dc bios missing": (("systems/sega",), 3),
    "gba bios": (("systems/nintendo",), 3),
    "bios not found": (("wiki/troubleshooting/",), 3),
    "offline install": (("wiki/installer/",), 3),
    "which mame version": (("wiki/technical-faq/",), 3),
    # A device name: the guide of the handhelds that run it.
    "r36s": (("platforms/", "which-pack/"), 3),
    "steam deck": (("platforms/emudeck/", "platforms/retrodeck/", "which-pack/"), 1),
    "knulli": (("platforms/knulli/",), 1),
}


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4321/retrobios/"
    root = "/" + base.split("/", 3)[3]
    run = subprocess.run(
        ["node", str(TOOLS / "measure.mjs"), "search", base, "starlight-ui", *QUERIES],
        capture_output=True, text=True, check=True,
    )
    answered = 0
    for line in run.stdout.splitlines():
        result = json.loads(line)
        if "query" not in result:
            print(f"first search: {result['first_search_kb']} KB in {result['first_search_requests']} requests")
            continue
        pages, rank = QUERIES[result["query"]]
        top = [entry.split(" | ") for entry in result["top"][:3]]
        hit = any(href[len(root):].startswith(pages) for _title, href in top[:rank])
        answered += hit
        titles = "; ".join(title for title, _href in top)
        print(f"{'ok  ' if hit else 'MISS'} {result['query']!r} (top {rank}): {titles}")
    print(f"{answered}/{len(QUERIES)} queries answered")
    return 0 if answered == len(QUERIES) else 1


if __name__ == "__main__":
    sys.exit(main())
