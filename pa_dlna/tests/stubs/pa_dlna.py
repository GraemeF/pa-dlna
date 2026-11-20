import sys
from unittest import mock

from .libpulse import use_libpulse_stubs
from ...encoders import select_encoder, Encoder
from ...config import UserConfig

# Use the patched pulseaudio and pa_dlna modules to avoid importing libpulse
# that is not required for running the test.
with use_libpulse_stubs(['pa_dlna.pulseaudio', 'pa_dlna.pa_dlna']) as modules:
    pulseaudio, pa_dlna = modules

async def new_renderer(mime_type, sock_path=None):
    control_point = ControlPoint()
    renderer = Renderer(control_point, mime_type)

    if sock_path is not None:
        await renderer.setup(sock_path)

        if 'l16' in mime_type:
            # The encoder process is not run.
            control_point.parec_cmd.append(sock_path)
        else:
            # Appending to 'parec_cmd' is sort of a hack as the command list
            # is extended later with other options by
            # http_server.StreamProcesses.run_parec().
            control_point.parec_cmd.append('None')
    else:
        await renderer.setup()

    return renderer

def set_control_point(control_point):
    control_point.parec_cmd = [sys.executable, '-m',
                               'pa_dlna.tests.stubs.parec_proc']

    # The following patches do:
    #  - Make encoders available whether they are installed or not.
    #  - Ignore the local pa_dlna.conf when it exists.
    with mock.patch.object(Encoder, 'available') as available,\
            mock.patch('builtins.open', mock.mock_open()) as m_open:
        available.return_value = True
        m_open.side_effect = FileNotFoundError()
        control_point.config = UserConfig()

class Sink:
    monitor_source_name = 'monitor source name'

class NullSink:
    sink = Sink()

class Renderer(pa_dlna.DLNATestDevice):
    def __init__(self, control_point, mime_type):
        super().__init__(control_point, mime_type)
        self.nullsink = NullSink()
        self.set_current_uri()

    async def setup(self, sock_path=None):
        await self.select_encoder(self.root_device.udn)
        if self.encoder is not None:
            self.encoder.command = [sys.executable, '-m',
                                    'pa_dlna.tests.stubs.encoder_proc']
            if sock_path is not None:
                self.encoder.command.append(sock_path)

    async def disable_root_device(self):
        pass

    async def close(self):
        # Terminate the parec process and the encoder process (if any).
        await self.stream_sessions.close_session()

class ControlPoint(pa_dlna.AVControlPoint):
    def __init__(self):
        self.port = 8080
        self.root_devices = {}
        set_control_point(self)

    def abort(self, msg):
        pass

    async def close(self, msg=None):
        pass
