"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import os
import asyncio
import signal
import logging
import h11
import datetime
import email.utils
import itertools
from http import HTTPStatus

from .upnp.util import AsyncioTasks, log_unhandled_exception, log_exception
from .encoders import FFMpegEncoder, L16Encoder

logger = logging.getLogger('http')

# A stream with a throughput of 1 Mbps sends 2048 bytes every 15.6 msecs.
HTTP_CHUNK_SIZE = 2048
MAX_RECV = 2**16
TIMEOUT = 10

# Our 'Server' header.
SERVER_HEADER = h11.PRODUCT_ID.encode('ascii')

async def kill_process(process):
    try:
        try:
            # First try with SIGTERM.
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=1.0)
    except ProcessLookupError as e:
        logger.debug(f"Ignoring exception: '{e!r}'")

def basic_headers():
    # Generate a RFC 7231 / RFC 9110 IMF-fixdate string.
    dt = datetime.datetime.now(datetime.timezone.utc)
    dt =  email.utils.format_datetime(dt, usegmt=True)

    # HTTP requires these headers in all responses.
    return [
        ('Date', dt.encode('ascii')),
        ('Server', SERVER_HEADER),
    ]

class Track:
    """An HTTP socket connected to a subprocess stdout."""

    def __init__(self, session, h11_wrapper, task_name):
        self.session = session
        self.task_name = task_name
        self.h11_wrapper = h11_wrapper
        self.task = None
        self.closing = False

    @log_unhandled_exception(logger)
    async def shutdown(self):
        """Close the HTTP socket."""

        if self.h11_wrapper is None:
            return
        h11_wrapper = self.h11_wrapper
        self.h11_wrapper = None

        try:
            # Write the last chunk.
            if (h11_wrapper.conn.our_state is h11.SEND_BODY and
                    not h11_wrapper.writer.is_closing()):
                await h11_wrapper.send(h11.EndOfMessage())
            await h11_wrapper.shutdown_and_clean_up()
            logger.debug(f'{self.task_name}: track is stopped')
        except asyncio.CancelledError:
            if asyncio.current_task() == self.task:
                raise
            logger.debug(f'{self.task_name}: Got CancelledError at Track'
                         f' shutdown')

    async def close(self):
        """Run the shutdown coroutine to stop the track."""

        if not self.closing:
            self.closing = True
            if asyncio.current_task() == self.task:
                await self.shutdown()
            else:
                # shutdown() is run in a task when Track.run() gets
                # CancelledError.
                self.task.cancel()

    async def write_track(self, reader):
        """Write to the StreamWriter what is read from a subprocess stdout."""

        logger = logging.getLogger('writer')
        while True:
            partial_data = False
            if self.h11_wrapper.writer.is_closing():
                logger.debug(f'{self.task_name}: socket is closing')
                break
            try:
                data = await reader.readexactly(HTTP_CHUNK_SIZE)
            except asyncio.IncompleteReadError as e:
                data = e.partial
                partial_data = True

            if data:
                h11_state = self.h11_wrapper.conn.our_state
                assert h11_state is h11.SEND_BODY, \
                    f'Expecting SEND_BODY but our state is {h11_state}'
                await self.h11_wrapper.send(h11.Data(data=data))

            if not data or partial_data:
                logger.debug(f'EOF reading from pipe on {self.task_name}')
                break

    @log_unhandled_exception(logger)
    async def run(self, reader):
        assert self.task is not None
        renderer = self.session.renderer
        try:
            logger.debug(f'{self.task_name}: track is started')
            await self.write_track(reader)
            await self.shutdown()
        except asyncio.CancelledError:
            self.session.stream_tasks.create_task(self.shutdown(),
                                                  name='shutdown')
        except ConnectionError as e:
            logger.error(f'{self.task_name} HTTP socket is closed: {e!r}')
            await self.session.close_session()
            await renderer.close()
        except Exception:
            await self.session.close_session()
            raise

class StreamProcesses:
    """Processes connected through pipes to an HTTP socket.

        - 'parec' records the audio from the nullsink monitor and pipes it
          to the encoder program.
        - The encoder program encodes the audio according to the encoder
          protocol and forwards it to the Track instance.
        - The Track instance writes the track to the HTTP socket.

    The track is written to the parec stdout buffer while the device is
    switching to a new track upon receiving the 'SetNextAVTransportURI' and
    the new encoder has not been started yet. Hence the need for the
    'pipe_reader' attribute.

    Attributes:
        pipe_reader
            The file descriptor of the parec to encoder pipe.
        stream_reader
            The asyncio StreamReader at the end of the pipe chain.

    """

    def __init__(self, session):
        self.session = session
        self.parec_proc = None
        self.parec_task = None
        self.encoder_proc = None
        self.encoder_task = None
        self.pipe_reader = -1
        self.stream_reader = None
        self.closing = False
        self.no_encoder = isinstance(session.renderer.encoder, L16Encoder)
        self.queue = asyncio.Queue()

    async def close_encoder(self):
        if self.encoder_proc is not None:
            # Prevent verbose error logs from ffmpeg upon SIGTERM.
            if isinstance(self.session.renderer.encoder, FFMpegEncoder):
                for task in self.session.stream_tasks:
                    if task.get_name() == 'encoder_stderr':
                        task.cancel()
                        break
            await kill_process(self.encoder_proc)
            self.encoder_proc = None
            self.stream_reader = None
            return True

    async def close(self, disable=False):
        if self.closing:
            return
        self.closing = True

        renderer = self.session.renderer
        try:
            parec_killed = False
            if self.parec_proc is not None:
                await kill_process(self.parec_proc)
                if not self.no_encoder:
                    os.close(self.pipe_reader)
                self.parec_proc = None
                self.stream_reader = None
                parec_killed = True

            if await self.close_encoder() or parec_killed:
                logger.debug(f'All {renderer.name} stream processes'
                             f' terminated')

            if disable:
                await renderer.disable_root_device()

        except Exception as e:
            log_exception(logger, f'{e!r}')

    async def get_stream_reader(self):
        """Get the stdout pipe of the last process."""

        # Use the same stream_reader when parec is the only subprocess
        # running.
        if self.stream_reader is None:
            self.stream_reader = await self.queue.get()
        return self.stream_reader

    @log_unhandled_exception(logger)
    async def log_stderr(self, name, stderr):
        logger = logging.getLogger(name)

        renderer = self.session.renderer
        remove_env = False
        if (name == 'encoder' and
                isinstance(renderer.encoder, FFMpegEncoder) and
                'AV_LOG_FORCE_NOCOLOR' not in os.environ):
            os.environ['AV_LOG_FORCE_NOCOLOR'] = '1'
            remove_env = True
        try:
            while True:
                msg = await stderr.readline()
                if msg == b'':
                    break
                logger.error(msg.decode().strip())
        finally:
            # Checking for the environment variable still in the environ
            # because the test suite also patches os.environ in
            # test_http_server.py.
            if remove_env and 'AV_LOG_FORCE_NOCOLOR' in os.environ:
                del os.environ['AV_LOG_FORCE_NOCOLOR']

    @log_unhandled_exception(logger)
    async def run_parec(self, encoder, parec_cmd, stdout=None):
        renderer = self.session.renderer

        if self.no_encoder:
            stdout = asyncio.subprocess.PIPE
        monitor = renderer.nullsink.sink.monitor_source_name
        parec_cmd.extend([f'--device={monitor}',
                          f'--format={encoder.sample_format}',
                          f'--rate={encoder.rate}',
                          f'--channels={encoder.channels}'])
        logger.info(f"{renderer.name}: {' '.join(parec_cmd)}")

        exit_status = 0
        self.parec_proc = await asyncio.create_subprocess_exec(
                                *parec_cmd,
                                stdin=asyncio.subprocess.DEVNULL,
                                stdout=stdout,
                                stderr=asyncio.subprocess.PIPE)

        if self.no_encoder:
            self.queue.put_nowait(self.parec_proc.stdout)
        else:
            os.close(stdout)
        self.session.stream_tasks.create_task(
                    self.log_stderr('parec', self.parec_proc.stderr),
                    name='parec_stderr')

        ret = await self.parec_proc.wait()
        self.parec_proc = None
        self.stream_reader = None
        exit_status = ret if ret >= 0 else signal.strsignal(-ret)
        logger.info(f'Exit status of parec process: {exit_status}')
        if exit_status in (0, 'Killed', 'Terminated'):
            await self.close()
            return

        await self.close(disable=True)

    @log_unhandled_exception(logger)
    async def run_encoder(self, encoder_cmd):
        renderer = self.session.renderer

        logger.info(f"{renderer.name}: {' '.join(encoder_cmd)}")

        exit_status = 0
        self.encoder_proc = await asyncio.create_subprocess_exec(
                                *encoder_cmd,
                                stdin=self.pipe_reader,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE)
        self.queue.put_nowait(self.encoder_proc.stdout)
        self.session.stream_tasks.create_task(
                self.log_stderr('encoder', self.encoder_proc.stderr),
                name='encoder_stderr')

        ret = await self.encoder_proc.wait()
        self.encoder_proc = None
        self.stream_reader = None
        exit_status = ret if ret >= 0 else signal.strsignal(-ret)
        # ffmpeg exit code is 255 when the process is killed with SIGTERM.
        # See ffmpeg main() at https://gitlab.com/fflabs/ffmpeg/-/blob/
        # 0279e727e99282dfa6c7019f468cb217543be243/fftools/ffmpeg.c#L4833
        if (isinstance(renderer.encoder, FFMpegEncoder) and
                exit_status == 255):
            exit_status = 'Terminated'
        logger.info(f'Exit status of encoder process: {exit_status}')

        if exit_status in (0, 'Killed', 'Terminated'):
            return

        await self.close(disable=True)

    async def run(self):
        renderer = self.session.renderer
        logger.info(f'Start {renderer.name} stream process(es)')
        encoder = renderer.encoder
        try:
            if self.parec_proc is None:
                # Start the parec task.
                # An L16Encoder stream only runs the parec program.
                # Use a copy of parec_cmd.
                parec_cmd = renderer.control_point.parec_cmd[:]
                if self.no_encoder:
                    coro = self.run_parec(encoder, parec_cmd)
                else:
                    self.pipe_reader, stdout = os.pipe()
                    coro = self.run_parec(encoder, parec_cmd, stdout)
                self.parec_task = self.session.stream_tasks.create_task(coro,
                                                                name='parec')

            # Start the encoder task.
            if not self.no_encoder and self.encoder_proc is None:
                encoder_cmd = encoder.command
                self.encoder_task = self.session.stream_tasks.create_task(
                                self.run_encoder(encoder_cmd), name='encoder')
        except Exception as e:
            log_exception(logger, f'{e!r}')
            await self.close(disable=True)

class StreamSessions:
    """Handle multiple tracks.

    A track is processed with the stream data flowing through pipes
    established between stream subprocesses and the HTTP socket:

        parec process | encoder process | Track instance writing to HTTP socket

    A new session starts when 'track_count' is zero and ends upon a call to
    the close_session() method. Stopping a track terminates the encoder
    process but not the parec process.

    Two tracks may overlap within a given session, indeed this is the purpose
    of the 'SetNextAVTransportURI' UPnP soap action: the DLNA device uploads
    the next track while it is playing the end of the current track by
    emptying its buffer. This is implemented here by the Track.shutdown()
    coroutine running in a task.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.is_playing = False
        self.processes = None
        self.track = None
        self.track_count = 0
        self.stream_tasks = AsyncioTasks()

    async def stop_track(self):
        self.is_playing = False
        if self.track is not None:
            try:
                await self.track.close()
            finally:
                self.track = None
        if self.processes is not None:
            await self.processes.close_encoder()

    async def close_session(self):
        try:
            await self.stop_track()
            if self.processes is not None:
                await self.processes.close()
                self.processes = None
        finally:
            self.track_count = 0

    async def start_track(self, h11_wrapper):
        self.is_playing = True
        # Start the subprocesses.
        if self.processes is None:
            self.processes = StreamProcesses(self)
        await self.processes.run()

        # Get the reader from the last subprocess on the pipe chain.
        reader = await self.processes.get_stream_reader()

        self.track_count += 1
        task_name = f'{self.renderer.name}-track-{self.track_count}'
        self.track = Track(self, h11_wrapper, task_name)
        self.track.task = self.stream_tasks.create_task(
                                        self.track.run(reader),
                                        name=task_name)

class H11Wrapper:
    _next_id = itertools.count()

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.conn = h11.Connection(h11.SERVER)

        # A unique id for this connection, to include in debugging output
        # (useful for understanding what's going on if there are multiple
        # simultaneous clients).
        self._obj_id = next(H11Wrapper._next_id)

    async def send(self, event):
        # The code below doesn't send ConnectionClosed, so we don't bother
        # handling it here either -- it would require that we do something
        # appropriate when 'data' is None.
        assert type(event) is not h11.ConnectionClosed
        data = self.conn.send(event)
        try:
            self.writer.write(data)
            await self.writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            # We have no choice but to give it up.
            self.conn.send_failed()
            logger.error(f'H11Wrapper.send() failure, '
                         f'our_state: {self.conn.our_state} - '
                         f'writer.is_closing: {self.writer.is_closing()}')
            raise

    async def _read_from_peer(self):
        # See https://datatracker.ietf.org/doc/html/rfc7231#section-5.1.1.
        #   "A client MUST NOT generate a 100-continue expectation in a
        #   request that does not include a message body".
        #if self.conn.they_are_waiting_for_100_continue:
        #    logger.error('Ignoring a "100 Continue"')

        try:
            data = await self.reader.read(MAX_RECV)
        except ConnectionError:
            # They've stopped listening. Not much we can do about it here.
            data = b''
        self.conn.receive_data(data)

    async def next_event(self):
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                await self._read_from_peer()
                continue
            return event

    async def shutdown_and_clean_up(self):
        async  def wait_and_read():
            try:
                while True:
                    # Attempt to read until EOF
                    got = await self.reader.read(MAX_RECV)
                    if not got:
                        break
            finally:
                self.writer.close()
                await self.writer.wait_closed()

        # When this method is called, it's because we definitely want to kill
        # this connection, either as a clean shutdown or because of some kind
        # of error or loss-of-sync bug, and we no longer care if that violates
        # the protocol or not. So we ignore the state of self.conn, and just
        # go ahead and do the shutdown on the socket directly. (If you're
        # implementing a client you might prefer to send ConnectionClosed()
        # and let it raise an exception if that violates the protocol.)
        #
        try:
            self.writer.write_eof()
        except ConnectionError:
            # They're already gone, nothing to do
            return

        # Wait and read for a bit to give them a chance to see that we closed
        # things, but eventually give up and just close the socket.
        try:
            await asyncio.wait_for(wait_and_read(), TIMEOUT)
        except (TimeoutError, ConnectionError):
            pass

    def info(self, msg):
        logger.debug(f'h11-{self._obj_id}: {msg}')

class HTTPServer:
    """HHTP server accepting connections only from 'allowed_ips'.

    Reference: Hypertext Transfer Protocol -- HTTP/1.1 - RFC 7230.
    """

    def __init__(self, control_point, ip_address, port):
        self.control_point = control_point
        self.ip_address = ip_address
        self.port = port
        self.allowed_ips = set()

        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def allow_from(self, ip_addr):
        self.allowed_ips.add(ip_addr)

    async def send_response(self, status_code, headers=None, body=None):
        self.h11_wrapper.info(f'Sending {status_code} response')
        headers = [] if headers is None else headers
        headers.extend(basic_headers())
        headers.append(('Connection', 'close'))

        if body is not None:
            headers.append(('Content-Length', str(len(body))))

        res = h11.Response(status_code=status_code, headers=headers)
        await self.h11_wrapper.send(res)
        if body is not None:
            await self.h11_wrapper.send(h11.Data(data=body))

    async def send_http_ok(self, renderer):
        headers = [('Content-Type', renderer.mime_type),
                   ('Transfer-Encoding', 'chunked')]
        await self.send_response(200, headers=headers)

    async def send_error_response(self, status_code, body=None):
        h11_wrapper = self.h11_wrapper
        if body is None:
            await self.send_response(status_code)
        else:
            await self.send_response(status_code,
                [('Content-Type', 'text/plain; charset=utf-8')],
                body.encode('utf-8'))
        await self.h11_wrapper.send(h11.EndOfMessage())

        logger.error(f'Http error: {status_code}'
                     f"{(' - ' + body) if body is not None else ''}")

    async def match_renderer(self, request):
        for renderer in self.control_point.renderers():
            if not renderer.match(request.target.decode()):
                continue

            if request.http_version != b'1.1':
                await self.send_error_response(
                                    HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                                    'HTTP/1.1 is required')
                await renderer.disable_root_device()
                break
            if renderer.stream_sessions.is_playing:
                await self.send_error_response(HTTPStatus.CONFLICT,
                                    f'Cannot start {renderer.name} stream'
                                    f' (already running)')
                break
            if renderer.nullsink is None:
                await self.send_error_response(HTTPStatus.CONFLICT,
                                    f'{renderer.name} temporarily disabled')
                break

            if request.method == b'HEAD':
                await self.send_http_ok(renderer)
                logger.info(f'HTTP/1.1 HEAD request from {self.ip_address}')
                await self.h11_wrapper.send(h11.EndOfMessage())
                break

            return renderer

        else:
            await self.send_error_response(HTTPStatus.NOT_FOUND,
                                          'Cannot find a matching renderer')

        return None

    @log_unhandled_exception(logger)
    async def client_connected(self, reader, writer):
        """Handle an HTTP 1.1 request from a DLNA device.

        This is a callback scheduled as a task by asyncio.
        """

        peername = writer.get_extra_info('peername')
        ip_source = peername[0]
        if ip_source not in self.allowed_ips:
            sockname = writer.get_extra_info('sockname')
            logger.warning(f'Discarded TCP connection from {ip_source} (not'
                           f' allowed) received on {sockname[0]}')
            writer.close()
            return

        h11_wrapper = H11Wrapper(reader, writer)
        self.h11_wrapper = h11_wrapper
        h11_wrapper.info('Got new connection')
        assert h11_wrapper.conn.states == {h11.CLIENT: h11.IDLE, h11.SERVER: h11.IDLE}

        do_close = True
        try:
            self.h11_wrapper.info('Server main loop waiting for request')
            event = await asyncio.wait_for(h11_wrapper.next_event(), TIMEOUT)
            self.h11_wrapper.info(f'Server main loop got event: {event}')

            if (type(event) is not h11.Request or
                    event.method not in (b'GET', b'HEAD')):
                await self.send_error_response(HTTPStatus.BAD_REQUEST)
                return

            renderer = await self.match_renderer(event)
            if renderer is not None:
                # Ok, handle the request.
                await self.send_http_ok(renderer)
                await renderer.stream_sessions.start_track(h11_wrapper)
                do_close = False
                return

        except Exception as exc:
            h11_wrapper.info(f'Error during response handler: {exc!r}')
            if isinstance(exc, h11.RemoteProtocolError):
                status_code = exc.error_status_hint
            else:
                status_code = 500
            await self.send_error_response(status_code, str(exc))
        finally:
            if do_close:
                await h11_wrapper.shutdown_and_clean_up()

    @log_unhandled_exception(logger)
    async def run(self):
        task_name = asyncio.current_task().get_name()
        try:
            aio_server = await asyncio.start_server(self.client_connected,
                                                    self.ip_address, self.port)
            addrs = ', '.join(str(sock.getsockname())
                              for sock in aio_server.sockets)
            logger.info(f'{task_name} serve HTTP requests on {addrs}')

            async with aio_server:
                try:
                    self.startup.set_result(None)
                    await aio_server.serve_forever()
                finally:
                    logger.info(f'{task_name} closed')

        except Exception as e:
            await self.control_point.close(f'{e!r}')
            raise
