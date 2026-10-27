"""The site's measurement pages, from the index sync_measurements.py writes.

Three places read the same index: the Tested block of each emulator page,
the Measurements explorer page and the host table of the methodology page.
A number is never shown without its method and host beside it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import sitemarkup as markup
from siterender import _escape_tags

EXPLORER_PAGE = "measurements.md"
METHOD_PAGE = "wiki/measurements.md"
HOSTS_MARKER = "<!-- measurements-hosts -->"


def by_emulator(index: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in index.get("items", []):
        out.setdefault(item["emulator"], []).append(item)
    return out


def _text(value: object) -> str:
    """Record text as the page shows it. A semicolon joining two clauses
    ends a sentence on the site: the record keeps the words as written."""
    text = " ".join(str(value or "").split())
    text = re.sub(r";\s+(\w)", lambda m: ". " + m[1].upper(), text)
    return _escape_tags(text)


def _short(commit: str | None) -> str:
    return f"`{commit[:12]}`" if commit else "unknown"


def tested_item(item: dict) -> list[str]:
    """One test: what ran, the result, the method and host beside it, the record."""
    title = item["claim"] or f"Benchmark of {item['content'] or 'the core alone'}"
    runs = "1 run" if item["runs"] == 1 else f"{item['runs']} runs, the latest shown"
    core = item["core"]
    build = f"{_text(core['library'])} at {_short(core['commit'])}" if core.get("commit") \
        else _text(core["library"])
    lines = [
        f"- **{_text(title)}**",
        "",
        f"    **Result:** `{item['status']}`. {_text(item['statement'])}.",
        "",
    ]
    if item.get("method"):
        lines += [f"    **Method:** {_text(item['method'])}", ""]
    lines += [
        f"    **Ran on:** {_text(item['host'])}, {item['date'][:10]}. Core build: {build}. "
        f"{item['variants']} variant{'s' if item['variants'] != 1 else ''}, "
        f"{item['repetitions']} repetition{'s' if item['repetitions'] != 1 else ''} each"
        + (f", {item['flags']} flag{'s' if item['flags'] != 1 else ''} raised" if item["flags"] else "")
        + ".",
        "",
        f"    [Full record]({item['record_url']}), [its data]({item['data_url']}), {runs}.",
        "",
    ]
    return lines


def tested_section(name: str, items: list[dict]) -> list[str]:
    """The Tested block of an emulator page, empty when nothing was recorded."""
    if not items:
        return []
    lines = [
        "## Tested",
        "",
        f"This emulator was run in real conditions by the [core probe](../{METHOD_PAGE}). "
        f"Each test below is the latest of its kind. "
        f"[All measurements](../{EXPLORER_PAGE}), "
        f"[these records as SQLite](../downloads/measurements/{name}.sqlite).",
        "",
    ]
    for item in sorted(items, key=lambda i: (i["date"], i["subject"]), reverse=True):
        lines += tested_item(item)
    return lines


def hosts_table(index: dict) -> list[str]:
    hosts: dict[str, int] = {}
    for item in index.get("items", []):
        # The load average differs per run: the computer is what comes before it.
        computer = item["host"].split(", load ")[0]
        hosts[computer] = hosts.get(computer, 0) + 1
    if not hosts:
        return ["No result is recorded at the pinned commit yet.", ""]
    return [
        "| Computer | Tests |",
        "|----------|------:|",
        *(f"| {_text(host)} | {count} |" for host, count in sorted(hosts.items())),
        "",
    ]


def with_hosts(page: str, index: dict) -> str:
    """The methodology page with the computers the results ran on."""
    return page.replace(HOSTS_MARKER, "\n".join(hosts_table(index)).rstrip("\n"))


def generate_explorer_page(index: dict) -> str:
    commit = index.get("commit")
    repo = index.get("repo") or ""
    source = (f"[retrobios-measurements]({repo}/tree/{commit}) at {_short(commit)}"
              if commit else "the measurements project, not read in this build")
    return "\n".join([
        "# Measurements",
        "",
        f"Emulators run in real conditions: {index.get('records', 0)} records, "
        f"{index.get('count', 0)} tests, read from {source}. "
        f"[How they are made]({METHOD_PAGE}).",
        "",
        " ".join([
            markup.button("SQLite of every record", "downloads/measurements.sqlite", primary=True),
            markup.button("Index as JSON", "data/measurements/index.json"),
        ]),
        "",
        "Each row is the latest record of one test, with its method and the computer it "
        "ran on. One SQLite file per emulator is linked from its page.",
        "",
        "<MeasurementsExplorer />",
        "",
    ])


def sqlite_tables(path: Path) -> list[tuple[str, list[str]]]:
    """Each table of a database with its columns, in the order created."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY rowid")]
        return [(name, [row[1] for row in connection.execute(f"PRAGMA table_info({name})")])
                for name in names]
    finally:
        connection.close()


def data_page_section(path: Path) -> list[str]:
    """The Data page's description of measurements.sqlite."""
    return [
        "## Measurements",
        "",
        "[`measurements.sqlite`](downloads/measurements.sqlite) holds every core probe "
        "record at the pinned commit of [retrobios-measurements]"
        "(https://github.com/Abdess/retrobios-measurements), and "
        "`downloads/measurements/<emulator>.sqlite` the records of one emulator, linked "
        f"from its page. The [Measurements]({EXPLORER_PAGE}) page reads "
        "[`data/measurements/index.json`](data/measurements/index.json). A record line is "
        "valid against [`core-probe-record.schema.json`](api/v1/schemas/core-probe-record.schema.json) "
        "and its data file against [`core-probe-data.schema.json`](api/v1/schemas/core-probe-data.schema.json).",
        "",
        "| Table | Columns |",
        "|-------|---------|",
        *(f"| `{name}` | {', '.join(f'`{c}`' for c in columns)} |" for name, columns in sqlite_tables(path)),
        "",
        "`meta` names the measurements commit and the record version. A list or object "
        "value is JSON text, an absent one empty. `runs.superseded` is 1 for a record a "
        "later one corrects.",
        "",
        "```sql",
        "SELECT r.emulator, c.claim, c.status, r.recorded_at, h.cpu_model",
        "FROM runs r JOIN checks c ON c.run_id = r.id JOIN hosts h ON h.id = r.host_id",
        "WHERE r.superseded = 0 ORDER BY r.recorded_at;",
        "```",
        "",
    ]
