import sys
import os
import socket
import asyncio
import unittest
import inspect
import io
import pprint
import http.server
import contextlib
from logging import NOTSET, DEBUG, INFO, WARNING, ERROR, CRITICAL
from unittest import IsolatedAsyncioTestCase, TestCase, mock

import logging
logger = logging.getLogger('tests')

from .. import TEST_LOGLEVEL
from ..upnp import UPnPControlPoint
from ..network import MCAST_ADDR

RUNNING_ONLY_MATCHING_TESTS = any(arg == '-k' for arg in sys.argv[1:])

MSEARCH_PORT = 9999
SSDP_NOTIFY = '\r\n'.join([
    'NOTIFY * HTTP/1.1',
    'Host: 239.255.255.250:1900',
    'Content-Length: 0',
    'Location: {url}',
    'Cache-Control: max-age={max_age}',
    'Server: Linux',
    'NT: upnp:rootdevice',
    '{nts}',
    'USN: {udn}::upnp:rootdevice',
    '',
    '',
])

HOST = '127.0.0.1'
HTTP_PORT = 9999
URL = f'http://{HOST}:{HTTP_PORT}/MediaRenderer/desc.xml'
UDN = 'uuid:ffffffff-ffff-ffff-ffff-ffffffffffff'
SSDP_PARAMS = { 'url': URL,
                'max_age': '1800',
                'udn': UDN
               }
SSDP_ALIVE = SSDP_NOTIFY.format(nts='NTS: ssdp:alive', **SSDP_PARAMS)

def bind_mcast_address():
    """Decorator raising SkipTest if MCAST_ADDR is already in use."""

    skip = False
    reason = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind(MCAST_ADDR)
        except OSError as e:
            if e.args[0] == 98:
                skip = True
                reason = e.args[1]
    return unittest.skipIf(skip, f'{MCAST_ADDR}: {reason}')

def load_ordered_tests(loader, standard_tests, pattern):
    """Keep the tests in the order they were declared in the class.

    Thanks to https://stackoverflow.com/a/62073640
    """

    ordered_cases = []
    for test_suite in standard_tests:
        ordered = []
        for test_case in test_suite:
            test_case_type = type(test_case)
            method_name = test_case._testMethodName
            testMethod = getattr(test_case, method_name)
            line = testMethod.__code__.co_firstlineno
            ordered.append( (line, test_case_type, method_name) )
        ordered.sort()
        for line, case_type, name in ordered:
            ordered_cases.append(case_type(name))
    return unittest.TestSuite(ordered_cases)

def find_in_logs(logs, logger, msg):
    """Return True if 'msg' from 'logger' is in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if len(log) == 3 and log[1] == logger and log[2] == msg:
            return True
    return False

def search_in_logs(logs, logger, matcher):
    """Return True if the matcher's pattern is found in a message in 'logs'."""

    for log in (log.split(':', maxsplit=2) for log in logs):
        if (len(log) == 3 and log[1] == logger and
                matcher.search(log[2]) is not None):
            return True
    return False

async def loopback_datagrams(datagrams, patch_method=None, setup=None):
    """Loopback datagrams to UPnPControlPoint._process_ssdp.

    datagrams       Either a coroutine that sends datagrams or a list of
                    datagrams to be broadcasted to the UPnP multicast
                    address.
    patch_method    The name of a method of the UPnPControlPoint instance to
                    patch.
    setup           A coroutine to be awaited for before sending the
                    datagrams.
    """

    async def send_datagrams(ip, protocol):
        # 'protocol' is the protocol of the MsearchServerProtocol instance.
        for datagram in datagrams:
            protocol.send_datagram(datagram)

    async def is_called(mock):
        while True:
            await asyncio.sleep(0.1)
            if mock.called:
                return True

    if inspect.iscoroutinefunction(datagrams):
        coro = datagrams
    else:
        coro = send_datagrams
    control_point = UPnPControlPoint(nics=['lo'], msearch_interval=3600)
    with mock.patch.object(control_point,
                           '_ssdp_msearch') as ssdp_msearch:
        if patch_method is not None:
            patcher = mock.patch.object(control_point, patch_method)
            method = patcher.start()

        # Prevent the msearch task to run UPnPControlPoint._ssdp_msearch.
        ssdp_msearch.side_effect = [None]
        if setup is not None:
            await setup(control_point)

        control_point.open()
        await control_point._notify.startup
        # 'coro' is a coroutine *function*.
        await control_point.msearch_once(coro, port=MSEARCH_PORT)

        if patch_method is not None:
            try:
                await asyncio.wait_for(is_called(method), 1)
            except TimeoutError:
                raise AssertionError(
                    f'{patch_method}() not called') from None

    return control_point

class HTTPRequestHandler(http.server.BaseHTTPRequestHandler):

    def __init__(self, reader, writer, peername):
        self._reader = reader
        self.wfile = writer
        self.client_address = peername

        # BaseHTTPRequestHandler invokes self.wfile.flush().
        def flush():
            pass
        setattr(writer, 'flush', flush)

    async def set_rfile(self):
        # Read the full HTTP request from the asyncio StreamReader into a
        # BytesIO.
        request = []
        while True:
            line = await self._reader.readline()
            request.append(line)
            if line in (b'\r\n', b'\n', b''):
                break
        self.rfile = io.BytesIO(b''.join(request))

    def log_message(self, format, *args):
        # Overriding log_message() that logs the errors.
        logger.error("%s - %s" % (self.client_address[0], format % args))

    def log_request(self, method):
        logger.info(f'{self.request_version} {method} request from '
                    f'{self.client_address[0]}')
        logger.debug(f"uri path: '{self.path}'")
        logger.debug(f'Request headers:\n'
                     f"{pprint.pformat(dict(self.headers.items()))}")

    def do_GET(self):
        self.log_request('GET')

    def do_POST(self):
        self.log_request('POST')

    def do_HEAD(self):
        self.log_request('HEAD')

@contextlib.contextmanager
def setup_test_logging(log_level, nolog_upnp):

    class CapturingHandler(logging.Handler):
        """Logging handler capturing logging output."""

        def __init__(self):
            super().__init__()
            self.output = []

        def emit(self, record):
            msg = self.format(record)
            self.output.append(msg)

    for name in ('upnp', 'network', 'asyncio'):
        logger = logging.getLogger(name)
        logger.setLevel(NOTSET)

    logging.getLogger('asyncio').setLevel(ERROR)
    if nolog_upnp:
        logging.getLogger('upnp').setLevel(INFO)
        logging.getLogger('network').setLevel(INFO)

    root = logging.getLogger()
    root.setLevel(log_level)

    # Set up the sys.stderr handler when testing matching tests.
    stderr_hdler = None
    if RUNNING_ONLY_MATCHING_TESTS:
        stderr_hdler = logging.StreamHandler()
        formatter = logging.Formatter(
                        fmt='%(name)-7s %(levelname)-7s %(message)s')
        stderr_hdler.setFormatter(formatter)
        root.addHandler(stderr_hdler)

    # Set up the capturing handler.
    capturing_hdler = CapturingHandler()
    formatter = logging.Formatter(fmt="%(levelname)s:%(name)s:%(message)s")
    capturing_hdler.setFormatter(formatter)
    root.addHandler(capturing_hdler)

    yield capturing_hdler.output

    # Remove the root logger handlers.
    if stderr_hdler is not None:
        root.removeHandler(stderr_hdler)
    root.removeHandler(capturing_hdler)

class BaseTestCase:
    def setUp(self):
        # Set up logging.
        self.test_loggers_stack = contextlib.ExitStack()
        self.logging_output = self.test_loggers_stack.enter_context(
                                setup_test_logging(TEST_LOGLEVEL, False))
        self.addCleanup(self.test_loggers_stack.close)

class LoggingIsolatedAsyncioTestCase(BaseTestCase, IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()

class LoggingTestCase(BaseTestCase, TestCase):
    def setUp(self):
        super().setUp()

        # Redirect stderr to os.devnull.
        if not RUNNING_ONLY_MATCHING_TESTS:
            f = self.test_loggers_stack.enter_context(open(os.devnull, 'w'))
            self.test_loggers_stack.enter_context(
                                            contextlib.redirect_stderr(f))
