#!/bin/sh
# run_gui.sh — start LinuxCNC with this GUI and restart it on request.
#
# The GUI is LinuxCNC's DISPLAY program, so when it exits the whole control
# shuts down. SYSTEM -> (OPRT) -> RESTRT saves any edits, touches the flag
# file named by GUI_RESTART_FLAG and quits; this loop sees the flag and
# starts linuxcnc again with the same ini. Without this launcher the RESTRT
# key reports "NO LAUNCHER".
#
#   ./run_gui.sh                      # uses the default ini below
#   ./run_gui.sh /path/to/machine.ini
INI="${1:-$HOME/linuxcnc/configs/hmc-sim/hmc-sim.ini}"
FLAG="${GUI_RESTART_FLAG:-/tmp/linuxcnc-gui-restart}"
export GUI_RESTART_FLAG="$FLAG"

while :; do
    rm -f "$FLAG"
    linuxcnc "$INI"
    [ -e "$FLAG" ] || break
    sleep 1                 # let the realtime side release before reloading
done
