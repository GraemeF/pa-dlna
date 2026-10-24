"""The encoder process used by the tests."""

import sys
import io
import socket
import time

from .streams import handle_first_command

STDIN_FILENO = 0
STDOUT_FILENO = 1

def encoder():
    """Write for ever 'count' bytes read from stdin to stdout."""

    stdin = io.BufferedReader(io.FileIO(STDIN_FILENO, mode='r'))
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))
    return_code = 0

    try:
        socket_path = sys.argv[1]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(socket_path)

            return_code, do_sleep = handle_first_command(sock)

            while True:
                # Get the 'count' value.
                bcount = sock.recv(1024)
                if not bcount:
                    break
                count = int(bcount)

                # Write 'count' bytes.
                data = stdin.read(count)
                stdout.write(data)
                stdout.flush()

                # Write the 'count' value.
                sock.sendall(bcount)

            # Sleep to let the Track.run() task be cancelled by Track.stop().
            if do_sleep:
                time.sleep(10)

    except Exception as e:
        return_code = 1
    finally:
        stdin.close()
        stdout.close()
        print(f'encoder stub return_code: {return_code}', file=sys.stderr)

    return return_code

if __name__ == '__main__':
    sys.exit(encoder())
