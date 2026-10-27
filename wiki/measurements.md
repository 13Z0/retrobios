---
level: reference
order: 70
group: Data
terms: measurement, benchmark, tested, core probe, real conditions, method
---

# How emulators are tested

Some pages say what an emulator does with a file. Where we ran it to check,
the emulator's page shows a **Tested** block. Every result is on the
[Measurements](../measurements.md) page.

## What a test does

- A script runs the emulator with no window and no sound.
- It starts a game or a disc and presses buttons at set frames.
- It runs again for each version of one file, such as a BIOS. Nothing else changes.
- It compares what the emulator showed, played or saved, by fingerprint (a hash).
- Each version runs at least twice, each time from a fresh start.
- The tests run by themselves each night on an idle computer. A clear result
  updates the emulator's page and names the test as proof. Anything else waits
  on the [Measurements](../measurements.md) page for someone to read the code.

## What a result says

- **same**: the file changed nothing that was compared.
- **differs**: some versions of the file changed it.
- **inconclusive**: two runs of one version did not match, so no answer is given.
- **measured**: a speed test, given as time per frame.

## What a result keeps

Each result names its method, the computer it ran on and the exact build of
the emulator. Its inputs are kept by fingerprint. A result holds for that
build and those inputs only.

A speed figure depends on the computer and how busy it was. It is never
shown without them.

## Where results are kept

Each result is a full record in the
[`measurements/`](https://github.com/Abdess/retrobios/tree/main/measurements)
folder of this project. A record is never changed once added. A new run is
added beside it, and a correction names the record it replaces.

The results so far ran on these computers:

<!-- measurements-hosts -->
