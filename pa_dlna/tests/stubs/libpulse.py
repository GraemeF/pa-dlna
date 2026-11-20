import sys
import re
import importlib
import contextlib
import asyncio
import collections.abc
from unittest import mock

# The following values are right but only needed to have the pulseaudio module
# import the current module as a stub.
PA_SUBSCRIPTION_MASK_SINK_INPUT = 4
PA_INVALID_INDEX = -1

SKIP_LOOP_ITERATIONS = 30

async def skip_loop_iterations(count):
    """Skip 'count' loop iterations (cost: few msecs)."""

    for i in range(count):
        await asyncio.sleep(0)

@contextlib.contextmanager
def use_libpulse_stubs(modules):
    """Patch 'modules' with stubs defined in this module.

    The first module in 'modules' is patched first.
    """

    def recurse_import(modules):
        if len(modules):
            module = modules.pop(0)
            with mock.patch.dict('sys.modules',
                                 {module: importlib.import_module(module)}):
                return recurse_import(modules) + [sys.modules[module]]
        else:
            return []

    for module in modules:
        if module in sys.modules:
            del sys.modules[module]
    for module in ('libpulse', 'libpulse.libpulse'):
        if module in sys.modules:
            del sys.modules[module]
    importlib.invalidate_caches()

    with mock.patch.dict('sys.modules',
                         {'libpulse': sys.modules[__name__],
                          'libpulse.libpulse': sys.modules[__name__]
                          }):
        yield tuple(reversed(recurse_import(modules.copy())))

    for module in modules:
        assert module not in sys.modules

class LibPulseError(Exception): pass
class LibPulseClosedError(LibPulseError): pass
class LibPulseStateError(LibPulseError): pass
class LibPulseOperationError(LibPulseError): pass

class Event:
    def __init__(self, event, proplist={'media.role': 'music'}):
        assert event in ('new', 'change', 'remove')
        self.type = event
        self.proplist = proplist
        self.index = None

class EventIterator:
    """Pulse events asynchronous iterator."""

    def __init__(self, lib_pulse):
        self.lib_pulse = lib_pulse

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            has_event = False
            for sink_input in self.lib_pulse.sink_inputs:
                event = sink_input.get_event()
                if event is not None:
                    has_event = True
                    return event
                    # Allow the processing of the event.
                    await skip_loop_iterations(SKIP_LOOP_ITERATIONS)
            if not has_event:
                # The sink_inputs don't have any more events.
                raise StopAsyncIteration

class SinkInput:
    index = 0

    def __init__(self, name, events):
        assert isinstance(events, collections.abc.Sequence)
        self.name = name
        self.events = events
        self.sink = None

        self.index = SinkInput.index
        SinkInput.index += 1

    def get_event(self):
        if len(self.events):
            event = self.events.pop(0)
            self.proplist = event.proplist
            return event

    def __str__(self):
        return self.name

class Sink:
    index = 0

    def __init__(self, name, owner_module=None):
        self.name = name
        self.owner_module = owner_module
        self.sink_input = None

        self.index = Sink.index
        Sink.index += 1

    def __str__(self):
        return self.name

class LibPulse():
    """LibPulse stub."""

    sink_inputs = None
    sink_input_index = 0
    do_raise_once = False

    def __init__(self, name):
        assert self.sink_inputs is not None, ('missing call to'
                                              ' LibPulse.add_sink_inputs()')

        self.raise_once()
        Sink.index = 0
        Event.index = 0
        self.module_index = 0
        default_sink = Sink('auto-null')    # The pulseaudio default sink.
        self.sinks = [default_sink]

    @classmethod
    def add_sink_inputs(cls, sink_inputs):
        """Extend the list of sink_inputs.

        This class method MUST be called BEFORE the instantiation of
        LibPulse.
        The first sink_input in the list (if any) is associated with the sink
        loaded by the following call to LibPulse.pa_context_load_module().
        """

        cls.sink_inputs = sink_inputs
        for sink_input in sink_inputs:
            index = cls.sink_input_index
            sink_input.index = index
            for event in sink_input.events:
                event.index = index
            cls.sink_input_index += 1

    async def pa_context_load_module(self, module, args):
        assert module == 'module-null-sink'
        args = dict(re.findall(r"(?P<key>\w+)=\"(?P<value>[^\"]*)\"", args))
        sink_name = args['sink_name'].strip("\"")
        for sink in self.sinks:
            if sink.name == sink_name:
                sink_name = sink_name + '.1'

        index = self.module_index
        sink = Sink(sink_name, owner_module=index)

        # Link this sink to the first sink_input.
        if len(LibPulse.sink_inputs):
            LibPulse.sink_inputs[0].sink = sink.index

        self.sinks.append(sink)
        self.module_index += 1
        return index

    async def pa_context_unload_module(self, index):
        for i, sink in enumerate(list(self.sinks)):
            if sink.owner_module == index:
                self.sinks.pop(i)
                break

    async def pa_context_get_sink_info_list(self):
        return list(sink for sink in self.sinks)

    async def pa_context_get_sink_input_info_list(self):
        return list(sink_input for sink_input in LibPulse.sink_inputs)

    async def pa_context_get_sink_info_by_name(self, name):
        for sink in self.sinks:
            if sink.name == name:
                return sink

    async def pa_context_subscribe(self, mask):
        assert mask == PA_SUBSCRIPTION_MASK_SINK_INPUT

    async def pa_context_get_client_info(self, index):
        if self.sink_inputs:
            return self.sink_inputs[0].client

    async def pa_context_get_client_info_list(self):
        return []

    async def log_server_info(self):
        return

    def get_events_iterator(self):
        return EventIterator(self)

    def raise_once(self):
        if self.do_raise_once:
            LibPulse.do_raise_once = False
            raise LibPulseStateError

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        LibPulse.sink_inputs = None
        LibPulse.sink_input_index = 0
        LibPulse.do_raise_once = False
