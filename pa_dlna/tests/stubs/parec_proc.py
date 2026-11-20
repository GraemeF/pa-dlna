"""The parec process used by the tests."""

import sys
import io
import socket

from .streams import handle_first_command

BLKSIZE = 2 ** 12   # 4096
ADEN_ARABIE = (b"J'avais vingt ans. Je ne laisserai personne dire que c'est"
               b' le plus bel age de la vie.')
STDOUT_FILENO = 1

def get_blk():
    """Return BLKSIZE bytes."""

    hunk = ADEN_ARABIE * (BLKSIZE // len(ADEN_ARABIE) + 1)
    hunk = hunk[:BLKSIZE]
    assert len(hunk) == BLKSIZE
    return hunk

def parec():
    return_code = 0
    hunk = get_blk()
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))

    try:
        socket_path = sys.argv[1]
        if socket_path == 'None':
            # Write BLKSIZE bytes forever to the encoder process.
            while True:
                stdout.write(hunk)

        else:
            # Write 'bcount' times BLKSIZE bytes to the Track instance.
            # There is no encoder process.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(socket_path)

                return_code, do_sleep = handle_first_command(sock)

                while True:
                    # Get the 'count' value.
                    bcount = sock.recv(1024)
                    if not bcount:
                        return
                    count = int(bcount)
                    assert count % BLKSIZE == 0

                    # Write 'count' bytes.
                    for i in range(count // BLKSIZE):
                        stdout.write(hunk)
                    stdout.flush()

                    # Write the 'count' value.
                    sock.sendall(bcount)

    except Exception as e:
        return_code = 1
    finally:
        stdout.close()

    return return_code

if __name__ == '__main__':
    sys.exit(parec())
