#! /bin/bash
# Start pipewire and run the test suite.

export XDG_RUNTIME_DIR=/tmp
echo "Starting pipewire"
pipewire 2>/dev/null &

echo "Starting pipewire-pulse"
pipewire-pulse 2>/dev/null &


echo "Starting wireplumber"
wireplumber 2>/dev/null &

sleep 1
echo; echo "*** Running 'pw-cli help'"
pw-cli help

echo; echo "*** Running 'pactl info'"
pactl info

echo; echo "*** Running the test suite"
python3 -m unittest --verbose --failfast

echo; echo "*** Running coverage"
python3 -m coverage run --include=./* -m unittest
python3 -m coverage report --show-missing
