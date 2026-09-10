"""An asyncio HHTP 1.1 server serving DLNA devices requests."""

import os
import asyncio
import signal
import urllib.parse
import logging
from http import HTTPStatus

from .upnp.util import (AsyncioTasks, log_unhandled_exception, log_exception,
                        HTTPRequestHandler)
from .encoders import FFMpegEncoder, L16Encoder

logger = logging.getLogger('http')

# A stream with a throughput of 1 Mbps sends 2048 bytes every 15.6 msecs.
HTTP_CHUNK_SIZE = 2048

async def kill_process(process):
    try:
        try:
            # First try with SIGTERM.
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except TimeoutError:
            pass
        finally:
            # Kill the process if the process is still alive.
            # And close transports of stdin, stdout, and stderr pipes,
            # otherwise we would get an exception on exit triggered by garbage
            # collection (a Python bug ?):
            # Exception ignored in: <function BaseSubprocessTransport.__del__:
            #   RuntimeError: Event loop is closed
            process._transport.close()
    except ProcessLookupError as e:
        logger.debug(f"Ignoring exception: '{e!r}'")

async def write_http_ok(writer, renderer):
    query = ['HTTP/1.1 200 OK',
             'Content-type: ' + renderer.mime_type,
             'Connection: close',
             'Transfer-Encoding: chunked',
             '', '']
    writer.write('\r\n'.join(query).encode('latin-1'))
    await writer.drain()

class Track:
    """An HTTP socket connected to a subprocess stdout.

    Attributes:
        writer
            The asyncio StreamWriter wrapping the HTTP socket.
    """

    def __init__(self, session, writer, task_name):
        self.session = session
        self.task_name = task_name
        self.writer = writer
        self.task = None
        self.closing = False

    @log_unhandled_exception(logger)
    async def shutdown(self):
        """Close the HTTP socket."""

        if self.writer is None:
            return
        writer = self.writer
        self.writer = None

        try:
            try:
                # Write the last chunk.
                if not writer.is_closing():
                    writer.write('0\r\n\r\n'.encode())
                await writer.drain()

                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass
            logger.debug(f'{self.task_name}: track is stopped')
        except asyncio.CancelledError:
            logger.debug(f'{self.task_name}: Got CancelledError at Track'
                         f' shutdown')

    def stop(self):
        """Stop the track and run the shutdown coro in a task."""

        # This method must not be run from the Track task.
        if asyncio.current_task() == self.task:
            self.session.renderer.control_point.abort(
                                'Running Track.stop() from the Track task')

        if not self.closing:
            self.closing = True
            self.task.cancel()

    async def close(self):
        """Run the shutdown coroutine to stop the track.

        This coroutine should be run after Track.write_track() has terminated.
        """

        # This method must be run from the Track task.
        if asyncio.current_task() != self.task:
            self.session.renderer.control_point.abort(
                            'Running Track.close() not from the Track task')

        if not self.closing:
            self.closing = True
            await self.shutdown()

    async def write_track(self, reader):
        """Write to the StreamWriter what is read from a subprocess stdout."""

        logger = logging.getLogger('writer')
        while True:
            partial_data = False
            if self.writer.is_closing():
                logger.debug(f'{self.task_name}: socket is closing')
                break
            try:
                data = await reader.readexactly(HTTP_CHUNK_SIZE)
            except asyncio.IncompleteReadError as e:
                data = e.partial
                partial_data = True
            if data:
                self.writer.write(f'{len(data):x}\r\n'.encode())
                self.writer.write(data)
                self.writer.write('\r\n'.encode())
                await self.writer.drain()
            if not data or partial_data:
                logger.debug(f'EOF reading from pipe on {self.task_name}')
                break

    @log_unhandled_exception(logger)
    async def run(self, reader):
        assert self.task is not None
        renderer = self.session.renderer
        try:
            await write_http_ok(self.writer, renderer)
            logger.debug(f'{self.task_name}: track is started')
            await self.write_track(reader)
            await self.shutdown()
        except asyncio.CancelledError:
            self.session.stream_tasks.create_task(self.shutdown(),
                                                  name='shutdown')
        except ConnectionError as e:
            logger.error(f'{self.task_name} HTTP socket is closed: {e!r}')
            await self.session.close_session(shutdown_coro=True)
            await renderer.close()
        except Exception:
            await self.session.close_session(shutdown_coro=True)
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
            # test_http_sever.py.
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
                self.track.stop()
            finally:
                self.track = None
        if self.processes is not None:
            await self.processes.close_encoder()

    async def close_session(self, shutdown_coro=False):
        self.is_playing = False
        self.track_count = 0
        if self.track is not None:
            try:
                if not shutdown_coro:
                    self.track.stop()
                else:
                    await self.track.close()
            finally:
                self.track = None
        if self.processes is not None:
            await self.processes.close()
            self.processes = None

    async def start_track(self, writer):
        self.is_playing = True
        # Start the subprocesses.
        if self.processes is None:
            self.processes = StreamProcesses(self)
        await self.processes.run()

        # Get the reader from the last subprocess on the pipe chain.
        reader = await self.processes.get_stream_reader()

        self.track_count += 1
        task_name = f'{self.renderer.name}-track-{self.track_count}'
        self.track = Track(self, writer, task_name)
        self.track.task = self.stream_tasks.create_task(
                                        self.track.run(reader),
                                        name=task_name)

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

    @log_unhandled_exception(logger)
    async def client_connected(self, reader, writer):
        """Handle an HTTP GET request from a DLNA device.

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

        do_close = True
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()

            if not hasattr(handler, 'path'):
                content = handler.rfile.getvalue().decode()
                request = content.splitlines()[0] if content else ''
                logger.error(f'Invalid path in HTTP request from {ip_source}:'
                             f' {request}')
                return

            # Start the stream in a new task if the GET request is valid and
            # the uri path matches one of the encoder's.

            # BaseHTTPRequestHandler has decoded the received bytes as
            # 'iso-8859-1' encoded, now unquote the uri path.
            uri_path = urllib.parse.unquote(handler.path)

            for renderer in self.control_point.renderers():
                if not renderer.match(uri_path):
                    continue

                if handler.request_version != 'HTTP/1.1':
                    handler.send_error(
                                HTTPStatus.HTTP_VERSION_NOT_SUPPORTED)
                    await renderer.disable_root_device()
                    break
                if renderer.stream_sessions.is_playing:
                    handler.send_error(HTTPStatus.CONFLICT,
                                       f'Cannot start {renderer.name} stream'
                                       f' (already running)')
                    break
                if renderer.nullsink is None:
                    handler.send_error(HTTPStatus.CONFLICT,
                                       f'{renderer.name} temporarily disabled')
                    break

                if handler.command == 'HEAD':
                    await write_http_ok(writer, renderer)
                    return

                # Ok, handle the request.
                await renderer.start_track(writer)
                # The track task has been started by the renderer's
                # StreamSessions instance.
                do_close = False
                return

            else:
                handler.send_error(HTTPStatus.NOT_FOUND,
                                   'Cannot find a matching renderer')

            # Flush the error response.
            await writer.drain()

        finally:
            if do_close:
                try:
                    writer.close()
                    await writer.wait_closed()
                except ConnectionError:
                    pass

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
