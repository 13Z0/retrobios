## Notes

RetroBIOS runs the RetroBIOS installer (`install.py`, from
[github.com/Abdess/retrobios](https://github.com/Abdess/retrobios)) on the
device. The installer finds the BIOS folder of the system, checks every file
its emulators read against its hash, and downloads the missing ones. The
screens are PortMaster's own.

Needs a network connection. Python 3.8 or newer: the system's, or
PortMaster's `python_3.11` runtime on aarch64, downloaded on first use.

## Controls

| Button | Action |
|--|--|
| D-Pad | Move in a list |
| A | Choose |
| B | Back, on PortMaster releases that read it (every list also has a Back or Quit entry) |
