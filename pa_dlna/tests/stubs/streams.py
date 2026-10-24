"""Play a track.

Does not use the external libpulse package. Use instead the '.libpulse'
module by importing the '.pa_dlna' module of this 'stubs' package.
Those modules are intrumented so that the pa_dlna.http_server module is used
by play_track().

Use a DLNATestDevice instance as the renderer.

See the documentation of play_track() below.
."""

import sys
import os
import asyncio
import socket
import tempfile
import contextlib
import logging

from ...http_server import HTTPServer
from .pa_dlna import new_renderer

logger = logging.getLogger('streams')

@contextlib.contextmanager
def unix_socket_path():
    path = tempfile.mktemp(prefix="test_http_", suffix='.sock',
                           dir=os.path.curdir)
    path = os.path.abspath(path)

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
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(*curl_cmd,
                                          stdin=asyncio.subprocess.DEVNULL,
                                          stdout=asyncio.subprocess.PIPE,
                                          stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        if proc is not None:
            proc.terminate()
            await proc.wait()
        return None, None

    return proc.returncode, len(stdout.decode())

async def play_track(mime_type, transactions, wait_for_completion, logs,
                                                skip_wait_unixserver):
    """The sequence of events used to start the audio stream is as follows:

    - curl is used as a DLNA device and sends a HTTP GET request.
    - Then the renderer instance starts a parec process (and possibly an
      encoder process)
    - The parec process or the encoder one (the last process in the chain
      parec -> encoder) connects to a UnixSocketServer instance.
    - The UnixSocketServer instance controls parec OR encoder

    The data flow is in the opposite direction and goes from
    UnixSocketServer -> DLNA device (i.e. curl):

    - parec generates the data (under the control of UnixSocketServer if no
      encoder) and sends it to the encoder if it exists or to the
      Track.write_track() instance otherwise.
    - encoder receives data from parec and sends it to the Track.write_track()
      instance (under the control of UnixSocketServer).
    """

    if wait_for_completion:
        loop = asyncio.get_running_loop()
        completed = loop.create_future()
    else:
        completed = None

    logger.info('Starting play_track()')
    with unix_socket_path() as sock_path:
        renderer = await new_renderer(mime_type, sock_path)

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

        if skip_wait_unixserver:
            return curl_task, http_server

        # Wait for the last chunk of data to be written to the pipe read by
        # Track.write_track().
        if completed is not None:
            try:
                await asyncio.wait_for(completed, timeout=1)
            except TimeoutError:
                logger.error(f'***** server.stage: {server.stage}')
                logger.error(f'***** http_server.stage: {http_server.stage}')
                logger.error('\n'.join(l for l in logs
                                       if ':asyncio:' not in l))
                raise

        return curl_task, renderer

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
        self.aio_server = None
        self.stage = 'init'

    def close(self):
        if self.aio_server is not None:
            self.aio_server.close()

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
            self.aio_server = aio_server
            async with aio_server:
                self.startup.set_result(True)
                await aio_server.serve_forever()
        except Exception as e:
            try:
                self.startup.set_result(True)
            except asyncio.InvalidStateError:
                pass
            raise
