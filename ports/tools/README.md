# RetroBIOS in the Tools menu

Copy `RetroBIOS.sh` and the `retrobios` folder next to the other tools:

| System | Folder | Menu |
|--------|--------|------|
| ArkOS, dArkOS | `tools` on the EASYROMS partition (`/roms/tools`) | Options, Tools |
| ROCKNIX, JELOS | `/storage/.config/modules` | Tools |
| AmberELEC | `/storage/.config/distribution/modules` | Tools |

It checks the BIOS folder against the files its emulators read and installs
the missing ones: downloaded with a network connection, or from a pack copied
into the `retrobios` folder without one.

Controls: D-pad to move, A or Start to choose, B or Select to go back, X to
tick a system, Y for the options, L1 and R1 to page. To quit, hold Select, then press Start: Start pressed
first would also choose the line in focus.

ArkOS has Python 3.7, older than the installer needs: RetroBIOS uses the
Python runtime the RetroBIOS PortMaster port downloads, so start that port
once first.
