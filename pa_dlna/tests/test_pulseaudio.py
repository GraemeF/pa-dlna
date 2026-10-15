"""Pulseaudio test cases."""

import re
import asyncio
import contextlib
import tempfile
import logging
from unittest import mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import find_in_logs, search_in_logs, LoggingIsolatedAsyncioTestCase
from .streams import pulseaudio, pa_dlna
from .libpulse import (SinkInput, Event, LibPulseClosedError, LibPulse,
                       LibPulseError, PA_SUBSCRIPTION_MASK_SINK_INPUT,
                       PA_INVALID_INDEX)
from ..pa_dlna import ControlPointAbortError

class Renderer(pa_dlna.DLNATestDevice):
    def __init__(self, control_point, mime_type, results=None):
        super().__init__(control_point, mime_type)
        self.results = results

    async def run(self):
        while True:
            event_tuple = await self.pulse_queue.get()
            if self.results is not None:
                self.results.append(event_tuple)
            evt, sink, sink_input = event_tuple
            if evt == 'new':
                self.nullsink.sink = sink
                self.nullsink.sink_input = sink_input

class ControlPoint(pa_dlna.AVControlPoint):
    def __init__(self, clients_uuids=None, applications=None):
        self.clients_uuids = clients_uuids
        self.applications = applications
        self.start_event = asyncio.Event()
        self.root_devices = {}

    def abort(self, msg):
        raise ControlPointAbortError(msg)

    async def close(self):
        pass

class Pulseaudio(LoggingIsolatedAsyncioTestCase):
    """Pulseaudio test cases."""

    def setUp(self):
        super().setUp()

        # The Pulse instance to test.
        self.control_point = ControlPoint()
        self.pulse = pulseaudio.Pulse(self.control_point)

    def new_renderer(self, mime_type, results):
        renderer = Renderer(self.control_point, f'audio/{mime_type}',
                            results)
        asyncio.create_task(renderer.run())
        return renderer

    async def test_run_pulse(self):
        LibPulse.add_sink_inputs([])
        await self.pulse.run()

        self.assertTrue(find_in_logs(self.logging_output,
                                                    'pulse', 'Close pulse'))

    async def test_dispatch_event(self):
        results = []
        renderer = self.new_renderer('mp3', results)
        sink_input = SinkInput('source', [Event('new')])
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            renderer.nullsink = await self.pulse.register(renderer)
            await self.pulse.lib_pulse.pa_context_subscribe(
                                        PA_SUBSCRIPTION_MASK_SINK_INPUT)
            iterator = self.pulse.lib_pulse.get_events_iterator()
            async for event in iterator:
                await self.pulse.dispatch_event(event)
                await asyncio.sleep(0)

        self.assertTrue(results[0] == ('new', renderer.nullsink.sink,
                                       sink_input))

    async def test_clients_uuids(self):
        class Client:
            def __init__(self, proplist):
                self.proplist = proplist

        uuid = object()
        client = Client({'application.name': 'Strawberry'})
        applications = {'Strawberry': uuid}

        control_point = ControlPoint(applications=applications)
        pulse = pulseaudio.Pulse(control_point)
        sink_input = SinkInput('sink-input with a client', [])
        sink_input.client = client
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as pulse.lib_pulse:
            result = await pulse.find_sink_input(uuid)

        self.assertTrue(result is sink_input)

    def test_write_applications(self):
        with tempfile.NamedTemporaryFile(mode='w+') as f:
            control_point = ControlPoint(clients_uuids=f.name)
            pulse = pulseaudio.Pulse(control_point)
            pulse.applications = {'Strawberry': 'uuid'}
            pulse.write_applications()

            content = f.read()
            self.assertTrue('Strawberry          -> uuid' in content)

    async def test_ignore_prev_sink_input(self):
        results = []
        renderer = self.new_renderer('mp3', results)
        proplist = {'media.role': 'video'}
        sink_input = SinkInput('source', [Event('new'), Event('change'),
                                    Event('change', proplist=proplist)])
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            renderer.nullsink = await self.pulse.register(renderer)
            await self.pulse.lib_pulse.pa_context_subscribe(
                                        PA_SUBSCRIPTION_MASK_SINK_INPUT)
            iterator = self.pulse.lib_pulse.get_events_iterator()
            count = 0
            async for event in iterator:
                await self.pulse.dispatch_event(event)
                # Do not dispatch the second Event.
                renderer.previous_idx = 0 if count == 0 else None
                count += 1
                await asyncio.sleep(0)

        self.assertTrue(len(results) == 2)
        self.assertTrue(results[0] == ('new', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(results[1] == ('change', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(sink_input.proplist is proplist)

    async def test_ignore_sound_setting(self):
        results = []
        renderer = self.new_renderer('mp3', results)
        proplist_event = {'media.role': 'event'}
        proplist_video = {'media.role': 'video'}
        sink_input = SinkInput('source', [Event('new'),
                                Event('change', proplist=proplist_event),
                                Event('change', proplist=proplist_video)])
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            renderer.nullsink = await self.pulse.register(renderer)
            await self.pulse.lib_pulse.pa_context_subscribe(
                                        PA_SUBSCRIPTION_MASK_SINK_INPUT)
            iterator = self.pulse.lib_pulse.get_events_iterator()
            count = 0
            async for event in iterator:
                await self.pulse.dispatch_event(event)
                # Do not dispatch the second Event.
                renderer.previous_idx = 0 if count == 0 else None
                count += 1
                await asyncio.sleep(0)

        self.assertTrue(len(results) == 2)
        self.assertTrue(results[0] == ('new', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(results[1] == ('change', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(sink_input.proplist is proplist_video)

    async def test_connect_raise_once(self):
        LibPulse.add_sink_inputs([SinkInput('source', [Event('new')])])
        LibPulse.do_raise_once = True
        await self.pulse.run()

        output = self.logging_output
        self.assertTrue(search_in_logs(output, 'pulse',
                                       re.compile(r'LibPulseStateError()')))
        self.assertTrue(find_in_logs(output, 'pulse', 'Close pulse'))

    async def test_disconnected(self):
        with mock.patch.object(self.pulse, 'dispatch_event') as dispatch:
            LibPulse.add_sink_inputs([SinkInput('source', [Event('new')])])
            dispatch.side_effect = LibPulseClosedError()
            await self.pulse.run()

        output = self.logging_output
        self.assertTrue(search_in_logs(output, 'pulse',
                                       re.compile(r'LibPulseClosedError')))
        self.assertTrue(find_in_logs(output, 'pulse', 'Close pulse'))

    async def test_plain_register(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        LibPulse.add_sink_inputs([])
        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            sink = await self.pulse.register(renderer)
            self.assertTrue(str(self.pulse.lib_pulse.sinks[1]).startswith(
                                                    'DLNATest_mp3-uuid:'))
            await self.pulse.unregister(sink)

        self.assertTrue(search_in_logs(self.logging_output, 'pulse',
                    re.compile('Load null-sink module'
                               ' DLNATest_mp3-uuid:.*\n.*description=')))

    async def test_bad_register(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        LibPulse.add_sink_inputs([])
        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            with mock.patch.object(self.pulse.lib_pulse,
                                   'pa_context_load_module') as load:
                load.side_effect = [PA_INVALID_INDEX]
                sink = await self.pulse.register(renderer)
            self.assertTrue(sink is None)

        self.assertTrue(search_in_logs(self.logging_output,
                    'pulse', re.compile('Failed loading DLNATest_mp3-uuid:')))

    async def test_bad_get_sink_by_module(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        LibPulse.add_sink_inputs([])
        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            with mock.patch.object(self.pulse.lib_pulse,
                                   'pa_context_load_module') as load:
                load.side_effect = [999]
                await self.pulse.register(renderer)

        self.assertTrue(search_in_logs(self.logging_output,
            'pulse', re.compile('Failed getting sink of DLNATest_mp3-uuid:')))

    async def test_register_twice(self):
        renderer = Renderer(self.control_point, 'audio/mp3')
        LibPulse.add_sink_inputs([])
        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            with self.assertRaises(ControlPointAbortError) as cm:
                await self.pulse.register(renderer)
                await self.pulse.register(renderer)
            self.assertTrue(cm.exception.args[0].startswith(
                        'Two DLNA devices registered with the same name'))

    async def test_remove_pulse_event(self):
        results = []
        renderer = self.new_renderer('mp3', results)
        sink_input = SinkInput('source', [Event('new'), Event('remove')])
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            renderer.nullsink = await self.pulse.register(renderer)
            await self.pulse.lib_pulse.pa_context_subscribe(
                                        PA_SUBSCRIPTION_MASK_SINK_INPUT)
            iterator = self.pulse.lib_pulse.get_events_iterator()
            async for event in iterator:
                await self.pulse.dispatch_event(event)
                await asyncio.sleep(0)

        self.assertTrue(results[0] == ('new', renderer.nullsink.sink,
                                       sink_input))
        self.assertTrue(results[1] == ('remove', None, None))

    async def test_exit_event(self):
        results = []
        mp3_renderer = self.new_renderer('mp3', results)
        mpeg_renderer = self.new_renderer('mpeg', results)
        sink_input = SinkInput('source', [Event('new'), Event('new')])
        LibPulse.add_sink_inputs([sink_input])

        async with LibPulse('pa-dlna') as self.pulse.lib_pulse:
            mp3_renderer.nullsink = await self.pulse.register(
                                                        mp3_renderer)
            await self.pulse.lib_pulse.pa_context_subscribe(
                                        PA_SUBSCRIPTION_MASK_SINK_INPUT)
            iterator = self.pulse.lib_pulse.get_events_iterator()
            async for event in iterator:
                await self.pulse.dispatch_event(event)
                await asyncio.sleep(0)
                if mpeg_renderer.nullsink is None:
                    mpeg_renderer.nullsink = await self.pulse.register(
                                                                mpeg_renderer)

        self.assertTrue(results[0] == ('new', mp3_renderer.nullsink.sink, sink_input))
        self.assertTrue(results[1] == ('new', mpeg_renderer.nullsink.sink, sink_input))
        self.assertTrue(results[2] == ('exit', None, None))


if __name__ == '__main__':
    unittest.main(verbosity=2)
