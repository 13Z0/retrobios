#!/usr/bin/env python3
"""Lighthouse mobile (default form factor) on a list of pages, one summary line each.

    python lighthouse.py <out dir> <name>=<base url> ... -- <page> ...

Needs `npx lighthouse` and Chrome at /usr/bin/google-chrome.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CATEGORIES = ("performance", "accessibility", "best-practices", "seo")


def main() -> int:
    args = sys.argv[1:]
    out = Path(args.pop(0))
    split = args.index("--")
    engines = dict(item.split("=", 1) for item in args[:split])
    pages = args[split + 1:]
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "CHROME_PATH": "/usr/bin/google-chrome"}
    for name, base in engines.items():
        for page in pages:
            slug = page.strip("/").replace("/", "_") or "home"
            report = out / f"{name}-{slug}.json"
            # Under WSL chrome-launcher names its profile folder with a Windows path,
            # created relative to the working directory: run it inside the output folder.
            subprocess.run(
                ["npx", "--prefix", str(Path(__file__).resolve().parent.parent), "lighthouse", base + page,
                 "--quiet", "--output=json", f"--output-path={report.resolve()}",
                 "--chrome-flags=--headless=new --no-sandbox",
                 f"--only-categories={','.join(CATEGORIES)}"],
                env=env, cwd=out, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            data = json.loads(report.read_text())
            cats, audits = data["categories"], data["audits"]
            failed = [k for k, a in audits.items()
                      if a.get("score") is not None and a["score"] < 1 and a.get("scoreDisplayMode") == "binary"]
            print(json.dumps({
                "engine": name, "page": page or "/",
                **{c: round(cats[c]["score"] * 100) for c in CATEGORIES},
                "lcp": audits["largest-contentful-paint"]["displayValue"],
                "tbt": audits["total-blocking-time"]["displayValue"],
                "cls": audits["cumulative-layout-shift"]["displayValue"],
                "bytes": audits["total-byte-weight"]["displayValue"],
                "dom": audits["dom-size"]["displayValue"],
                "failed": failed,
            }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
