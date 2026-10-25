#! /bin/bash
# Start pipewire-pulse and run the test suite.

export XDG_RUNTIME_DIR=/tmp
pipewire 2>/dev/null &
echo "*** pipewire started"

pipewire-pulse 2>/dev/null &
echo "*** pipewire-pulse started"

wireplumber 2>/dev/null &
echo "*** wireplumber started"

sleep 1
pw-cli help
pactl info
python -m unittest --verbose --failfast
