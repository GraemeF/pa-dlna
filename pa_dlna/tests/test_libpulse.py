"""Testing calls to libpulse.libpulse.LibPulse methods."""

import asyncio
import logging
import re
import uuid

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import (requires_resources, search_in_logs,
               LoggingIsolatedAsyncioTestCase)
from ..pulseaudio import Pulse, NullSink

logger = logging.getLogger('libpulse tests')

class SinkInput:
    def __init__(self, index=None, client=None):
        self.index = index
        self.client = client

class Sink:
    def __init__(self, index=None, name=None):
        self.index = index
        self.name = name

class Renderer:
    def __init__(self, sink):
        self.nullsink = NullSink(sink)

class ControlPoint:
    def __init__(self):
        self.clients_uuids = None
        self.applications = None
        self.start_event = asyncio.Event()

        loop = asyncio.get_running_loop()
        self.test_end = loop.create_future()

    async def close(self):
        self.start_event.set()

    async def dispatch_event(self, event):
        pass

@requires_resources('libpulse')
class LibPulseTests(LoggingIsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.control_point = ControlPoint()
        self.pulse = Pulse(self.control_point)
        asyncio.create_task(self.pulse.run())

        # Wait for the connection to PulseAudio/Pipewire to be ready.
        await self.control_point.start_event.wait()
        if self.pulse.closing:
            self.skipTest('Cannot connect to libpulse')

    async def asyncTearDown(self):
        # Terminate the self.pulse.run() asyncio task.
        self.control_point.test_end.set_result(True)

    async def get_invalid_index(self, pulse_object):
        # Get the list of the current 'pulse_object'.
        list_method = getattr(self.pulse.lib_pulse,
                              f'pa_context_get_{pulse_object}_info_list')
        members = await list_method()
        logger.debug(f'{pulse_object}s '
                         f'{dict((el.name, el.index) for el in members)}')

        # Find the last pulse_object index.
        indexes = [member.index for member in members]
        max_index = max(indexes) if indexes else 0
        logger.debug(f'max_index: {max_index}')

        return max_index + 10

    async def test_get_client(self):
        invalid_index = await self.get_invalid_index('client')

        # Use an invalid client index.
        sink_input = SinkInput(client=invalid_index)
        client = await self.pulse.get_client(sink_input)
        self.assertEqual(client, None)

        self.assertTrue(search_in_logs(self.logging_output,
                            'pulse', re.compile(r'LibPulseOperationError')))

    async def test_move_sink_input(self):
        invalid_index = await self.get_invalid_index('sink_input')

        # Use an invalid sink_input index.
        sink_input = SinkInput(index=invalid_index)
        sink = Sink(index=0)
        sink_input = await self.pulse.move_sink_input(sink_input, sink)
        self.assertEqual(sink_input, None)

        self.assertTrue(search_in_logs(self.logging_output,
                                'pulse', re.compile(r'PA_OPERATION_RUNNING')))

    async def test_get_renderer_sink(self):
        # Use an invalid sink name.
        sinks = await self.pulse.lib_pulse.pa_context_get_sink_info_list()
        names = [sink.name for sink in sinks]
        while True:
            name = str(uuid.uuid4())
            if name not in names:
                break
        logger.debug(f'Sink name: {name}')

        sink = Sink(name=name)
        renderer = Renderer(sink)
        sink = await self.pulse.get_renderer_sink(renderer)
        self.assertEqual(sink, None)

        self.assertTrue(search_in_logs(self.logging_output,
                            'pulse', re.compile(r'LibPulseOperationError')))
