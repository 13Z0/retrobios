#!/usr/bin/env python3
"""Report dump-catalog coverage of the collection.

Reads provenance/*.json snapshots and database.json, then reports per
source how many catalog entries the collection holds and which are
missing. Missing entries are acquisition targets: catalog-verified
dumps the collection does not have yet.

Usage:
    python scripts/provenance_report.py
    python scripts/provenance_report.py --missing
    python scripts/provenance_report.py --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from common import (
    DEFAULT_PROVENANCE_DIR,
    load_database,
    load_provenance_snapshots,
    write_if_changed,
)


def archive_members(db: dict) -> dict[tuple[str, int], list[tuple[str, str]]]:
    """(crc32, size) of every member of the collection's ZIPs -> (zip, member).

    Read from the central directories alone: a romset member is a dump the
    collection already holds, and listing it as an acquisition target sent
    searches after astrocdw.zip's bioswhit.bin and the Gamate BIOS.
    """
    members: dict[tuple[str, int], list[tuple[str, str]]] = {}
    for entry in db.get("files", {}).values():
        path = entry.get("path", "")
        if not path.endswith(".zip") or not os.path.exists(path):
            continue
        try:
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    key = (f"{info.CRC:08x}", info.file_size)
                    members.setdefault(key, []).append((path, info.filename))
        except (zipfile.BadZipFile, OSError) as exc:
            print(f"  WARNING: {path}: {exc}", file=sys.stderr)
    return members


def _held_in_archive(entry: dict, members: dict) -> bool:
    """Whether a catalog entry is a member of one of the collection's ZIPs.

    crc32 and size only nominate candidates. A declared sha1 must match the
    member's bytes.
    """
    crc = str(entry.get("crc32") or "").lower()
    size = entry.get("size")
    if not crc or size is None:
        return False
    candidates = members.get((crc.zfill(8), int(size)), [])
    sha1 = str(entry.get("sha1") or "").lower()
    if not sha1:
        return bool(candidates)
    for path, name in candidates:
        try:
            with zipfile.ZipFile(path) as archive:
                if hashlib.sha1(archive.read(name)).hexdigest() == sha1:
                    return True
        except (zipfile.BadZipFile, OSError, KeyError) as exc:
            print(f"  WARNING: {path}:{name}: {exc}", file=sys.stderr)
    return False


RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"


def romset_index(recipes_dir: Path = RECIPES_DIR) -> dict[tuple[str, int], list[dict]]:
    """(crc32, size) of every member of an imported romset -> where it sits.

    A dump the collection lacks may still ship inside an emulator's romset:
    17 TOSEC ZX Spectrum ROMs are members of MAME's spectrum.zip. Naming the
    set and the versions that carry it turns a dead end into a download.
    """
    index: dict[tuple[str, int], list[dict]] = {}
    for snapshot in sorted(recipes_dir.glob("*.json")):
        with snapshot.open(encoding="utf-8") as handle:
            data = json.load(handle)
        source = data.get("source", snapshot.stem)
        for entry in data.get("entries", []):
            dats = entry.get("dats") or [entry.get("dat", "")]
            for member in entry.get("members", []) or []:
                crc, size = str(member.get("crc32") or "").lower(), member.get("size")
                if not crc or size is None:
                    continue
                index.setdefault((crc.zfill(8), int(size)), []).append({
                    "source": source,
                    "archive": entry.get("name", ""),
                    "member": member.get("name", ""),
                    "sha1": member.get("sha1", ""),
                    "dats": dats,
                })
    return index


def romsets_holding(entry: dict, index: dict) -> list[dict]:
    """The imported romsets whose members are this dump.

    A member and an entry that both carry a sha1 must agree on it: crc32
    and size alone match by accident across 40 000 ROMs.
    """
    crc, size = str(entry.get("crc32") or "").lower(), entry.get("size")
    if not crc or size is None:
        return []
    sha1 = str(entry.get("sha1") or "").lower()
    # A set whose contents changed between versions is stored once per
    # distinct content: one line per set and member, with every version.
    merged: dict[tuple[str, str, str], dict] = {}
    for hit in index.get((crc.zfill(8), int(size)), []):
        if sha1 and hit["sha1"] and hit["sha1"].lower() != sha1:
            continue
        key = (hit["source"], hit["archive"], hit["member"])
        held = merged.setdefault(key, {**hit, "dats": []})
        held["dats"] = sorted(set(held["dats"]) | set(hit["dats"]))
    return list(merged.values())


def dump_identity(entry: dict) -> tuple:
    """What makes two catalog listings one dump: the strongest hash stated."""
    if entry.get("sha1"):
        return ("sha1", str(entry["sha1"]).lower())
    if entry.get("md5"):
        return ("md5", str(entry["md5"]).lower(), entry.get("size"))
    if entry.get("crc32"):
        return ("crc32", str(entry["crc32"]).lower(), entry.get("size"))
    return ("name", entry.get("dat", ""), entry.get("name", ""))


def distinct_missing(report: dict) -> int:
    """Missing dumps across every catalog, each counted once."""
    return len({dump_identity(e) for data in report.values() for e in data["missing"]})


def build_report(
    db: dict,
    snapshots: dict,
    members: dict | None = None,
    romsets: dict | None = None,
) -> dict:
    """Compare each snapshot against the collection.

    A DAT counts as covered when the collection holds at least one of
    its entries. Missing entries from covered DATs are acquisition
    targets. Missing entries from DATs the collection does not cover at
    all are out of scope and only counted. No-Intro tags every non-game
    dump "[BIOS]", including digital title distribution, so without this
    split the target list is swamped by content the project never ships.
    """
    by_sha1 = db.get("files", {})
    # The members a romset holds are held too, whoever asks: the site built
    # its Wanted page without them and asked the public for the Astrocade
    # and Gamate BIOS that astrocdw.zip and gamate.zip already carry.
    members = archive_members(db) if members is None else members
    romsets = romset_index() if romsets is None else romsets
    by_md5_size = {
        (entry.get("md5", ""), entry.get("size", 0)) for entry in by_sha1.values()
    }
    # Redump lists some dumps by crc32 alone (ps2-0101jd-20030110, DTL-H10100):
    # collected as a loose file it would stay MISSING for good without this.
    by_crc32_size = {
        (str(entry.get("crc32", "")).lower(), entry.get("size", 0))
        for entry in by_sha1.values()
        if entry.get("crc32")
    }

    report = {}
    for source, snapshot in sorted(snapshots.items()):
        matched: set = set()
        covered_dats = set()
        unmatched = []
        for entry in snapshot["entries"]:
            if (
                entry.get("sha1") in by_sha1
                or (entry.get("md5"), entry.get("size")) in by_md5_size
                or (str(entry.get("crc32") or "").lower(), entry.get("size")) in by_crc32_size
                or _held_in_archive(entry, members)
            ):
                matched.add(dump_identity(entry))
                covered_dats.add(entry.get("dat", ""))
            else:
                unmatched.append(entry)
        # One dump listed by several DATs (No-Intro's three FM-7 DATs share
        # DICROM.rom, KANJI.rom...) is one dump: counted per listing, the
        # report announced 2 485 missing for 2 284 distinct ones.
        merged: dict = {}
        for entry in unmatched:
            if entry.get("dat", "") not in covered_dats:
                continue
            held = merged.get(dump_identity(entry))
            if held is None:
                merged[dump_identity(entry)] = {**entry, "dats": [entry.get("dat", "")]}
            elif entry.get("dat", "") not in held["dats"]:
                held["dats"].append(entry.get("dat", ""))
        missing = list(merged.values())
        for entry in missing:
            holders = romsets_holding(entry, romsets)
            if holders:
                entry["romsets"] = holders
        out_of_scope = len({
            dump_identity(e) for e in unmatched if e.get("dat", "") not in covered_dats
        })
        matched = len(matched)
        report[source] = {
            "imported_at": snapshot.get("imported_at", ""),
            "dats": snapshot.get("dats", {}),
            "total": len(snapshot["entries"]),
            "matched": matched,
            "covered_dats": sorted(covered_dats),
            "missing": missing,
            "out_of_scope": out_of_scope,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False, description="Dump-catalog coverage report")
    parser.add_argument("--db", default="database.json", help="Database path")
    parser.add_argument(
        "--provenance-dir",
        default=DEFAULT_PROVENANCE_DIR,
        help="Directory with dump-catalog snapshots",
    )
    parser.add_argument("--missing", action="store_true", help="List missing entries")
    parser.add_argument(
        "--derive",
        action="store_true",
        help="Find missing entries a held dump contains (halves, word splits, "
        "byte swaps, concatenations). Reads the collection, about 35 s",
    )
    parser.add_argument("--json", action="store_true", help="Full report as JSON")
    parser.add_argument(
        "--write-derivable",
        metavar="PATH",
        help="With --derive, record the derivable entries for the site "
        "(the pipeline writes derivable.json)",
    )
    args = parser.parse_args()
    if args.write_derivable and not args.derive:
        parser.error("--write-derivable records what --derive finds: pass both")

    db = load_database(args.db)
    snapshots = load_provenance_snapshots(args.provenance_dir)
    if not snapshots:
        print(f"No provenance snapshots in {args.provenance_dir}/")
        return 0

    report = build_report(db, snapshots, archive_members(db))
    derived: dict[str, dict] = {}
    if args.derive:
        from derivations import derivable  # noqa: PLC0415

        derived = derivable((e for d in report.values() for e in d["missing"]), db)
        for data in report.values():
            data["derivable"] = {
                e["sha1"]: derived[e["sha1"]] for e in data["missing"]
                if e.get("sha1") in derived
            }
        if args.write_derivable:
            # The site cannot spend 80 s reading the collection on every
            # build: the pipeline records what this pass found, and the
            # Wanted page stops asking for dumps the collection can produce.
            write_if_changed(
                args.write_derivable,
                json.dumps(dict(sorted(derived.items())), indent=2, sort_keys=True) + "\n",
            )

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    for source, data in report.items():
        in_scope = data["matched"] + len(data["missing"])
        pct = 100 * data["matched"] / in_scope if in_scope else 0
        print(
            f"  {source} ({data['imported_at']}): "
            f"{data['matched']}/{in_scope} in collection ({pct:.0f}%) "
            f"across {len(data['covered_dats'])} covered DATs"
        )
        if data["out_of_scope"]:
            print(
                f"    {data['out_of_scope']} entries in DATs the collection "
                f"does not cover, not counted as targets"
            )
        if args.missing:
            for entry in data["missing"]:
                label = entry.get("description") or entry["name"]
                source = derived.get(entry.get("sha1", ""))
                if source:
                    print(f"    DERIVABLE {entry['name']} ({label}): {source['transform']} "
                          f"of {' + '.join(source['from'])}")
                else:
                    print(f"    MISSING {entry['name']} ({label}) sha1={entry.get('sha1')}")
                for hit in entry.get("romsets", []):
                    versions = hit["dats"][0] if len(hit["dats"]) == 1 else (
                        f"{hit['dats'][0]} to {hit['dats'][-1]}"
                    )
                    print(f"      in {hit['source']} {hit['archive']} as "
                          f"{hit['member']} ({versions})")

    total_missing = distinct_missing(report)
    if total_missing:
        line = f"  {total_missing} catalogued dumps missing from collection"
        if args.derive:
            line += f", {len(derived)} of them derivable from held dumps"
        print(line)
    return 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
