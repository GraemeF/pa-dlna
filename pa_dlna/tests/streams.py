"""The parec.py (or encoder.py) script used for testing."""

import sys
import os
import io
import asyncio
import socket
import time
import tempfile
import contextlib
from unittest import mock

from . import TEST_TIMEOUT
from .libpulse import use_libpulse_stubs
from ..http_server import HTTPServer
from ..encoders import select_encoder, Encoder
from ..config import UserConfig

BLKSIZE = 2 ** 12   # 4096
ADEN_ARABIE = (b"J'avais vingt ans. Je ne laisserai personne dire que c'est"
               b' le plus bel age de la vie.')
PAREC_PATH_ENV = 'PA_DLNA_PAREC_PATH'
ENCODER_PATH_ENV = 'PA_DLNA_ENCODER_PATH'
STDIN_FILENO = 0
STDOUT_FILENO = 1

# Use the patched pulseaudio and pa_dlna modules to avoid importing libpulse
# that is not required for running the test.
with use_libpulse_stubs(['pa_dlna.pulseaudio', 'pa_dlna.pa_dlna']) as modules:
    pulseaudio, pa_dlna = modules

@contextlib.contextmanager
def unix_socket_path(socket_path_env):
    path = tempfile.mktemp(prefix="test_http_", suffix='.sock',
                           dir=os.path.curdir)
    path = os.path.abspath(path)
    with mock.patch.dict('os.environ', {socket_path_env: path}):
        yield path
        try:
            os.unlink(path)
        except OSError:
            pass

async def run_curl(url, http_version='http1.1', extra_args=None):
    curl_cmd = ['curl', '--silent', '--show-error', f'--{http_version}']
    if extra_args is not None:
        curl_cmd.extend(extra_args)
    curl_cmd.append(url)
    proc = await asyncio.create_subprocess_exec(*curl_cmd,
                                          stdin=asyncio.subprocess.DEVNULL,
                                          stdout=asyncio.subprocess.PIPE,
                                          stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if stderr and 0:
        print(f'CURL stderr: {stderr.decode().strip()}')
    return proc.returncode, len(stdout.decode())

async def new_renderer(mime_type):
    renderer = Renderer(ControlPoint(), mime_type)
    await renderer.setup()
    return renderer

def set_control_point(control_point):
    control_point.parec_cmd = [sys.executable, '-m', 'pa_dlna.tests.parec']

    # The following patches do:
    #  - Make encoders available whether they are installed or not.
    #  - Ignore the local pa_dlna.conf when it exists.
    with mock.patch.object(Encoder, 'available') as available,\
            mock.patch('builtins.open', mock.mock_open()) as m_open:
        available.return_value = True
        m_open.side_effect = FileNotFoundError()
        control_point.config = UserConfig()

async def play_track(mime_type, transactions, wait_for_completion=True,
                     logs=None):
    if wait_for_completion:
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
    else:
        completed = None
    env_path = PAREC_PATH_ENV if 'l16' in mime_type else ENCODER_PATH_ENV

    with unix_socket_path(env_path) as sock_path:
        renderer = await new_renderer(mime_type)

        # Start the http server.
        control_point = renderer.control_point
        http_server = _HTTPServer(control_point,
                                  renderer.root_device.local_ipaddress,
                                  control_point.port)
        http_server.allow_from(renderer.root_device.peer_ipaddress)
        http_server_t = asyncio.create_task(http_server.run(),
                                            name='http_server')

        # Start the AF_UNIX socket server.
        server = UnixSocketServer(sock_path, transactions, completed)
        server_t = asyncio.create_task(server.run(), name='socket server')

        # Start curl.
        await http_server.startup
        await server.ready_fut
        curl_task = asyncio.create_task(run_curl(renderer.current_uri),
                                        name='curl')

        # Wait for the last chunk of data to be written to the pipe read by
        # Track.write_track().
        if completed is not None:
            try:
                await asyncio.wait_for(completed, timeout=TEST_TIMEOUT)
            except TimeoutError:
                print(f'***** server.stage: {server.stage}', file=sys.stderr)
                print(f'***** http_server.stage: {http_server.stage}',
                      file=sys.stderr)
                if logs is not None:
                    print('\n'.join(l for l in logs.output if
                                    ':asyncio:' not in l), file=sys.stderr)
                raise

        return curl_task, renderer

class UnixSocketServer:
    """Accept connections on an AF_UNIX socket."""

    def __init__(self, path, transactions, completed):
        self.path = path
        self.transactions = transactions
        self.completed = completed
        self.stage = 'init'
        loop = asyncio.get_running_loop()
        self.ready_fut = loop.create_future()

    async def client_connected(self, reader, writer):
        """Handle request/expect transactions.

        The first element of 'transactions' is either:
            - 'ignore'
            - 'dont_sleep'
            - 'FFMpegEncoder'
            - an Exception class name
        The following elements are the number of bytes to write to stdout.
        """

        self.stage = 'connected'
        first = self.transactions[0]
        self.stage = 'before first command'
        assert (first in ('ignore', 'dont_sleep', 'FFMpegEncoder') or
                isinstance(eval(first + '()'), Exception))
        writer.write(first.encode())
        resp = await reader.read(1024)
        assert resp == b'Ok'

        self.stage = 'before count loop'
        for count in self.transactions[1:]:
            assert isinstance(count, int)
            self.stage = 'before count write'
            writer.write(str(count).encode())
            await writer.drain()

            self.stage = 'before count read'
            await reader.read(1024)

        self.stage = 'after count loop'
        try:
            writer.close()
            await writer.wait_closed()
        except ConnectionError:
            pass
        self.stage = 'end connection'
        if self.completed is not None:
            self.completed.set_result(True)

    async def run(self):
        try:
            aio_server = await asyncio.start_unix_server(
                                        self.client_connected, self.path)
            async with aio_server:
                self.ready_fut.set_result(True)
                await aio_server.serve_forever()
        except Exception as e:
            try:
                self.ready_fut.set_result(True)
            except asyncio.InvalidStateError:
                pass
            raise

class _HTTPServer(HTTPServer):
    def __init__(self, control_point, ip_address, port):
        super().__init__(control_point, ip_address, port)
        self.stage = 'init'

    async def client_connected(self, reader, writer):
        self.stage = 'connected'
        try:
            return await super().client_connected(reader, writer)
        finally:
            self.stage = 'end connection'

    async def run(self):
        try:
            aio_server = await asyncio.start_server(self.client_connected,
                                                self.ip_address, self.port)
            async with aio_server:
                self.startup.set_result(True)
                await aio_server.serve_forever()
        except Exception as e:
            try:
                self.startup.set_result(True)
            except asyncio.InvalidStateError:
                pass
            raise

class Sink:
    monitor_source_name = 'monitor source name'

class NullSink:
    sink = Sink()

class Renderer(pa_dlna.DLNATestDevice):
    def __init__(self, control_point, mime_type):
        super().__init__(control_point, mime_type)
        self.nullsink = NullSink()
        self.set_current_uri()

    async def setup(self):
        await self.select_encoder(self.root_device.udn)
        if self.encoder is not None:
            self.encoder.command = [sys.executable, '-m',
                                    'pa_dlna.tests.encoder']

    async def disable_root_device(self):
        pass

    async def close(self):
        pass

class ControlPoint(pa_dlna.AVControlPoint):
    def __init__(self):
        self.port = 8080
        self.root_devices = {}
        set_control_point(self)

    def abort(self, msg):
        pass

    async def close(self, msg=None):
        pass

### The parec_py and encoder_py functions. ###
def get_blk():
    """Return BLKSIZE bytes."""

    hunk = ADEN_ARABIE * (BLKSIZE // len(ADEN_ARABIE) + 1)
    hunk = hunk[:BLKSIZE]
    assert len(hunk) == BLKSIZE
    return hunk

def handle_first_command(sock):
    return_code = 0
    do_sleep = True
    resp = b'Ok'
    exception = None

    command = sock.recv(1024)
    command = command.decode()

    if command == 'ignore':
        pass
    elif command == 'dont_sleep':
        do_sleep = False
    elif command == 'FFMpegEncoder':
        return_code = 255
        do_sleep = False
    else:
        try:
            obj = eval(command + '()')
        except NameError:
            resp = b'NameError'
        else:
            if isinstance(obj, Exception):
                exception = obj
            else:
                resp = b'Unknown'
    sock.sendall(resp)

    if exception is not None:
        raise exception

    return return_code, do_sleep

def parec_py():
    print('parec stub starting', file=sys.stderr)
    return_code = 0
    hunk = get_blk()
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))

    try:
        socket_path = os.environ.get(PAREC_PATH_ENV)
        if socket_path is None:
            while True:
                stdout.write(hunk)

        else:
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
        print(f'parec stub error: {e!r}', file=sys.stderr)
        return_code = 1
    finally:
        stdout.close()

    return return_code

def encoder_py():
    """Write for ever 'count' bytes read from stdin to stdout."""

    print('encoder stub starting', file=sys.stderr)
    stdin = io.BufferedReader(io.FileIO(STDIN_FILENO, mode='r'))
    stdout = io.BufferedWriter(io.FileIO(STDOUT_FILENO, mode='w'))
    return_code = 0

    try:
        socket_path = os.environ[ENCODER_PATH_ENV]
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
        print(f'encoder stub error: {e!r}', file=sys.stderr)
        return_code = 1
    finally:
        stdin.close()
        stdout.close()
        print(f'encoder stub return_code: {return_code}', file=sys.stderr)

    return return_code

def main():
    processes = {
        'parec.py': parec_py,
        'encoder.py': encoder_py
        }

    proc_name = os.path.basename(sys.argv[0])
    return processes[proc_name]()

if __name__ == '__main__':
    sys.exit(main())
