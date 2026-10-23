#!/bin/bash
# HELP: Checks the BIOS folder and installs the missing BIOS files
# ICON: retrobios
# GRID: RetroBIOS

# RetroBIOS for muOS, from Applications: the installer's full-screen
# interface in muxterm, the terminal muOS's own Terminal core runs, with
# PortMaster's gptokeyb2 adding the keys muxterm leaves unmapped. Without
# muxterm it runs on the console with the full mapping.

MUOS="${RB_MUOS:-/opt/muos}"
# shellcheck source=/dev/null
. "$MUOS/script/var/retro.sh"
SETUP_APP "retrobios" ""

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
ROM_MOUNT="$(GET_VAR "device" "storage/rom/mount")"
PM_DIR="$ROM_MOUNT/MUOS/PortMaster"
MUXTERM="$MUOS/frontend/muxterm"

RB_DIR="$APP_DIR"
RB_PACK_PATH="$APP_DIR:$ROM_MOUNT"
RB_LOG="$APP_DIR/log.txt"
# PortMaster keeps its runtimes here: the Python runtime is used when
# PortMaster has downloaded it.
controlfolder="$PM_DIR"

# shellcheck source=../shared/tui.txt
source "$APP_DIR/tui.txt"
: > "$RB_LOG"

if [ -x "$PM_DIR/gptokeyb2" ]; then
  # As muOS's own launchers start it (script/launch/ext-xroar.sh).
  ln -sf "$PM_DIR/libinterpose.aarch64.so" "/usr/lib/libinterpose.aarch64.so" >/dev/null 2>&1
  RB_MAPPER="$PM_DIR/gptokeyb2"
fi
if [ -x "$MUXTERM" ]; then
  RB_ROUTES="app"
  RB_TERMAPP=("$MUXTERM" --gl --)
  RB_GPTK="$APP_DIR/muxterm.gptk"
else
  RB_ROUTES="vt"
  RB_CONSOLE="${RB_CONSOLE:-/dev/tty0}"
  RB_GPTK="$APP_DIR/retrobios.gptk"
fi

rb_select_python
# A launcher's own arguments are not install.py's (muOS passes the app folder).
# shellcheck disable=SC2119
rb_launch
status=$?
rb_release_python
exit $status
