#!/bin/bash
# HELP: Checks the BIOS folder and installs the missing BIOS files
# ICON: retrobios
# GRID: RetroBIOS

# RetroBIOS for muOS, from Applications: the installer's full-screen
# interface in muxterm, the terminal muOS's own Terminal core runs, with
# PortMaster's gptokeyb2 turning the buttons into keys. Without muxterm it
# runs on the console.

MUOS="${RB_MUOS:-/opt/muos}"
# shellcheck source=/dev/null
. "$MUOS/script/var/retro.sh"
SETUP_APP "retrobios" ""

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ROM_MOUNT="$(GET_VAR "device" "storage/rom/mount")"
PM_DIR="$ROM_MOUNT/MUOS/PortMaster"
MUXTERM="$MUOS/frontend/muxterm"

RB_DIR="$APP_DIR"
RB_LOG="$APP_DIR/log.txt"
# muOS binds MUOS/bios from SD2 when that card has one, else from SD1, onto
# /run/muos/storage/bios (script/device/bind.sh:10-11,30-52): the other card's
# MUOS/bios is the one offered to copy from.
SD2_MOUNT="$(GET_VAR "device" "storage/sdcard/mount")"
RB_PACK_PATH="$APP_DIR:$ROM_MOUNT:$SD2_MOUNT"
RB_OTHER_CARDS="$ROM_MOUNT/MUOS/bios:$SD2_MOUNT/MUOS/bios"
RB_BIOS_DEST="${RB_BIOS_DEST:-/run/muos/storage/bios}"
# PortMaster keeps its runtimes here: the Python runtime is used when
# PortMaster has downloaded it.
controlfolder="$PM_DIR"

# shellcheck source=../shared/tui.txt
source "$APP_DIR/tui.txt"
rb_open_log fresh

RB_GPTK="$APP_DIR/retrobios.gptk"
if [ -x "$PM_DIR/gptokeyb2" ]; then
  # As muOS's own launchers start it (script/launch/ext-xroar.sh). gptokeyb2
  # kills only with SIGKILL: by process name by default, which never names
  # this run, and quits all the same, or with -X by command line, which is
  # what reaches the Start + Select sentinel (gptokeyb2 src/util.c).
  ln -sf "$PM_DIR/libinterpose.aarch64.so" "/usr/lib/libinterpose.aarch64.so" >/dev/null 2>&1
  RB_MAPPER="$PM_DIR/gptokeyb2 -X"
fi
if [ -x "$MUXTERM" ]; then
  RB_ROUTES="app"
  if [ -n "${RB_MAPPER:-}" ]; then
    # muxterm reads the pad itself and sends Y as Space (MustardOS/terminal
    # input.c), where the interface reads Y as Options. The SDL hint allows
    # only vendor and product 0001:0001, which no pad carries, so muxterm
    # leaves the pad to gptokeyb2 and every button is sent once.
    RB_TERMAPP=(env SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x0001/0x0001 "$MUXTERM" --gl --)
  else
    # No gptokeyb2: muxterm's own buttons are all there is.
    RB_TERMAPP=("$MUXTERM" --gl --)
    RB_PAD="muxterm"
  fi
else
  RB_ROUTES="vt"
  RB_CONSOLE="${RB_CONSOLE:-/dev/tty0}"
fi

rb_select_python
# A launcher's own arguments are not install.py's (muOS passes the app folder).
# shellcheck disable=SC2119
rb_launch
status=$?
rb_release_python
exit $status
