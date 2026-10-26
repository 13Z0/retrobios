# RetroBIOS for muOS

Copy `RetroBIOS.muxapp` to the card, open it from muOS's archive manager to
install it, then start **RetroBIOS** from **Applications**.

It checks the BIOS folder (`/run/muos/storage/bios`) against the files its
emulators read and installs the missing ones: downloaded with a network
connection, or from a pack copied to the root of the card without one.

It runs in muxterm, muOS's terminal. With PortMaster installed, gptokeyb2
turns every button into a key and muxterm is told to leave the pad alone, so
each button is sent once: D-pad to move, A or Start to choose, B or Select to
go back, X to tick a system, Y for the options. L1 and R1 do nothing in
muxterm, which keeps Page Up and Page Down for its own scrollback: Left and
Right on the D-pad page a list instead. Hold
Select, then press Start to quit. Without PortMaster, muxterm reads the pad
itself (Y then ticks a system) and Menu closes it.

With no button pressed on its first screen for 5 minutes, it closes by
itself: a pad the system cannot read then never leaves it stuck.

Not tried on a device yet: the icon, and whether muxterm honours SDL's
controller allow-list as SDL 2.30 does.
