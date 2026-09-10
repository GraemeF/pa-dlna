import os
import contextlib
import logging
import subprocess
import unittest
import functools
import shutil
import asyncio

from ..init import require_libpulse_version, MIN_LIBPULSE_VERSION
from ..upnp.tests import load_ordered_tests, find_in_logs, search_in_logs

def setUpModule():
    require_libpulse_version(MIN_LIBPULSE_VERSION)

def _id(obj):
    return obj

@functools.cache
def requires_resources(resources):
    """Skip the test when one of the resource is not available.

    'resources' is a string or a tuple instance (MUST be hashable).
    """

    resources = [resources] if isinstance(resources, str) else resources
    for res in resources:
        try:
            if res == 'os.devnull':
                # Check that os.devnull is writable.
                with open(os.devnull, 'w'):
                    pass
            elif res in ('curl', 'ffmpeg', 'upmpdcli', 'mpd'):
                path = shutil.which(res)
                if path is None:
                    raise Exception
            elif res == 'libpulse':
                # Check that pulseaudio or pipewire-pulse is running.
                subprocess.run(['pactl', 'info'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=True)
            else:
                # Otherwise check that the module can be imported.
                exec(f'import {res}')
        except Exception:
            return unittest.skip(f"'{res}' is not available")
    else:
        return _id

async def skip_loop_iterations(count):
    """Skip 'count' loop iterations (cost: few msecs)."""

    for i in range(count):
        await asyncio.sleep(0)

class BaseTestCase(unittest.TestCase):
    def setUp(self):
        # Redirect stderr to os.devnull.
        self.stack = contextlib.ExitStack()
        f = self.stack.enter_context(open(os.devnull, 'w'))
        self.stack.enter_context(contextlib.redirect_stderr(f))

    def tearDown(self):
        self.stack.close()

        # Remove the root logger handler set up by init.setup_logging().
        root = logging.getLogger()
        for hdl in root.handlers:
            root.removeHandler(hdl)
