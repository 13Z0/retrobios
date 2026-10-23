# RetroBIOS for muOS

Copy `RetroBIOS.muxapp` to the card, open it from muOS's archive manager to
install it, then start **RetroBIOS** from **Applications**.

It checks the BIOS folder (`/run/muos/storage/bios`) against the files its
emulators read and installs the missing ones: downloaded with a network
connection, or from a pack copied to the root of the card without one.

It runs in muxterm, muOS's terminal. muxterm already sends the D-pad as
arrows, Start as Enter, B as Backspace (back) and Y as Space (tick). With
PortMaster installed, gptokeyb2 adds A (choose), X (tick) and L1/R1 (page).
Menu closes it.

Not tried on a device yet: the icon, and whether muxterm and gptokeyb2 share
the pad as described.
