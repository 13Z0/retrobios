#!/bin/bash
# RetroBIOS for Batocera and Knulli, started from Ports: checks the BIOS
# folder and downloads the files it lacks, in the installer's full-screen
# interface. Batocera gives the controller the keys of RetroBIOS.sh.keys
# (evmapy) while this runs.
#
# The interface needs a terminal the player can see, and EmulationStation
# covers the console. In order: xterm or foot in the graphical session, else
# console 4 switched to and back from. Console 4 because Batocera's exit
# hotkey sends Alt+F4 (batocera_launch emulators/sh.py), which on a console
# switches to console 4: there it leaves the interface where it is.

HERE="$(cd "$(dirname "$0")" && pwd)"
RB_DIR="$HERE/retrobios"
RB_ROUTES="app vt"
RB_VT_SWITCH="${RB_VT_SWITCH:-4}"
RB_PACK_PATH="$RB_DIR:$HERE:/userdata"
RB_LOG="$RB_DIR/log.txt"

# The X11 builds start EmulationStation on display :0, which a script run
# from it does not always inherit, with the cookie S31emulationstation
# exports: xterm is refused the display without it.
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] \
    && [ -S "${RB_X_SOCKET:-/tmp/.X11-unix/X0}" ]; then
  export DISPLAY=:0
fi
RB_XAUTH="${RB_XAUTH:-/var/lib/.Xauthority}"
if [ -n "${DISPLAY:-}" ] && [ -z "${XAUTHORITY:-}" ] && [ -f "$RB_XAUTH" ]; then
  export XAUTHORITY="$RB_XAUTH"
fi

# shellcheck source=../shared/tui.txt
source "$RB_DIR/tui.txt"
rb_open_log fresh
rb_select_python
# A launcher's own arguments are not install.py's (muOS passes the app folder).
# shellcheck disable=SC2119
rb_launch
status=$?
rb_release_python
exit $status
