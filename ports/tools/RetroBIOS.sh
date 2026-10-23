#!/bin/bash
# RetroBIOS from the Tools menu of ArkOS, dArkOS, ROCKNIX, JELOS and
# AmberELEC: checks the BIOS folder and downloads the files it lacks, in the
# installer's full-screen interface, each firmware the way its own tools do.
#
#   ArkOS, dArkOS   console 1 with a Terminus font, /opt/inttools/gptokeyb
#                   (dArkOS_Tools/Wifi.sh)
#   AmberELEC       /dev/console, TERM=linux, the system gptokeyb
#                   (modules/SetTime.sh)
#   ROCKNIX, JELOS  the foot or weston terminal the Tools menu opens, then a
#                   graphical terminal or the console, with gptokeyb

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${RB_ROOT:-}"
RB_DIR="$HERE/retrobios"
RB_LOG="$RB_DIR/log.txt"
ESUDO=""
OS_NAME=""
if [ -f "$ROOT/etc/os-release" ]; then
  OS_NAME=$(awk -F= '$1 == "NAME" { gsub(/"/, "", $2); print $2 }' "$ROOT/etc/os-release")
fi

rb_gptokeyb() {
  local candidate
  for candidate in "$@"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  command -v gptokeyb 2>/dev/null
}

if [ -d "$ROOT/opt/system" ] && [ -d "$ROOT/opt/inttools" ]; then
  # ArkOS and dArkOS run EmulationStation as a user with sudo.
  if sudo -n true >/dev/null 2>&1; then
    ESUDO="sudo"
  fi
  RB_ROUTES="vt"
  RB_CONSOLE="${RB_CONSOLE:-/dev/tty1}"
  RB_PACK_PATH="$RB_DIR:$HERE:/roms:/roms2"
  # The second card is /roms2, and "Switch to SD2 for Roms" moves RetroArch's
  # system folder there (dArkOS scripts/Switch to SD2 for Roms.sh:91-92): the
  # folder RetroArch reads is the destination, the other card's is offered.
  RB_OTHER_CARDS="${RB_OTHER_CARDS:-/roms/bios:/roms2/bios}"
  RB_BIOS_DEST=$(awk -F'"' '/^system_directory *=/ { print $2; exit }' \
    "$ROOT/home/ark/.config/retroarch/retroarch.cfg" 2>/dev/null)
  controlfolder="$ROOT/opt/system/Tools/PortMaster"
  mapper=$(rb_gptokeyb "$ROOT/opt/inttools/gptokeyb" "$controlfolder/gptokeyb")
  [ -f "$ROOT/opt/inttools/gamecontrollerdb.txt" ] \
    && export SDL_GAMECONTROLLERCONFIG_FILE="$ROOT/opt/inttools/gamecontrollerdb.txt"
elif [ "$OS_NAME" = "AmberELEC" ]; then
  RB_ROUTES="vt"
  RB_CONSOLE="${RB_CONSOLE:-/dev/console}"
  RB_PACK_PATH="$RB_DIR:$HERE:/storage/roms"
  controlfolder="/storage/roms/ports/PortMaster"
  mapper=$(rb_gptokeyb "$ROOT/usr/bin/gptokeyb" "$controlfolder/gptokeyb")
  [ -f /storage/.config/SDL-GameControllerDB/gamecontrollerdb.txt ] \
    && export SDL_GAMECONTROLLERCONFIG_FILE="/storage/.config/SDL-GameControllerDB/gamecontrollerdb.txt"
else
  # ROCKNIX, JELOS and the other systems built like them: the Tools menu
  # runs this inside foot on sway builds, so its own terminal comes first.
  RB_ROUTES="here app vt"
  RB_CONSOLE="${RB_CONSOLE:-/dev/tty1}"
  RB_PACK_PATH="$RB_DIR:$HERE:/storage/roms"
  controlfolder="/storage/roms/ports/PortMaster"
  mapper=$(rb_gptokeyb "$ROOT/usr/bin/gptokeyb" "$controlfolder/gptokeyb")
fi
# Kill mode on the installer's own path: Start + Select closes the
# interface, never another program.
RB_MAPPER=""
[ -n "$mapper" ] && RB_MAPPER="$ESUDO $mapper -1"

# shellcheck source=../shared/tui.txt
source "$RB_DIR/tui.txt"
: > "$RB_LOG"
rb_select_python
# A launcher's own arguments are not install.py's (muOS passes the app folder).
# shellcheck disable=SC2119
rb_launch
status=$?
rb_release_python
exit $status
