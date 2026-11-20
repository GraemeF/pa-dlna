#! /bin/env python

import sys
from subprocess import *

# Loop over executing a test to find the random occurence of SEARCH_FOR.
# For example run:
#   $ ./tools/loop_cmd.py python -m unittest -v -k test_play_track_l1

COUNT = 30
SEARCH_FOR = 'ResourceWarning'

def main():
    for i in range(COUNT):
        proc = run(sys.argv[1:], stdout=PIPE, stderr=STDOUT, check=True)

        stdout = proc.stdout.decode()
        print(stdout)
        print('Iteration:', i+1)

        if SEARCH_FOR in stdout:
            print(f"Got '{SEARCH_FOR}' at iteration {i+1}")
            break

if __name__ == '__main__':
    sys.exit(main())
