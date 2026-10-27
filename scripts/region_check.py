#!/usr/bin/env python3
"""Region checks: which regional BIOS a core reads for a game, and whether
the game boots without it.

For a system whose profile marks its BIOS entries with `region:`, each test
game of a region (tests/campaign/content.yml) runs through core_probe four
times, each a record in measurements/<emulator>.jsonl:

  full    every regional BIOS the profile declares
  own     only the entries whose `region:` holds the game's region
  others  only the entries of the other regions
  pack    the files of `generate_pack.py --emulator E --region R`, at
          the place the pack puts them

The outcome reads the records: the file the core opened in the full run
(from strace) settles the `region:` claim, the verdicts of `own` and
`others` say whether the BIOS is locked to the region, and `pack` says
whether the pack filtered for the region boots the region's game.

  run     the four runs for each region game of one core and system
  report  the outcome of every region record already in measurements/

A maintainer starts a run. Nothing here runs on a timer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTENT_FILE = ROOT / "tests" / "campaign" / "content.yml"
CORES = ROOT / ".cache" / "region" / "cores"
NIGHTLY = "https://buildbot.libretro.com/nightly/linux/x86_64/latest/{core}_libretro.so.zip"
VARIANTS = ("full", "own", "others", "pack")
SUBJECT = re.compile(r"^region-(?P<core>[a-z0-9_]+)-(?P<game>[a-z0-9-]+)-(?P<variant>full|own|others|pack)$")
# The territory a test game names, as generate_pack --region takes it.
PACK_REGION = {"japan": "jp", "north-america": "us", "europe": "eu"}
REGION_WORDS = {"japan": "Japanese", "north-america": "US", "europe": "European"}


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

def regions_of(entry: dict) -> list[str]:
    regions = entry.get("region") or []
    return [regions] if isinstance(regions, str) else list(regions)


def entry_dest(entry: dict) -> str:
    path = entry.get("path")
    if isinstance(path, str) and path and not path.endswith("/"):
        return path
    return entry["name"]


def region_entries(profile: dict, system: str) -> list[dict]:
    """The BIOS entries of a system that a `region:` ties to territories."""
    out = []
    for entry in profile.get("files") or ():
        systems = entry.get("systems") or ([entry["system"]] if entry.get("system")
                                            else profile.get("systems") or [])
        if (system in systems and regions_of(entry) and "world" not in regions_of(entry)
                and entry.get("category", "bios") == "bios" and entry.get("mode") != "standalone"):
            out.append(entry)
    return out


def plan_variants(entries: Iterable[dict], region: str,
                  local: Callable[[dict], Path | None]) -> dict[str, dict[str, Path]]:
    """The staged files of `full`, `own` and `others`, dest -> file. An entry
    the collection does not hold is left out of every variant."""
    held = [(entry, local(entry)) for entry in entries]
    held = [(entry, path) for entry, path in held if path]
    full = {entry_dest(e): p for e, p in held}
    own = {entry_dest(e): p for e, p in held if region in regions_of(e)}
    others = {entry_dest(e): p for e, p in held if region not in regions_of(e)}
    return {"full": full, "own": own, "others": others}


def subject(core: str, game: str, variant: str) -> str:
    words = re.findall(r"[a-z0-9]+", game.lower())
    return f"region-{core.lower().replace('-', '_')}-{'-'.join(words)[:60].strip('-')}-{variant}"


def opened(record: dict) -> set[str]:
    return {item["path"] for variant in record["variants"] for rep in variant["repetitions"]
            for item in rep.get("files_opened") or ()}


def frames(record: dict) -> list:
    rep = record["variants"][0]["repetitions"][0]
    return (rep.get("observables") or {}).get("frame_hashes") or []


def verdict(record: dict) -> str:
    return record["variants"][0].get("verdict") or "unknown"


def outcome(records: dict[str, dict], entries: list[dict], region: str) -> dict:
    """What the four records of one game on one core settle.

    `selected` is the regional file the core opened with every one staged.
    The `region:` claim is `confirmed` when the regions of what it opened
    hold the game's region, `contradicted` when none does, `unread` when it
    opened none. Each variant's result is its oracle verdict. While no
    reference frame is known for the game, a variant is compared with the
    full run instead: `as_full` when it samples the same frames, which is
    what the oracle's reference frames will name.
    """
    by_dest = {entry_dest(e): e for e in entries}
    full = records.get("full")
    selected = sorted(opened(full) & set(by_dest)) if full else []
    if not full:
        claim = "unrun"
    elif not selected:
        claim = "unread"
    elif any(region in regions_of(by_dest[d]) for d in selected):
        claim = "confirmed"
    else:
        claim = "contradicted"
    results: dict[str, dict] = {}
    for label in VARIANTS:
        record = records.get(label)
        if not record:
            continue
        loaded = all(rep.get("loaded") for v in record["variants"] for rep in v["repetitions"])
        item = {"record": record["id"], "verdict": verdict(record), "loaded": loaded,
                "staged": sorted(record["variants"][0]["system_files"])}
        if full and label != "full":
            item["as_full"] = loaded and frames(record) == frames(full)
        results[label] = item

    full_verdict = verdict(full) if full else None

    def boots(label: str) -> bool | None:
        item = results.get(label)
        if not item:
            return None
        if item["verdict"] != "unknown":
            return item["verdict"] == "boots"
        # No verdict: the interim rule, as the full run behaves, and only
        # while the full run itself is not shown to fail.
        if full_verdict in ("boots", "unknown") and "as_full" in item:
            return item["as_full"]
        return None

    return {
        "region": region,
        "selected": [by_dest[d]["name"] for d in selected],
        "claim": claim,
        "variants": results,
        "own_boots": boots("own"),
        "others_boot": boots("others"),
        "pack_boots": boots("pack"),
        "judged_by": "verdict" if full_verdict not in (None, "unknown") else "full_run_frames",
    }


def sentence(game_region: str, result: dict) -> str:
    """The outcome in words, for the site."""
    word = REGION_WORDS.get(game_region, game_region)
    own, others = result.get("own_boots"), result.get("others_boot")
    if own is None:
        return f"The {word} game has no run with its own BIOS yet."
    first = f"The {word} BIOS {'boots' if own else 'does not boot'} the {word} game"
    if others is None:
        return first + "."
    if others:
        return first + ", and so do the other regions' BIOS."
    return first + ", the other regions' BIOS do not."


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

def load_content(path: Path = CONTENT_FILE) -> dict[str, list[dict]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, list[dict]] = {}
    for system, spec in (data.get("systems") or {}).items():
        for item in spec.get("content") or ():
            out.setdefault(system, []).append({**item, "romfetch": spec["romfetch"]})
    return out


def content_file(item: dict, content_dir: Path) -> Path:
    folder = content_dir / item["romfetch"] / item["name"]
    if item.get("load") == "cue":
        cues = sorted(folder.glob("*.cue"))
        if len(cues) != 1:
            raise FileNotFoundError(f"{folder}: {len(cues)} .cue files, one expected")
        return cues[0]
    if item.get("load"):
        return folder / item["load"]
    files = [p for p in sorted(folder.iterdir()) if p.is_file() and p.name != "identity.json"]
    if len(files) != 1:
        raise FileNotFoundError(f"{folder}: {len(files)} files, name one with load:")
    return files[0]


def nightly_core(core: str) -> tuple[Path, str]:
    """The buildbot nightly of a core, downloaded once, and its URL."""
    import zipfile  # noqa: PLC0415

    url = NIGHTLY.format(core=core)
    target = CORES / f"{core}_libretro.so"
    if not target.is_file():
        CORES.mkdir(parents=True, exist_ok=True)
        archive = CORES / f"{core}.zip"
        with urllib.request.urlopen(url, timeout=300) as response, archive.open("wb") as out:
            out.write(response.read())
        with zipfile.ZipFile(archive) as handle:
            member = f"{core}_libretro.so"
            if member not in handle.namelist():
                raise FileNotFoundError(f"{archive.name}: no {member} inside")
            partial = target.with_suffix(".part")
            partial.write_bytes(handle.read(member))
            partial.replace(target)
        archive.unlink()
    return target, url


def pack_files(emulator: str, region: str, work: Path) -> dict[str, Path]:
    """The files generate_pack puts in the emulator's pack for one region,
    extracted, by their path in the pack."""
    from ziptools import safe_extract_zip  # noqa: PLC0415

    out_dir = work / "pack"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_pack.py"), "--emulator", emulator,
                    "--region", PACK_REGION[region], "--output-dir", str(out_dir)],
                   cwd=ROOT, check=True, capture_output=True, text=True)
    zips = sorted(out_dir.glob("*.zip"))
    if len(zips) != 1:
        raise FileNotFoundError(f"{out_dir}: {len(zips)} packs, one expected")
    tree = work / "pack-tree"
    safe_extract_zip(str(zips[0]), str(tree))
    return {p.relative_to(tree).as_posix(): p for p in sorted(tree.rglob("*")) if p.is_file()
            and not p.name.upper().startswith("README")}


def local_resolver() -> Callable[[dict], Path | None]:
    from common import load_database, resolve_local_file  # noqa: PLC0415

    db = load_database(str(ROOT / "database.json"))

    def local(entry: dict) -> Path | None:
        hint = entry.get("path") if isinstance(entry.get("path"), str) else ""
        path, status = resolve_local_file(entry, db, dest_hint=hint or "")
        if not path or status in ("not_found", "hash_mismatch"):
            return None
        found = Path(path) if Path(path).is_absolute() else ROOT / path
        return found if found.is_file() else None

    return local


def run(args: argparse.Namespace) -> int:
    from common import load_emulator_profiles  # noqa: PLC0415

    profile = load_emulator_profiles(str(ROOT / "emulators"))[args.emulator]
    entries = region_entries(profile, args.system)
    if not entries:
        print(f"ERROR: {args.emulator} declares no regional BIOS for {args.system}", file=sys.stderr)
        return 2
    games = [g for g in load_content().get(args.system, []) if g.get("region") in PACK_REGION]
    if not games:
        print(f"ERROR: content.yml has no regional game for {args.system}", file=sys.stderr)
        return 2
    core, url = (Path(args.core), args.core_artifact) if args.core else nightly_core(args.core_name)
    local = local_resolver()
    failures = 0
    for game in games:
        load = content_file(game, Path(args.content_dir))
        with tempfile.TemporaryDirectory(dir=ROOT / "tmp") as scratch:
            variants = plan_variants(entries, game["region"], local)
            variants["pack"] = pack_files(args.emulator, game["region"], Path(scratch))
            for label in args.variants or VARIANTS:
                argv = [sys.executable, str(ROOT / "scripts" / "core_probe.py"), "run",
                        "--core", str(core), "--content", str(load), "--content-label", game["name"],
                        "--frames", str(game.get("frames", 3600)),
                        "--frame-hash-every", str(game.get("frame_hash_every", 10)),
                        "--record", str(ROOT), "--emulator", args.emulator,
                        "--subject", subject(core.name.removesuffix("_libretro.so"), game["name"], label),
                        "--require-sandbox", "--timeout", str(args.timeout)]
                if url:
                    argv += ["--core-artifact", url]
                for dest, src in sorted(variants[label].items()):
                    argv += ["--system-file", f"{dest}={src}"]
                proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
                status = "recorded" if proc.returncode == 0 else f"exit {proc.returncode}"
                print(json.dumps({"game": game["name"], "variant": label, "status": status,
                                  "error": proc.stderr.strip()[-400:] if proc.returncode else None}),
                      flush=True)
                failures += proc.returncode != 0
    return 1 if failures else 0


def collect(root: Path = ROOT, stored_records: list | None = None) -> list[dict]:
    """Every region outcome the records under measurements/ hold. A game
    listed under several systems (sega-megacd and mega-cd) is judged under
    the one whose profile ran it."""
    from common import load_emulator_profiles  # noqa: PLC0415
    from measurement_records import live, read_records  # noqa: PLC0415

    content: dict[str, list[tuple[str, dict]]] = {}
    for system, items in load_content(root / "tests" / "campaign" / "content.yml").items():
        for item in items:
            content.setdefault(item["name"], []).append((system, item))
    profiles = load_emulator_profiles(str(root / "emulators"))
    groups: dict[tuple[str, str, str], dict[str, dict]] = {}
    for stored in read_records(root) if stored_records is None else stored_records:
        match = SUBJECT.match(stored.record.get("subject", ""))
        label = ((stored.record.get("inputs") or {}).get("content") or {}).get("label")
        if not match or label not in content:
            continue
        key = (stored.emulator, match["core"], label)
        groups.setdefault(key, {})[match["variant"]] = stored.record
    out = []
    for (emulator, core, game), records in sorted(groups.items()):
        records = {k: v for k, v in records.items() if v["id"] in {r["id"] for r in live(records.values())}}
        profile = profiles.get(emulator, {})
        system, item = next(((s, i) for s, i in content[game] if s in (profile.get("systems") or ())),
                            content[game][0])
        entries = region_entries(profile, system)
        result = outcome(records, entries, item["region"])
        out.append({"emulator": emulator, "core": core, "system": system, "game": game, **result,
                    "sentence": sentence(item["region"], result)})
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    go = sub.add_parser("run", help="the four runs of each region game, recorded")
    go.add_argument("--emulator", required=True, help="the profile, e.g. beetle_psx")
    go.add_argument("--system", required=True, help="the system, e.g. sony-playstation")
    go.add_argument("--core-name", help="buildbot core name, e.g. mednafen_psx (downloads the nightly)")
    go.add_argument("--core", help="a core .so to use instead of the nightly")
    go.add_argument("--core-artifact", help="where --core was downloaded from")
    go.add_argument("--content-dir", default=str(ROOT / ".cache" / "test-content"))
    go.add_argument("--variants", nargs="*", choices=VARIANTS, help="a subset of the four runs")
    go.add_argument("--timeout", type=int, default=900, help="seconds per run")
    sub.add_parser("report", help="the outcomes the records hold, as JSON")
    args = parser.parse_args(argv)
    if args.command == "run":
        if not (args.core or args.core_name):
            parser.error("run needs --core-name or --core")
        return run(args)
    print(json.dumps(collect(), indent=2))
    return 0


if __name__ == "__main__":
    from common import run_cli

    run_cli(main)
