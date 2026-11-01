#! /bin/bash
# Start pipewire-pulse and run the test suite.
#
# IMPORTANT:
#   Rebuild the docker image whenever there is a change in this file.

if [ "$DOCKER_ENV" = "local" ]; then \
        echo "Cloning pa-dlna from the current directory" && \
        cp -r /mnt/shared/ pa-dlna; \
    else \
        echo "Cloning pa-dlna from gitlab" && \
        git clone -b master --single-branch --depth 1 https://gitlab.com/xdegaye/pa-dlna.git; \
    fi

cd pa-dlna

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
python3 -m coverage report -m
