import sys
import os
import re
import contextlib
import logging
import subprocess
import unittest
import functools
import shutil
import asyncio
from logging import NOTSET, DEBUG, INFO, WARNING, ERROR, CRITICAL
from unittest import IsolatedAsyncioTestCase, TestCase

from ..init import require_libpulse_version, MIN_LIBPULSE_VERSION
from ..upnp.tests import (load_ordered_tests, find_in_logs, search_in_logs,
                          RUNNING_ONLY_MATCHING_TESTS, setup_test_logging)

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
            elif res == 'pulseaudio or pipewire':
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

class BaseTestCase:
    def setUp(self):
        # Set up logging.
        self.test_loggers_stack = contextlib.ExitStack()
        self.logging_output = self.test_loggers_stack.enter_context(
                                        setup_test_logging(DEBUG, True))
        self.addCleanup(self.test_loggers_stack.close)

class LoggingIsolatedAsyncioTestCase(BaseTestCase, IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()

    async def has_error_in_logs(self, logger, error_msg):
        regexp = re.compile(error_msg)
        while True:
            if search_in_logs(self.logging_output, logger, regexp):
                return regexp
            await asyncio.sleep(0.1)

class LoggingTestCase(BaseTestCase, TestCase):
    def setUp(self):
        super().setUp()

        # Redirect stderr to os.devnull.
        if not RUNNING_ONLY_MATCHING_TESTS:
            f = self.test_loggers_stack.enter_context(open(os.devnull, 'w'))
            self.test_loggers_stack.enter_context(
                                            contextlib.redirect_stderr(f))
