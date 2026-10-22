#!/usr/bin/env python3
"""The search gate: ten queries a visitor types, and the pages that answer them.

Runs measure.mjs against a served build and counts a query as answered when
one of its expected pages is in the first three results of the site's own
search box.

    python website/tools/search_gate.py http://127.0.0.1:4321/retrobios/
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent

# Query, then the pages that answer it (path prefixes under the site).
QUERIES: dict[str, tuple[str, ...]] = {
    "playstation bios": ("systems/sony/",),
    # The file itself: its card on the system page, or a core that reads it.
    "scph5501": ("systems/sony/", "emulators/beetle_psx/", "emulators/duckstation/",
                 "emulators/duckstation-legacy/", "emulators/pcsx1/", "emulators/swanstation/",
                 "emulators/ares/", "emulators/pcsx_rearmed/", "files/"),
    "batocera folder": ("platforms/batocera/",),
    "neogeo.zip": ("emulators/fbneo", "emulators/mame", "systems/snk/", "systems/arcade"),
    "duckstation": ("emulators/duckstation/", "emulators/duckstation-legacy/"),
    "steam deck": ("platforms/emudeck/", "platforms/retrodeck/", "which-pack/"),
    "missing bios": ("wiki/troubleshooting/", "wiki/what-are-bios-files/", "wiki/faq/"),
    "md5": ("wiki/verification-modes/",),
    "retroarch system folder": ("platforms/retroarch/",),
    "knulli": ("platforms/knulli/",),
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
        top = [entry.split(" | ")[1][len(root):] for entry in result["top"][:3]]
        hit = any(page.startswith(QUERIES[result["query"]]) for page in top)
        answered += hit
        print(f"{'ok  ' if hit else 'MISS'} {result['query']!r}: {', '.join(top)}")
    print(f"{answered}/{len(QUERIES)} queries answered in the top 3")
    return 0 if answered >= 9 else 1


if __name__ == "__main__":
    sys.exit(main())
