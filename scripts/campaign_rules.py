"""The campaign's fixed rule table: which observable settles which profile field.

Each rule is a pure function of a core probe record (or what was read from
one) and a profile entry. It returns verdicts. A verdict with `apply` set is
settled by the table and written into the profile with the record ids as
`evidence:`. Any other verdict goes to the review queue
(measurements/findings.jsonl).

  observable                                        field          settled
  boots to the game with no file                    hle_fallback   true
  no game screen without it, the game with it       required       true
                                                    hle_fallback   false
  opens the staged file under another path          path           that path
  opens the staged file under another name          aliases        name added
  a staged file the content should use, never read  -              flagged only

"Boots to the game" is read from the sampled frames: the run without the
file shows at least MIN_SHARED_SCREENS distinct screens that the run with
the file also shows, single-colour frames excluded. "No game screen" is a
run that did not load, or whose every sampled frame is a single colour. A
run that shows screens of its own that the reference never shows settles
nothing.

Standard library only.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

MIN_SHARED_SCREENS = 2
BOOTS = "boots"
FAILS = "fails"
INCONCLUSIVE = "inconclusive"

# A field an entry leaves out reads as this value.
FIELD_DEFAULTS: dict[str, object] = {"hle_fallback": False, "path": None, "aliases": []}
SETTLED_FIELDS = ("required", "hle_fallback", "path", "aliases")


@dataclass(frozen=True)
class Boot:
    """What the runs without the file showed, against the runs with it."""

    status: str
    detail: str
    shared: int = 0


@dataclass(frozen=True)
class Verdict:
    rule: str
    field: str
    value: object
    statement: str
    evidence: tuple[str, ...]
    apply: bool = False


@dataclass(frozen=True)
class Opened:
    """Where a run found the staged file opened."""

    dest: str
    record_id: str
    extra: dict = field(default_factory=dict)


def screens(repetition: dict) -> set[str]:
    """The distinct sampled frames of a repetition that are not one colour."""
    observables = repetition.get("observables") or {}
    uniform = set(observables.get("uniform_frames") or ())
    return {digest for _, digest in observables.get("frame_hashes") or () if digest not in uniform}


def variant(record: dict, label: str) -> dict:
    for item in record.get("variants") or ():
        if item.get("label") == label:
            return item
    raise KeyError(f"record {record.get('id')}: no variant {label!r}")


def boot_outcome(record: dict, absent_label: str, present_label: str) -> Boot:
    """Whether the core reached the game without the file.

    Every repetition must agree: one that boots and one that does not leave
    the question open.
    """
    present = variant(record, present_label)["repetitions"]
    if any(not rep.get("loaded") for rep in present):
        return Boot(INCONCLUSIVE, "with the file, the core did not load the content")
    reference: set[str] = set()
    for rep in present:
        shown = screens(rep)
        if not shown:
            return Boot(INCONCLUSIVE, "with the file, the core showed no game screen")
        reference |= shown
    outcomes: list[str] = []
    shared_counts: list[int] = []
    for rep in variant(record, absent_label)["repetitions"]:
        if not rep.get("loaded"):
            outcomes.append(FAILS)
            continue
        shown = screens(rep)
        if not shown:
            outcomes.append(FAILS)
            continue
        shared = len(shown & reference)
        shared_counts.append(shared)
        outcomes.append(BOOTS if shared >= MIN_SHARED_SCREENS else INCONCLUSIVE)
    shared = min(shared_counts) if shared_counts else 0
    if all(o == BOOTS for o in outcomes):
        return Boot(BOOTS, f"without the file, every run showed {shared} or more screens the "
                           f"run with the file shows", shared)
    if all(o == FAILS for o in outcomes):
        return Boot(FAILS, "without the file, no run loaded the content or showed more than "
                           "one colour, while the run with the file reached the game")
    return Boot(INCONCLUSIVE, "without the file, the runs showed screens the run with the file "
                              "never shows, or disagreed with each other", shared)


def current(entry: dict, name: str) -> object:
    if name in entry:
        return entry[name]
    return FIELD_DEFAULTS.get(name)


def agrees(entry: dict, verdict: Verdict) -> bool:
    """True when the profile already says what the run showed."""
    if verdict.field == "aliases":
        return set(verdict.value or ()) <= set(entry.get("aliases") or ())
    return current(entry, verdict.field) == verdict.value


def rule_bios_need(boot: Boot, entry: dict, record_id: str) -> list[Verdict]:
    """Boot with no file against boot with the declared file."""
    name = entry["name"]
    evidence = (record_id,)
    if boot.status == BOOTS:
        out = [Verdict("boots_without_file", "hle_fallback", True,
                       f"the core reaches the game without {name}: {boot.detail}", evidence, True)]
        if entry.get("required") is True:
            out.append(Verdict(
                "boots_without_file", "required", False,
                f"the profile says {name} is required, and the core reaches the game "
                f"without it. The table settles hle_fallback from this run, not required",
                evidence))
        return out
    if boot.status == FAILS:
        return [
            Verdict("fails_without_file", "required", True,
                    f"the core reaches the game only with {name}: {boot.detail}", evidence, True),
            Verdict("fails_without_file", "hle_fallback", False,
                    f"no stand-in for {name}: {boot.detail}", evidence, True),
        ]
    return [Verdict("inconclusive", "hle_fallback", None,
                    f"the runs with and without {name} settle nothing: {boot.detail}", evidence)]


def rule_path(entry: dict, declared: str, opened: Opened) -> Verdict | None:
    """The core opened the file at another place under the system directory."""
    if opened.dest == declared:
        return None
    return Verdict("opened_under_other_path", "path", opened.dest,
                   f"the core opened {entry['name']} as {opened.dest}, the profile declares "
                   f"{declared}", (opened.record_id,), True)


def rule_alias(entry: dict, opened: Opened) -> Verdict | None:
    """The core opened the file under a name the entry does not declare."""
    name = opened.dest.rsplit("/", 1)[-1]
    known = [entry["name"], *(entry.get("aliases") or ())]
    if name in known:
        return None
    return Verdict("opened_other_name", "aliases", [*(entry.get("aliases") or ()), name],
                   f"the core opened {entry['name']} under the name {name}",
                   (opened.record_id,), True)


def rule_never_opened(entry: dict, record_ids: Sequence[str], content: str) -> Verdict:
    return Verdict("never_opened", "opened", False,
                   f"{entry['name']} was staged and the core never opened it while running "
                   f"{content}", tuple(record_ids))


def rule_variants(status: str, labels: Iterable[str], entry: dict, record_id: str) -> Verdict | None:
    """Copies of one file that change what the core shows: a pack choice to review."""
    if status != "differs":
        return None
    return Verdict("variant_changes_output", "variants", sorted(labels),
                   f"the copies of {entry['name']} change what the core shows",
                   (record_id,))


def merge_evidence(existing: dict | None, verdict: Verdict) -> dict:
    """The entry's evidence: map with the verdict's record ids added under its field."""
    out = {key: list(value) for key, value in (existing or {}).items()}
    ids = out.setdefault(verdict.field, [])
    for rid in verdict.evidence:
        if rid not in ids:
            ids.append(rid)
    return {key: out[key] for key in sorted(out)}
