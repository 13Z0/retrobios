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
- It starts a game and runs it for a set time.
- Each run starts fresh and runs at least twice.
- Each run gets one result, by comparing its screens with ones we checked by eye.

## What a result says

- **boots**: the game reached a screen it shows when it works.
- **bios_error**: the emulator said a BIOS is missing or wrong.
- **crash**: the emulator stopped.
- **hang**: the screen stayed blank, or the run never ended.
- **unknown**: none of these could be shown.
- **measured**: a speed test, given as time per frame.
- **no verdict**: an older test that only compared screens.

## How a file is proven

The game runs with no BIOS, with the BIOS alone, and with a wrong file under
its name: zeros, half the file, or another region's BIOS.

- **Required**: no BIOS fails, the BIOS alone boots.
- **Built-in stand-in**: no BIOS still boots.
- **Checked**: a wrong file is refused.

## When tests run

A maintainer starts a test run. A clear result updates the emulator's page and
names the test as proof. Anything else waits on the
[Measurements](../measurements.md) page for someone to read the code.

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
