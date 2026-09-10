"""Tests that stream tracks to upmpdcli and mpd."""

import sys
import os
import time
import asyncio
import tempfile
import pathlib
import subprocess
from textwrap import dedent
from signal import SIGINT, SIGTERM
from contextlib import asynccontextmanager, AsyncExitStack
from unittest import IsolatedAsyncioTestCase
try:
    from libpulse import libpulse
except ImportError:
    libpulse = None

from . import requires_resources, setUpModule
from ..init import parse_args
from ..config import UserConfig
from ..pa_dlna import AVControlPoint

import logging
logger = logging.getLogger('tsample')

TRACK_TIMEOUT = 20
DEFAULT_ENCODER = 'L16Encoder'

# Courtesy of https://espressif-docs.readthedocs-hosted.com/projects/esp-adf/
# en/latest/design-guide/audio-samples.html.
# A 16 seconds track: Duration: 00:00:15.88, start: 0.025057, bitrate: 64 kb/s
TRACK_16 = pathlib.Path(__file__).parent / 'gs-16b-1c-44100hz.mp3'

# Map values to their name.
if libpulse is not None:
    SINK_STATES = dict((eval(f'libpulse.{state}'), state) for state in
                            ('PA_SINK_IDLE',
                             'PA_SINK_INIT',
                             'PA_SINK_INVALID_STATE',
                             'PA_SINK_RUNNING',
                             'PA_SINK_SUSPENDED',
                             'PA_SINK_UNLINKED'))

class TrackRuntimeError(Exception): pass

@asynccontextmanager
async def create_config_home(encoder, sink_name):
    "Yield temporary directory to be used as the value of XDG_CONFIG_HOME"

    with tempfile.TemporaryDirectory(dir='.') as tmpdirname:
        # Create the minimum set of mpd files.
        config_home = pathlib.Path(tmpdirname).absolute()
        mpd_path = config_home / 'mpd'

        mpd_path.mkdir()
        state_path = mpd_path / 'state'
        with open(state_path, 'w'):
            pass
        sticker_path = mpd_path / 'sticker.sql'
        with open(sticker_path, 'w'):
            pass
        mpd_conf = mpd_path / 'mpd.conf'
        with open(mpd_conf, 'w') as f:
            f.write(dedent(f'''\
                        state_file      "{state_path}"
                        sticker_file    "{sticker_path}"

                        audio_output {{
                            type            "pulse"
                            name            "My Pulse Output"
                            sink            "{sink_name}"
                        }}
                        '''))

        # Create the pa-dlna configuration file.
        padlna_path = config_home / 'pa-dlna'
        padlna_path.mkdir()
        pa_dlna_conf = padlna_path / 'pa-dlna.conf'
        with open(pa_dlna_conf, 'w') as f:
            f.write(dedent(f'''\
                        [DEFAULT]
                        selection =
                            {encoder},
            '''))

        yield str(config_home)

@asynccontextmanager
async def run_control_point(config_home, loglevel):
    async def cp_connected():
        # Wait for the connection to LibPulse.
        while cp.pulse is None or cp.pulse.pa_dlna_clients_count is None:
            await asyncio.sleep(0)
        if cp.pulse.pa_dlna_clients_count > 1:
            raise TrackRuntimeError('a pa-dlna instance is already running')
        logger.debug('Connected to libpulse')
        return  cp

    argv = ['--nics', 'lo', '--loglevel', loglevel]
    options, _ = parse_args('pa-dlna sample tests', argv=argv)

    # Override any existing pa-dlna user configuration with no user
    # configuration.
    _environ = os.environ.copy()
    try:
        os.environ.update({'XDG_CONFIG_HOME': config_home})
        config = UserConfig()
        cp = AVControlPoint(config=config, **options)
        asyncio.create_task(cp.run_control_point())
    finally:
        os.environ.clear()
        os.environ.update(_environ)

    try:
        yield await asyncio.wait_for(cp_connected(), 5)
    except TimeoutError:
        raise TrackRuntimeError('Cannot connect to libpulse') from None
    finally:
        await cp.close()

async def proc_terminate(proc, signal=None, timeout=0.2):
    async def _terminate(funcname, delay):
        start = time.monotonic()
        if funcname == 'send_signal':
            proc.send_signal(signal)
        else:
            getattr(proc, funcname)()
        await asyncio.sleep(0)
        while proc.returncode is None and time.monotonic() - start < delay:
            await asyncio.sleep(0)

    if proc.returncode is None:
        if signal is not None:
            await _terminate('send_signal', timeout)
        else:
            await _terminate('terminate', timeout)
        if proc.returncode is None:
            await _terminate('kill', 0)

@asynccontextmanager
async def proc_run(cmd, *args, env=None):
    logger.debug(f"Run command '{cmd} {' '.join(args)}'")
    environ = None
    if env is not None:
        environ = os.environ.copy()
        environ.update(env)
    proc = await asyncio.create_subprocess_exec(
        cmd, *args, env=environ,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        stderr = await asyncio.wait_for(proc.stderr.readline(), 5)
    except TimeoutError:
        raise TrackRuntimeError(
            f"'{cmd!r}' failure to output first stderr line") from None

    yield proc

    if cmd == 'upmpdcli':
        await proc_terminate(proc, signal=SIGINT)
    else:
        await proc_terminate(proc)
    await proc.wait()

    if logger.getEffectiveLevel() == logging.DEBUG:
        logger.debug(f'[{cmd!r} exited with {proc.returncode}]')
        stdout = await proc.stdout.read()
        if stdout:
            logger.debug(f'[stdout]\n{stdout.decode()}')
        stderr += await proc.stderr.read()
        if stderr:
            logger.debug(f'[stderr]\n{stderr.decode()}')

async def get_sink_state(lib_pulse, sink):
    for _sink in await lib_pulse.pa_context_get_sink_info_list():
        if _sink.index == sink.index:
            return SINK_STATES[_sink.state]
    else:
        raise TrackRuntimeError(f"'Cannot find sink '{sink.name}'")

async def sink_is_running(lib_pulse, sink):
    # Loop for ever.
    while True:
        state = await get_sink_state(lib_pulse, sink)
        if state == 'PA_SINK_RUNNING':
            return True
        await asyncio.sleep(0)

async def http_transfer_end(renderer):
    # Wait for the termination of the HTTP 1.1 chunked transfer encoding.
    while True:
        if renderer.stream_sessions.track_count == 0:
            return
        await asyncio.sleep(0.1)

class UpmpdcliMpd:
    """Set up the environment to play tracks with upmpdcli and mpd.

    'upmpdcli' is a DLNA Media Renderer implementation that forwards audio
    to 'mpd' and 'mpd' is configured to output audio to a pulse sink.

    The UpmpdcliMpd instance starts both processes, creates the pulse sink
    used by 'mpd' and gets the upmpdcli pa-dlna renderer.

    UpmpdcliMpd must be instantiated in an 'async with' statement.
    """

    def __init__(self, testcase, encoder=DEFAULT_ENCODER,
                                mpd_sink_name='MPD-sink', loglevel='error'):
        self.testcase = testcase
        self.encoder = encoder
        self.mpd_sink_name = mpd_sink_name
        self.loglevel = loglevel
        self.mpd_sink = None
        self.control_point = None
        self.lib_pulse = None
        self.renderer = None

        self.closed = False
        self.curtask = asyncio.current_task()
        self.exit_stack = AsyncExitStack()

    async def shutdown(self, end_event):
        # Run by the 'shutdown' task.
        await end_event.wait()
        await self.close('Got SIGINT or SIGTERM')

    async def close(self, msg=None):
        if self.closed:
            return

        self.closed = True
        try:
            # Close the UPnP control point to avoid annoying logs from the
            # _ssdp_notify task.
            # This will close the AVControlPoint instance.
            if (self.control_point is not None and
                    self.control_point.upnp_control_point is not None):
                self.control_point.upnp_control_point.close()

            await self.exit_stack.aclose()
        finally:
            if self.curtask != asyncio.current_task():
                self.curtask.cancel(msg)

            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.remove_signal_handler(sig)

    @asynccontextmanager
    async def create_sink(self):
        # Refuse to create the sink if it already exists.
        for sink in await self.lib_pulse.pa_context_get_sink_info_list():
            if sink.name == self.mpd_sink_name:
                raise TrackRuntimeError(
                dedent(f"""\
                The '{sink.name}' sink already exists.
                To remove this sink run the command 'pactl list sinks' to get
                the <index> of the 'Owner Module' of '{sink.name}' and unload
                this module with the command 'pactl unload-module <index>'"""
                ))

        logger.debug(f"Create sink '{self.mpd_sink_name}'")
        module_index = await self.lib_pulse.pa_context_load_module(
            'module-null-sink',
            f'sink_name="{self.mpd_sink_name}" '
            f'sink_properties=device.description="{self.mpd_sink_name}"')
        try:
            for sink in await self.lib_pulse.pa_context_get_sink_info_list():
                if sink.owner_module == module_index:
                    yield sink
                    break
            else:
                raise TrackRuntimeError(
                    f"Cannot find sink '{self.mpd_sink_name}'")
        finally:
            await self.lib_pulse.pa_context_unload_module(module_index)
            logger.debug(f'Unload null-sink module of {self.mpd_sink_name}')

    async def get_renderer(self):
        renderer = None
        while renderer is None:
            for rndrer in self.control_point.renderers():
                if rndrer.name.startswith('UpMpd-'):
                    renderer = rndrer
                    break
            await asyncio.sleep(0.1)

        while renderer.encoder is None:
            await asyncio.sleep(0.1)

        # Make sure the control_point is idle.
        await asyncio.sleep(1)

        logger.debug(f'Found renderer {renderer.name}')
        return renderer

    async def start_track(self, track_path):
        # ffmpeg plays a track to the sink of the upmpdcli renderer.
        renderer_sink = self.renderer.nullsink.sink
        args = ['-hide_banner', '-nostats', '-i', str(track_path),
                '-f', 'pulse', '-device', str(renderer_sink.index),
                track_path.stem]
        track_proc = await self.exit_stack.enter_async_context(
                                                proc_run('ffmpeg', *args))

        # Wait for the MPD sink to be running.
        try:
            await asyncio.wait_for(sink_is_running(self.lib_pulse,
                                                self.mpd_sink), TRACK_TIMEOUT)
        except TimeoutError:
            try:
                state = await get_sink_state(self.lib_pulse, self.mpd_sink)
                logger.error(f"MPD sink state is '{state}'"
                             f" after {TRACK_TIMEOUT} seconds")
            finally:
                await self.stop_track(track_proc)
                track_proc = None

        return track_proc

    async def stop_track(self, track_proc):
        # Stop the stream.
        await proc_terminate(track_proc)

        # The timeout value depends on ISSUE_48_TIMER value.
        # It has been increased by ISSUE_48_TIMER after the issue #48 fix.
        timeout = 7
        try:
            await asyncio.wait_for(http_transfer_end(self.renderer), timeout)
        except TimeoutError:
            logger.error(f'Http transfer still running {timeout} seconds '
                         f'after the audio stream source has been terminated')

    async def __aenter__(self):
        try:
            # Run the AVControlPoint.
            config_home = await self.exit_stack.enter_async_context(
                                    create_config_home(self.encoder,
                                                       self.mpd_sink_name))
            self.control_point = await self.exit_stack.enter_async_context(
                                    run_control_point(config_home,
                                                        self.loglevel))
            # Add the signal handlers (overridding the AVControlPoint
            # signal handlers).
            end_event = asyncio.Event()
            asyncio.create_task(self.shutdown(end_event), name='shutdown')
            loop = asyncio.get_running_loop()
            for sig in (SIGINT, SIGTERM):
                loop.add_signal_handler(sig, end_event.set)

            self.lib_pulse = self.control_point.pulse.lib_pulse
            logger.debug(f"XDG_CONFIG_HOME is '{config_home}'")

            # Create 'MPD-sink'.
            self.mpd_sink = await self.exit_stack.enter_async_context(
                                    self.create_sink())

            # Start the mpd and upmpdcli processes.
            await self.exit_stack.enter_async_context(
                                    proc_run('mpd', '--no-daemon',
                                        env={'XDG_CONFIG_HOME': config_home}))
            await self.exit_stack.enter_async_context(
                                    proc_run('upmpdcli', '-i', 'lo'))

            # Get the pa-dlna Renderer instance of the upmpdcli DLNA device.
            try:
                self.renderer = await asyncio.wait_for(self.get_renderer(), 5)
            except TimeoutError:
                raise TrackRuntimeError(
                    'Cannot find the upmpdcli Renderer instance') from None

            return self

        except Exception as e:
            await self.exit_stack.aclose()
            if isinstance(e, TrackRuntimeError):
                self.testcase.skipTest(e)
            else:
                raise

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

@requires_resources(('libpulse', 'ffmpeg', 'upmpdcli', 'mpd'))
class PlayTracks(IsolatedAsyncioTestCase):

    async def play_track(self, upmpdcli):
        proc = None
        cancelled = False
        try:
            proc = await upmpdcli.start_track(TRACK_16)
            self.assertTrue(proc is not None)

            lib_pulse = upmpdcli.lib_pulse
            mpd_state = await get_sink_state(lib_pulse, upmpdcli.mpd_sink)
            self.assertEqual(mpd_state, 'PA_SINK_RUNNING')

            renderer_sink = upmpdcli.renderer.nullsink.sink
            renderer_state = await get_sink_state(lib_pulse,
                                                            renderer_sink)
            self.assertEqual(renderer_state, 'PA_SINK_RUNNING')

            state = await upmpdcli.renderer.get_transport_state()
            self.assertEqual(state, 'PLAYING')

        except asyncio.CancelledError as e:
            logger.info(f'Got {e!r}')
            cancelled = True
        finally:
            if proc is not None:
                await upmpdcli.stop_track(proc)
            if cancelled:
                self.fail('The test has been cancelled')

    async def test_play_track_aac(self):
        async with UpmpdcliMpd(self, encoder='FFMpegAacEncoder') as upmpdcli:
            await self.play_track(upmpdcli)

    async def test_play_track_l16(self):
        async with UpmpdcliMpd(self, encoder='L16Encoder') as upmpdcli:
            await self.play_track(upmpdcli)

async def main():
    encoder = DEFAULT_ENCODER
    if len(sys.argv) == 2:
        encoder = sys.argv[1]

    async with UpmpdcliMpd(encoder=encoder, loglevel='debug') as upmpdcli:
        logger.info(f"Using '{encoder}' encoder")

        proc = None
        try:
            proc = await upmpdcli.start_track(TRACK_16)
            if proc is None:
                return

            lib_pulse = upmpdcli.lib_pulse
            mpd_state = await get_sink_state(lib_pulse, upmpdcli.mpd_sink)
            logger.info(f'MPD sink state: {mpd_state}')

            renderer_sink = upmpdcli.renderer.nullsink.sink
            renderer_state = await get_sink_state(lib_pulse,
                                                            renderer_sink)
            logger.info(f'upmpdcli sink state: {renderer_state}')

            # Get the upmpdcli MediaRenderer state using a
            # 'GetTransportInfo' soap action.
            state = await upmpdcli.renderer.get_transport_state()
            logger.info(f'upmpdcli MediaRenderer state: {state}')

        except asyncio.CancelledError as e:
            logger.info(f'Got {e!r}')
        finally:
            if proc is not None:
                await upmpdcli.stop_track(proc)

if __name__ == '__main__':
    asyncio.run(main())
