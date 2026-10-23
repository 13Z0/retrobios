---
level: start
order: 40
---

# When a game does not start

Most often a BIOS file is missing or wrong.

## 1. Check my BIOS folder { #checking-what-you-installed }

<span id="game-wont-start-black-screen"></span><span id="my-setup-says-untested"></span>

Run the command with `--check`. It changes nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

Read the count line:

```
  1/109 present (0 verified, 1 wrong hash)
```

- **`wrong hash`**: another region or model, or damaged.
- **Fewer present than the total**: files are missing.
- **A line under a file**, like `still zipped`, says what to fix.
- **`0 wrong hash`, all present**: go to step 3.

## 2. Fix the files

Run it without `--check`. It adds missing files and replaces wrong ones.

## 3. Check the folder and the names

- **Right folder?** [Get your files](../which-pack.md) names it.
- **Exact name?** `scph5501.bin` and `SCPH-5501.BIN` differ. On Linux,
  capitals count too.
- **A `bios` folder inside your BIOS folder?** Run the installer: it copies
  those files up one level.
- **Unzipped by hand on Linux?** The command adds names that differ by
  capitals.
- **A ZIP in parts, like `Pack.part1of2.zip`?** Unzip every part into one
  folder.

## An arcade game does not start

It needs its BIOS set, like `neogeo.zip`, and a game set for the same emulator
version.

- Keep `neogeo.zip` zipped.
- A game set for another MAME version often fails.
  Details: [the version of each emulator](../emulators/index.md).
- Run the command once. It installs the BIOS sets your emulators read.

## The BIOS is fine, the game still fails

- **Another emulator?** Each reads its own files, listed on its page.
- **A BIOS setting?** Some emulators can skip the BIOS. In PCSX ReARMed, set
  **BIOS Selection** to **Auto**.
- **A disc in several files?** Open its `.cue` or `.m3u`, not a `.bin`.

## If the command fails { #installation-script-fails }

- **Network error:** try again later.
- **`The clock reads ...`:** set the right date.
- **Permission denied:** do what the message says. It names what to run.
- **No setup found:** name it and its folder:

<!-- os-tabs windows: --platform retroarch --dest C:\path\to\RetroArch\system -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest ~/.config/retroarch/system
```

Other messages: [When something goes wrong](installer.md#when-something-goes-wrong).

<span id="hash-mismatch-untested"></span><span id="pack-is-missing-files"></span><span id="verifypy-reports-errors"></span><span id="one-console-only"></span>

## Next steps

- [Get your files](../which-pack.md)
- [Common questions](faq.md)
