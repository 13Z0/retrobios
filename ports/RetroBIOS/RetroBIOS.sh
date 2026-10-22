#!/bin/bash
# RetroBIOS: checks the BIOS folder of this system and downloads the files
# it lacks, with PortMaster's own screens.

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

source "$controlfolder/control.txt"
[ -f "${controlfolder}/mod_${CFW_NAME}.txt" ] && source "${controlfolder}/mod_${CFW_NAME}.txt"
get_controls

GAMEDIR="/$directory/ports/retrobios"
cd "$GAMEDIR" || exit 1

> "$GAMEDIR/log.txt" && exec > >(tee "$GAMEDIR/log.txt") 2>&1

# PortMasterDialog.txt has shipped with PortMaster since before 2024.02.26,
# and the 8.5.23 copy JELOS and KOLOS preinstall has no funcs.txt to source
# it: it is sourced here.
if [ ! -f "$controlfolder/PortMasterDialog.txt" ]; then
  echo "RetroBIOS needs PortMaster 2024.02.26 or newer: update PortMaster, then start RetroBIOS again."
  exit 1
fi
source "$controlfolder/PortMasterDialog.txt"
source "$GAMEDIR/screens.txt"

export SDL_GAMECONTROLLERCONFIG="$sdl_controllerconfig"

rb_main
status=$?

if declare -F pm_finish > /dev/null; then
  pm_finish
fi
exit $status
