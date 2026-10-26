#!/bin/bash
# RetroBIOS: checks the BIOS folder of this system and downloads the files
# it lacks, in the installer's full-screen interface on the console, the way
# PortMaster's Moonlight and Rlvm ports draw dialog there.

XDG_DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

if [ -d "/opt/system/Tools/PortMaster/" ]; then
  controlfolder="/opt/system/Tools/PortMaster"
elif [ -d "/opt/tools/PortMaster/" ]; then
  controlfolder="/opt/tools/PortMaster"
elif [ -d "$XDG_DATA_HOME/PortMaster/" ]; then
  controlfolder="$XDG_DATA_HOME/PortMaster"
else
  controlfolder="/roms/ports/PortMaster"
fi

# shellcheck source=/dev/null
source "$controlfolder/control.txt"
# shellcheck source=/dev/null
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"
get_controls

# Set by control.txt.
: "${directory:?}" "${GPTOKEYB:=}" "${sdl_controllerconfig:=}"

GAMEDIR="/$directory/ports/retrobios"
cd "$GAMEDIR" || exit 1

# Appended to, as the launcher and install.py also write this file: a tee
# writing from its own offset would overwrite their lines. The previous run's
# is kept, for a player who starts again before reading it.
[ -s "$GAMEDIR/log.txt" ] && mv -f "$GAMEDIR/log.txt" "$GAMEDIR/log.prev.txt" 2>/dev/null
{ : > "$GAMEDIR/log.txt"; } 2>/dev/null && exec > >(tee -a "$GAMEDIR/log.txt") 2>&1

# PortMasterDialog.txt has shipped with PortMaster since before 2024.02.26,
# and the 8.5.23 copy JELOS and KOLOS preinstall has no funcs.txt to source
# it: it is sourced here. It downloads the Python runtime when one is
# needed, and draws the fallback screens.
if [ ! -f "$controlfolder/PortMasterDialog.txt" ]; then
  echo "RetroBIOS needs PortMaster 2024.02.26 or newer: update PortMaster, then start RetroBIOS again."
  exit 1
fi
# shellcheck source=/dev/null
source "$controlfolder/PortMasterDialog.txt"

RB_DIR="$GAMEDIR"
RB_ROUTES="app vt"
RB_CONSOLE="${CUR_TTY:-/dev/tty0}"
RB_MAPPER="$GPTOKEYB"
RB_FALLBACK=1
RB_PACK_PATH="$GAMEDIR:/$directory/ports:/$directory"
# ArkOS and dArkOS, as control.txt tells them apart (its plymouth theme):
# their two card folders are offered to each other, and the destination is
# the one RetroArch reads. control.txt names the card PortMaster runs from
# roms2 whenever fstab mounts a second card, even when RetroArch still reads
# /roms/bios, so RetroArch's own setting decides, as in the Tools entry.
if [ -e "${RB_ARK_MARKER:-/usr/share/plymouth/themes/text.plymouth}" ]; then
  RB_OTHER_CARDS="${RB_OTHER_CARDS:-/roms/bios:/roms2/bios}"
  RB_BIOS_DEST=$(awk -F'"' '/^system_directory *=/ { print $2; exit }' \
    "${RB_RETROARCH_CFG:-/home/ark/.config/retroarch/retroarch.cfg}" 2>/dev/null)
  RB_BIOS_DEST="${RB_BIOS_DEST:-/$directory/bios}"
  # The font ArkOS's own tools put back on leaving (dArkOS_Tools/Wifi.sh).
  RB_FONT_DEFAULT="Lat7-Terminus20x10"
fi
# shellcheck source=../shared/tui.txt
source "$GAMEDIR/tui.txt"
# shellcheck source=retrobios/screens.txt
source "$GAMEDIR/screens.txt"
rb_open_log keep

# muOS: PortMaster's mod_muOS.txt points CUR_TTY at /dev/null, so there is no
# console to draw on. muxterm, the terminal muOS's Terminal core runs, shows
# the interface instead, told to leave the pad to gptokeyb (rb_muxterm).
RB_MUXTERM="${RB_MUXTERM:-/opt/muos/frontend/muxterm}"
if [ -x "$RB_MUXTERM" ] && [ -n "$RB_MAPPER" ]; then
  rb_muxterm "$RB_MUXTERM"
fi

export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"

rb_select_python
# A launcher's own arguments are not install.py's (muOS passes the app folder).
# shellcheck disable=SC2119
rb_launch
status=$?
if [ "$status" = "125" ]; then
  # No console or terminal app shows the interface here: PortMaster's own
  # screens do.
  rb_main
  status=$?
fi
rb_release_python

if declare -F pm_finish > /dev/null; then
  pm_finish
fi
exit $status
