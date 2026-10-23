---
level: start
order: 40
---

# When a game does not start

Most of the time, a BIOS file is missing or is the wrong version. Three checks
find it.

## 1. Check your BIOS folder { #checking-what-you-installed }

<span id="game-wont-start-black-screen"></span>

Run the command with `--check`. It changes nothing.

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

Read the count line:

```
  1/109 present (0 verified, 1 wrong hash)
```

- **`wrong hash`**: a file is there with the wrong contents: another region
  or model, or damaged.
- **Fewer present than the total**: some files are missing.
- **A line under a file**, like `still zipped`: it says what to fix.
- **`0 wrong hash` and all present**: your files are fine. Go to step 3.

## 2. Fix the files

Run the same command without `--check`. It adds the missing files and replaces
the wrong ones, keeping the old ones.

## 3. Check the folder and the names

- **Right folder?** Your emulator must read the folder the command used.
  [Your setup's page](../which-pack.md) names it.
- **Is the name exact?** `scph5501.bin` and `SCPH-5501.BIN` are two different
  names. On Linux, capital letters count too.
- **A `bios` folder inside your BIOS folder?** That ZIP holds its own top
  folder. Move its files up one level.
- **Unzipped by hand on Linux?** Run the command once. It adds the names that
  differ only by capital letters, which some ZIPs leave out.
- **A ZIP in parts will not open?** Download every part, like
  `Pack.part1of2.zip` and `Pack.part2of2.zip`. Unzip each one into the same
  folder. `--check` names a part you missed.

## One console only

Add `--system` and its name:

<!-- os-tabs -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --system sony-playstation --verbose
```

`--verbose` names each missing or wrong file. `--list-systems` shows every
console name.

## My setup says UNTESTED

Batocera shows `UNTESTED` for a file with unexpected contents. Run the command
without `--check` to replace it.

## If the command fails { #installation-script-fails }

- **Network error:** check that github.com opens, then try again.
- **Permission denied:** do what the message says. It names what to run.
- **No setup found:** pick it from the list. Or name it and its folder:

<!-- os-tabs windows: --platform retroarch --dest C:\path\to\RetroArch\system -->
```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest ~/.config/retroarch/system
```

Other messages: [When something goes wrong](installer.md#when-something-goes-wrong).

<span id="hash-mismatch-untested"></span><span id="pack-is-missing-files"></span><span id="verifypy-reports-errors"></span>

## Next steps

- [Your setup's page](../which-pack.md): its folder and how it checks files
- [Common questions](faq.md)
