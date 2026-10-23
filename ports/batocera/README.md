# RetroBIOS for Batocera and Knulli

Copy `RetroBIOS.sh`, `RetroBIOS.sh.keys` and the `retrobios` folder into
`roms/ports` on the SHARE partition (`/userdata/roms/ports`), update the
game list, then start **RetroBIOS** from **Ports**.

It checks the BIOS folder (`/userdata/bios`) against the files its emulators
read and installs the missing ones: downloaded with a network connection,
or from a pack copied into `roms/ports/retrobios` without one.

The controller drives it through `RetroBIOS.sh.keys`: D-pad to move, A or
Start to choose, B or Select to go back, X to tick a system, Y for the
options, L1 and R1 to page. B on the first screen closes it.

## Not tried on a device yet

EmulationStation covers the text console on Batocera and Knulli, and no
terminal for a script is documented there. RetroBIOS opens xterm (the X11
builds) or foot when either is installed, else switches to console 4, runs
there, and switches back. Which of the two shows up on a given build is not
verified. If the screen stays black, `retrobios/log.txt` says which route
was taken, and the computer route always works: with the card mounted, run
the one-line command from abdess.github.io/retrobios/wiki/installer.
