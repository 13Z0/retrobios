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

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check
```

Read the count line:

```
  1/109 present (0 verified, 1 wrong hash)
```

- **`wrong hash`**: a file is there, but its contents are wrong. It may be for
  another region or model, or damaged.
- **Fewer present than the total**: some files are missing.
- **`0 wrong hash` and all present**: your files are fine. Go to step 3.

## 2. Fix the files

Run the same command without `--check`. It adds the missing files and replaces
the wrong ones. It keeps the old ones in `retrobios-backup`, beside your BIOS
folder.

## 3. Check the folder and the names

- **Right folder?** Your emulator must read the folder the command used.
  [Your setup's page](../which-pack.md) names it.
- **Is the name exact?** `scph5501.bin` and `SCPH-5501.BIN` are two different
  names. On Linux, capital letters count too.
- **Unzipped by hand on Linux?** Use the command. Some emulators there need a
  lower-case name the ZIP lacks.
- **A ZIP in parts will not open?** Download every part, like
  `Pack.part1of2.zip` and `Pack.part2of2.zip`. Unzip each one into the same
  folder.

## One console only

Add `--system` and its name:

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --check --system sony-playstation --verbose
```

`--verbose` names each missing or wrong file. `--list-systems` shows every
console name.

## My setup says UNTESTED

Batocera shows `UNTESTED` for a file whose contents it does not expect. Run
the command without `--check` to replace it.

## If the command fails { #installation-script-fails }

- **Network error:** check that github.com opens, then try again.
- **Permission denied:** on Batocera, Recalbox and ROCKNIX, put `sudo` and a
  space before the command. It then runs as the administrator.
- **No setup found:** name it yourself, with your BIOS folder:

```sh
curl -fsSL https://raw.githubusercontent.com/Abdess/retrobios/main/install.sh | sh -s -- --platform retroarch --dest ~/.config/retroarch/system
```

Other messages: Details: [When something goes wrong](installer.md#when-something-goes-wrong).

<span id="hash-mismatch-untested"></span><span id="pack-is-missing-files"></span><span id="verifypy-reports-errors"></span>

:::tip[Next]
- [Your setup's page](../which-pack.md): its folder and how it checks files
- [FAQ](faq.md)
:::

