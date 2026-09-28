#!/bin/sh
# run_gui.sh — start LinuxCNC with this GUI and restart it on request.
#
# The GUI is LinuxCNC's DISPLAY program, so when it exits the whole control
# shuts down. SYSTEM -> (OPRT) -> RESTRT saves any edits, touches the flag
# file named by GUI_RESTART_FLAG and quits; this loop sees the flag and
# starts linuxcnc again with the same ini. Without this launcher the RESTRT
# key reports "NO LAUNCHER".
#
#   ./run_gui.sh /path/to/machine.ini
if [ $# -ne 1 ] || [ ! -f "$1" ]; then
    echo "usage: $0 /path/to/machine.ini" >&2
    exit 2
fi
INI="$1"
FLAG="${GUI_RESTART_FLAG:-${XDG_RUNTIME_DIR:-/tmp}/linuxcnc-gui-restart}"
export GUI_RESTART_FLAG="$FLAG"

while :; do
    rm -f "$FLAG"
    linuxcnc "$INI"
    [ -e "$FLAG" ] || break
    sleep 1                 # let the realtime side release before reloading
done
