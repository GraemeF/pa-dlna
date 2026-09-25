"""Network test cases."""

import re
import asyncio
import psutil
import logging
import socket
from collections import namedtuple
from ipaddress import IPv4Address
from unittest import TestCase, IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import (find_in_logs, search_in_logs, loopback_datagrams, URL,
               MSEARCH_PORT, HOST, HTTP_PORT, SSDP_PARAMS, SSDP_NOTIFY,
               SSDP_ALIVE, HTTPRequestHandler)
from .. import TEST_LOGLEVEL
from .. import network as network_module
from ..network import (send_mcast, msearch, http_get, UPnPInvalidHttpError,
                       http_soap, Notify, ipaddr_from_nics, UPnPHttpError)
from ..util import log_unhandled_exception

logger = logging.getLogger('test_network')

ST_ROOT_DEVICE = 'ST: upnp:rootdevice'
SSDP_MSEARCH = '\r\n'.join([
    'HTTP/1.1 200 OK',
    ('Location: ' + URL),
    'Cache-Control: max-age=1800',
    'Content-Length: 0',
    'Server: Linux',
    'EXT:',
    '{st}',
    'USN: uuid:ffffffff-ffff-ffff-ffff-ffffffffffff::upnp:rootdevice',
    '', '',
])
MSEARCH_RESPONSE = SSDP_MSEARCH.format(st=ST_ROOT_DEVICE)

NOT_FOUND_REASON = 'A dummy reason'
NOT_FOUND = '\r\n'.join([
    f'HTTP/1.1 404 {NOT_FOUND_REASON}',
    'Content-Length: 0',
    '', '',
])

# A shortened psutil address.
snicaddr = namedtuple('snicaddr', ['address', 'netmask', 'family'],
                      defaults=[socket.AF_INET])
snicstats = namedtuple('snicstats', ['isup'])
NICS_STAT = {'lo': snicstats(True)}

class HTTPServer:
    def __init__(self, body, content_length=None, transfer_encoding=None,
                 start_line=None, wait_closed=None):
        self.body = body.encode()
        self.body_length = len(self.body)
        self.wait_closed = wait_closed

        header = ['HTTP/1.1 200 OK' if start_line is None else
                  start_line]
        if content_length is not None:
            header.append(f'Content-Length: {content_length}')

        if transfer_encoding is not None:
            header.append(f'Transfer-Encoding: {transfer_encoding}')
        header.extend(['', ''])
        self.header = '\r\n'.join(header).encode('ascii')

        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    @log_unhandled_exception(logger)
    async def client_connected(self, reader, writer):
        """Handle an HTTP GET request and return the response."""

        peername = writer.get_extra_info('peername')
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()

            # Write the response.
            writer.write(self.header)

            # Hang for ever.
            if self.wait_closed:
                await writer.drain()
                await writer.wait_closed()

            if self.body_length:
                writer.write(self.body)
        finally:
            try:
                await writer.drain()
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def run(self):
        aio_server = await asyncio.start_server(self.client_connected,
                                                HOST, HTTP_PORT)
        async with aio_server:
            self.startup.set_result(None)
            await aio_server.serve_forever()

class AddrFromNics(TestCase):
    """network.ipaddr_from_nics() tests."""

    def test_cfg_skip_loopback_true(self):
        # Test that '127.0.0.1' is returned when 'lo' is configured, even
        # though 'skip_loopback' is true.
        nics_addr = {'lo': [snicaddr('127.0.0.1', '255.0.0.0')]}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_addrs.return_value = nics_addr
            net_if_stats.return_value = NICS_STAT
            result = list(ipaddr_from_nics(['lo'], skip_loopback=True))
            self.assertEqual(result, ['127.0.0.1'])

    def test_cfg_skip_loopback_false(self):
        # Test that '127.0.0.1' is returned when 'lo' is not configured
        # and 'skip_loopback' is false.
        nics_addr = {'lo': [snicaddr('127.0.0.1', '255.0.0.0')]}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_stats.return_value = NICS_STAT
            net_if_addrs.return_value = nics_addr
            result = list(ipaddr_from_nics(['lo'], skip_loopback=False))
            self.assertEqual(result, ['127.0.0.1'])

    def test_nocfg_skip_loopback_true(self):
        # Test that '127.0.0.1' is ignored when 'lo' is not configured
        # and 'skip_loopback' is true.
        nics_addr = {'lo': [snicaddr('127.0.0.1', '255.0.0.0')]}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_stats.return_value = NICS_STAT
            net_if_addrs.return_value = nics_addr
            result = list(ipaddr_from_nics([], skip_loopback=True))
            self.assertEqual(result, [])

    def test_nocfg_skip_loopback_false(self):
        # Test that '127.0.0.1' is returned when 'lo' is not configured
        # and 'skip_loopback' is false.
        nics_addr = {'lo': [snicaddr('127.0.0.1', '255.0.0.0')]}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_addrs.return_value = nics_addr
            net_if_stats.return_value = NICS_STAT
            result = list(ipaddr_from_nics([], skip_loopback=False))
            self.assertEqual(result, ['127.0.0.1'])

    def test_no_netmask(self):
        nics_addr = {'lo': [snicaddr('127.0.0.1', None)]}
        with mock.patch.object(psutil,'net_if_addrs') as net_if_addrs,\
                mock.patch.object(psutil, 'net_if_stats') as net_if_stats:
            net_if_addrs.return_value = nics_addr
            net_if_stats.return_value = NICS_STAT
            result = list(ipaddr_from_nics(['lo'], as_string=False))
            self.assertEqual(result, [IPv4Address('127.0.0.1')])

class SSDP_notify(IsolatedAsyncioTestCase):
    """SSDP notify test cases.

    These tests use the fact that multicast datagrams sent to the loopback
    interface are looped back to the notify task.
    """

    async def test_ssdp_notify(self):
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            await loopback_datagrams([SSDP_ALIVE],
                                     patch_method='_create_root_device')

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', SSDP_ALIVE))

    async def test_membership_OSError(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            try:
                notify = Notify(None, set())
                notify.manage_membership(set(['256.0.0.0']))
            finally:
                notify.sock.close()

        self.assertTrue(search_in_logs(m_logs.output, 'network',
            re.compile(r'256\.0\.0\.0 cannot be member of 239.255.255.250')))

    async def test_failed_memberships(self):
        try:
            ip = '127.0.0.1'
            notify = Notify(None, set())
            with self.assertLogs(level=logging.DEBUG) as m_logs:
                with mock.patch.object(notify, 'sock') as sock:
                    sock.setsockopt.side_effect = OSError()
                    notify.manage_membership(set([ip]))
                    self.assertEqual(notify.failed_memberships, {ip})

                notify.manage_membership(set([ip]))
                self.assertEqual(notify.failed_memberships, set())
        finally:
            notify.sock.close()

    async def test_notify_OSError(self):
        async def setup(control_point):
            patcher = mock.patch.object(control_point, '_process_ssdp')
            proc_ssdp = patcher.start()
            proc_ssdp.side_effect = OSError(err_msg)

        err_msg = 'Exception raised by _process_ssdp'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            control_point = await loopback_datagrams([SSDP_ALIVE],
                                                     setup=setup)
            # Wait until completion of the notify task.
            try:
                for task in control_point._upnp_tasks:
                    if task.get_name() == 'ssdp notify':
                        break
                else:
                    raise AssertionError(f'SSDP notify task not found')
                await asyncio.wait_for(task, 1)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                self.fail('Notify task did not terminate as expected')

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                       re.compile('on_con_lost.*done')))
        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                    re.compile(fr'OSError\({err_msg!r}\)')))

    async def test_invalid_field(self):
        field = 'invalid NTS field'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await loopback_datagrams(
                            [SSDP_NOTIFY.format(nts=field, **SSDP_PARAMS),
                            SSDP_ALIVE], patch_method='_create_root_device')

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                            re.compile(f'malformed HTTP header:\n.*{field}')))

    async def test_no_NTS_field(self):
        not_nts = 'FOO: dummy field name'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await loopback_datagrams(
                            [SSDP_NOTIFY.format(nts=not_nts, **SSDP_PARAMS),
                             SSDP_ALIVE], patch_method='_create_root_device')

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                        re.compile(f'missing "NTS" field')))

class SSDP_msearch(IsolatedAsyncioTestCase):
    """SSDP msearch test cases."""

    @staticmethod
    def _sendto_coro(datagram):
        """Return a coroutine to send a datagram using a socket.

        The datagram is received by the MsearchServerProtocol instance.
        """

        async def _get_result(protocol):
            return protocol.get_result()

        async def send_datagram(ip, protocol):
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setblocking(False)
                sock.sendto(datagram.encode(),
                            (HOST, MSEARCH_PORT))

                # With python 3.12 the tests calling this function fail unless
                # we give back control twice to the asyncio loop.
                await asyncio.sleep(0)
                await asyncio.sleep(0)

                try:
                    return await asyncio.wait_for(_get_result(protocol), 1)
                except TimeoutError:
                    raise AssertionError ('The sent datagram has not been'
                                          ' received')
        return send_datagram

    async def test_ssdp_msearch(self):
        async def _msearch(ip, protocol):
            await msearch(ip, protocol, msearch_count=1, msearch_interval=0,
                          mx=0)

        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await loopback_datagrams(_msearch)

        self.assertTrue(search_in_logs(m_logs.output, 'network', re.compile(
            r"Sent 1 M-SEARCH datagrams to \('239\.255\.255\.250', 1900\)")))

    async def test_ssdp_socket_msearch(self):
        coro = self._sendto_coro(MSEARCH_RESPONSE)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            await loopback_datagrams(coro,
                                     patch_method='_create_root_device')

        self.assertTrue(find_in_logs(m_logs.output, 'upnp', MSEARCH_RESPONSE))

    async def test_bad_start_line(self):
        coro = self._sendto_coro(NOT_FOUND)
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            await loopback_datagrams(coro)

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{NOT_FOUND.splitlines()[0]}' request")))

    async def test_not_root_device(self):
        device = 'urn:schemas-upnp-org:device:MediaServer:1'
        st = f'ST: {device}'
        coro = self._sendto_coro(SSDP_MSEARCH.format(st=st))
        with self.assertLogs(level=TEST_LOGLEVEL) as m_logs:
            await loopback_datagrams(coro)

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                re.compile(f"Ignore '{device}': non root device")))

    async def test_invalid_ip(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            await send_mcast('256.0.0.0', 0)

        self.assertTrue(search_in_logs(m_logs.output, 'network',
                                re.compile(r'Cannot bind.*256\.0\.0\.0')))

class HttpQuery(IsolatedAsyncioTestCase):
    """Http test cases."""

    @staticmethod
    async def _loopback_get(body, content_length=None, transfer_encoding=None,
                            start_line=None, wait_closed=None):
        """Start the http server and send the query."""

        http_server = HTTPServer(body, content_length, transfer_encoding,
                                                        start_line, wait_closed)
        asyncio.create_task(http_server.run())
        await http_server.startup
        return await http_get(URL)

    @staticmethod
    async def _loopback_soap(body, start_line=None):
        """Start the http server and send the query."""

        soap_body = b'The soap action'
        soap_header = [('Content-length', str(len(soap_body)))]
        http_server = HTTPServer(body, start_line=start_line)
        asyncio.create_task(http_server.run())
        await http_server.startup
        return await asyncio.wait_for(http_soap(URL, soap_header, soap_body),
                                      1)

    async def test_http_get(self):
        body = 'Some content.'
        received_body = await self._loopback_get(body)

        self.assertEqual(body, received_body.decode())

    async def test_http_1_0_response(self):
        body = 'Some content.'
        start_line = 'HTTP/1.0 200 OK'
        received_body = await self._loopback_get(body, start_line=start_line)

        self.assertEqual(body, received_body.decode())

    async def test_zero_length(self):
        body = 'Some content.'
        with self.assertLogs(level=logging.DEBUG) as m_logs, \
                self.assertRaises(UPnPInvalidHttpError) as cm:
            await self._loopback_get(body, content_length=0)

        self.assertIn("h11.RemoteProtocolError", cm.exception.args[0])

    async def test_http_request_timeout(self):

        body = 'Some content'
        timeout = 0.5
        with self.assertLogs(level=logging.DEBUG) as m_logs, \
                mock.patch.object(network_module, 'TIMEOUT', timeout), \
                self.assertRaises(UPnPHttpError) as cm:
            res= await self._loopback_get(body, wait_closed=True)

        self.assertIn(f"Task '_http_request()' has been hanging for {timeout}"
                      f' seconds', cm.exception.args[0])

    async def test_empty_body(self):
        body = ''
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            body = await self._loopback_get(body)

        self.assertEqual(body, b'')

    async def test_bad_http_version(self):
        body = 'Some content.'
        start_line = 'HTTP/2.0 200 OK'
        with self.assertRaises(UPnPInvalidHttpError) as cm:
            await self._loopback_get(body, start_line=start_line)

        self.assertIn("http_version=b'2.0'", cm.exception.args[0])

    async def test_transfer_encoding(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs, \
                self.assertRaises(UPnPInvalidHttpError) as cm:
            body = 'Some content.'
            await self._loopback_get(body, transfer_encoding='deflate')

        self.assertIn("h11.RemoteProtocolError", cm.exception.args[0])

    async def test_http_soap(self):
        body = 'soap response'
        is_fault, response_body = await self._loopback_soap(body)

        self.assertEqual(body, response_body.decode())
        self.assertFalse(is_fault)

    async def test_soap_fault(self):
        body = 'soap response'
        is_fault, response_body = await self._loopback_soap(body,
                            start_line='HTTP/1.0 500 Internal Server Error')

        self.assertEqual(body, response_body.decode())
        self.assertTrue(is_fault)

    async def test_bad_soap(self):
        body = 'soap response'
        start_line = 'HTTP/2.0 200 OK'
        with self.assertRaises(UPnPInvalidHttpError) as cm:
            is_fault, body = await self._loopback_soap(body,
                                                    start_line=start_line)

        self.assertIn("http_version=b'2.0'", cm.exception.args[0])

    async def test_chunked_body(self):
        body = '4\r\nabcd\r\n0\r\n\r\n'
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            body = await self._loopback_get(body, transfer_encoding='chunked')
        self.assertEqual(body, b'abcd')

if __name__ == '__main__':
    unittest.main(verbosity=2)
