import sys
import socket
import asyncio
import unittest
import inspect
from unittest import mock

from ..upnp import UPnPControlPoint
from ..network import MCAST_ADDR

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
            await asyncio.sleep(0)
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
            except asyncio.TimeoutError:
                raise AssertionError(
                    f'{patch_method}() not called') from None

    return control_point
