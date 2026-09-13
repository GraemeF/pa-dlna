"""Networking utilities."""

import asyncio
import socket
import h11
import struct
import time
import re
import io
import logging
import urllib.parse
import psutil
from ipaddress import IPv4Interface, IPv4Address

from . import UPnPError, TEST_LOGLEVEL
from .util import log_exception

logger = logging.getLogger('network')

MCAST_GROUP = '239.255.255.250'
MCAST_PORT = 1900
MCAST_ADDR = (MCAST_GROUP, MCAST_PORT)
UPNP_ROOTDEVICE = 'upnp:rootdevice'

MSEARCH_COUNT= 3                        # number of MSEARCH requests each time
MSEARCH_INTERVAL = 0.2                  # sent at seconds intervals
MX = 2                                  # seconds to delay response

MSEARCH = '\r\n'.join([
        f'M-SEARCH * HTTP/1.1',
        f'HOST: {MCAST_GROUP}:{MCAST_PORT}',
        f'MAN: "ssdp:discover"',
        f'ST: {UPNP_ROOTDEVICE}',
        f'MX: {MX}',
        f'',
        f'',
        ])

TIMEOUT = 5

# Chunked transfer encoding.
HTTP_CHUNK_SIZE = 512
SEP = b'\r\n'
CHUNK_EXT = b';'
HEXDIGITS = re.compile(b'[0-9a-fA-F]+')

class ChunkState:
    PARSE_CHUNKED_SIZE = 0
    PARSE_CHUNKED_CHUNK = 1
    PARSE_CHUNKED_CHUNK_EOF = 2
    PARSE_MAYBE_TRAILERS = 3
    PARSE_TRAILERS = 4

class UPnPInvalidSsdpError(UPnPError): pass
class UPnPHttpError(UPnPError): pass
class UPnPInvalidHttpError(UPnPHttpError): pass
class UPnPNoHTTPResponseError(UPnPHttpError): pass

# Networking helper functions.
def ipaddr_from_nics(nics, skip_loopback=False, as_string=True):
    """Yield the IPv4 addresses of NICS in the UP state.

    Use all existing network interface when 'nics' is empty, except the
    loopback interface when 'skip_loopback' is true.
    """

    # Get the IP addresses of each NIC in the UP state.
    all_nics = {}
    nics_stats = psutil.net_if_stats()
    for nic, val in psutil.net_if_addrs().items():
        if nic in nics_stats and nics_stats[nic].isup:
            all_nics[nic] = val

    for nic in filter(lambda x:
                      not nics and (not skip_loopback or x != 'lo') or
                      x in nics, all_nics):
        for addr in filter(lambda x:
                           x.family == socket.AF_INET, all_nics[nic]):
            if addr.netmask is not None:
                ip_addr = IPv4Interface(f'{addr.address}/{addr.netmask}')
                if ip_addr.network.prefixlen != 32:
                    yield addr.address if as_string else ip_addr
            else:
                yield addr.address if as_string else IPv4Address(addr.address)

def http_header_as_dict(header):
    """Return the http header as a dict."""

    def normalize(args):
        """Return a normalized (key, value) tuple."""
        return args[0].strip().upper(), args[1].strip()

    # RFC 2616 (obsoleted) section 4.2: Header fields can be extended over
    # multiple lines by preceding each extra line with at least one SP or HT.
    # But see RFC 7230 section 3.2.4: A server that receives an obs-fold ...
    # [may] replace each received obs-fold with one or more SP octets.
    compacted = ''
    for line in header:
        sep = '' if not compacted or line.startswith((' ', '\t')) else '\n'
        compacted = sep.join((compacted, line))

    try:
        return dict(normalize(line.split(':', maxsplit=1))
                    for line in compacted.splitlines())
    except (ValueError, IndexError):
        raise UPnPInvalidSsdpError(f'malformed HTTP header:\n{header}')

def check_ssdp_header(header, is_msearch):
    """Check the SSDP header."""

    def exist(keys):
        for key in keys:
            if key not in header:
                raise UPnPInvalidSsdpError(
                    f'missing "{key}" field in SSDP notify:\n{header}')

    # Check the presence of some required keys.
    if is_msearch:
        exist(('ST', 'LOCATION', 'USN'))
    else:
        exist(('NT', 'NTS', 'USN'))
        if header['NTS'] in ('ssdp:alive', 'ssdp:update'):
            exist(('LOCATION',))

def parse_ssdp(datagram, peer_ipaddress, is_msearch):
    """Return None when ignoring the SSDP, otherwise return a dict."""

    req_line = 'HTTP/1.1 200 OK' if is_msearch else 'NOTIFY * HTTP/1.1'

    # Ignore non 'notify' and non 'msearch' SSDPs.
    header = datagram.decode().splitlines()
    start_line = header[:1]
    if not start_line or start_line[0].strip() != req_line:
        if start_line:
            logger.log(TEST_LOGLEVEL,
                       f"Ignore '{start_line[0].strip()}' request")
        return None

    # Parse the HTTP header as a dict.
    try:
        header = http_header_as_dict(header[1:])
        check_ssdp_header(header, is_msearch)
    except UPnPInvalidSsdpError as e:
        logger.warning(f'Error from {peer_ipaddress}: {e}')
        return None

    # Ignore non root device responses.
    _type = header['ST'] if is_msearch else header['NT']
    if _type != UPNP_ROOTDEVICE:
        logger.log(TEST_LOGLEVEL, f"Ignore '{_type}': non root device")
        return None

    return header

async def msearch(ip, protocol, msearch_count=MSEARCH_COUNT,
                  msearch_interval=MSEARCH_INTERVAL, mx=MX):
    """Implement the SSDP search protocol on the 'ip' network interface.

    Return the list of received (data, peer_addr, local_addr).
    """

    expire = time.monotonic() + mx

    for i in range(msearch_count):
        await asyncio.sleep(msearch_interval)
        if not protocol.closed():
            protocol.send_datagram(MSEARCH)
        else:
            break
    logger.debug(f'Sent {i + 1} M-SEARCH datagrams to {MCAST_ADDR} from {ip}')

    if not protocol.closed():
        remain = expire - time.monotonic()
        if remain > 0:
            await asyncio.sleep(expire - time.monotonic())

    return  protocol.get_result()

async def send_mcast(ip, port, ttl=2, coro=msearch):
    """Send multicast datagrams.

    'coro' is a coroutine *function* and when invoked, the coroutine is
    awaited with the 'protocol' end point as parameter for sending and
    receiving datagrams.
    """

    # Create the socket.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)

    try:
        # Prevent multicast datagrams to be looped back to ourself.
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

        try:
            sock.bind((ip, port))
        except OSError as e:
            # Just log the exception, the associated network interface may
            # be reconnected later.
            logger.debug(f'Cannot bind to IP address {ip}: {e!r}')
            return

        # Start the server.
        transport = None
        try:
            loop = asyncio.get_running_loop()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: MsearchServerProtocol(ip), sock=sock)

            # Prepare the socket for sending from the network
            # interface of 'ip'.
            sock.setsockopt(socket.SOL_IP, socket.IP_MULTICAST_IF,
                            socket.inet_aton(ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                            ttl)

            return await coro(ip, protocol)

        finally:
            if transport is not None:
                transport.close()
    finally:
        # Needed when OSError is raised upon binding the socket.
        sock.close()

def trim_bytes_to_string(source, size=80):
    text = source.decode("ascii", "surrogateescape")
    trailer = '...' if len(text) > size else ''
    return text[:size] + trailer

async def http_request(url, headers=None, post_body=None):
    """An HTTP 1.0 GET or POST request.

    headers:    list of tuples [(header name, header value), ...]
    post_body:  bytes object
    """

    async def _http_request():
        async with HttpClient(url) as client:
            # Send the request.
            method = 'POST' if post_body else 'GET'
            await client.send_request(method, headers, post_body)

            # Parse the http response.
            return await client.receive_response()

    try:
        return await asyncio.wait_for(_http_request(), TIMEOUT)
    except TimeoutError:
        raise UPnPHttpError(f"Task '_http_request()' has been hanging"
                            f' for {TIMEOUT} seconds')

async def http_get(url):
    """An HTTP 1.0 GET request."""

    response, body, host = await http_request(url)

    if response.status_code != 200 or response.http_version > b'1.1':
        raise UPnPInvalidHttpError(f"Response={response}, Body={body}"
                                   f" from {host}")
    return body

async def http_soap(url, headers, body):
    """HTTP 1.0 POST request used to submit a SOAP action."""

    response, body, host = await http_request(url, headers, body)

    if response.http_version <= b'1.1':
        is_fault = True
        if response.status_code == 200:
            return not is_fault, body

        # HTTP/1.0 500 Internal Server Error.
        elif response.status_code == 500:
            return is_fault, body

    raise UPnPInvalidHttpError(f"Response={response}, Body={body}"
                               f" from {host}")

# Classes.
class Notify:
    """Implement the SSDP advertisement protocol.

    See section 21.10 Sending and Receiving in
    "Network Programming Volume 1, Third Edition" Stevens et al.
    See also section 5.10.2 Receiving IP Multicast Datagrams
    in "An Advanced 4.4BSD Interprocess Communication Tutorial".
    """

    def __init__(self, process_datagram, ip_addresses):
        self.process_datagram = process_datagram
        self.failed_memberships = set()

        # Create the socket.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)

        self.manage_membership(ip_addresses)

        # Future used by the test suite.
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    def close(self):
        self.sock.close()

    def manage_membership(self, new_ips, stale_ips=None):
        def member(ip, option):
            msg = ('member of' if option == socket.IP_ADD_MEMBERSHIP else
                   'dropped from')
            try:
                mreq = struct.pack('4s4s', socket.inet_aton(MCAST_GROUP),
                                   socket.inet_aton(ip))
                self.sock.setsockopt(socket.IPPROTO_IP, option, mreq)
                logger.debug(f'SSDP notify: {ip} {msg} multicast group'
                             f' {MCAST_GROUP}')
                if (option == socket.IP_ADD_MEMBERSHIP and
                        ip in self.failed_memberships):
                    self.failed_memberships.remove(ip)
            except OSError as e:
                # Log the warning only once.
                if (option == socket.IP_ADD_MEMBERSHIP and
                        ip not in self.failed_memberships):
                    logger.warning(f'SSDP notify: {ip} cannot be {msg}'
                                   f' {MCAST_GROUP}: {e!r}')
                    self.failed_memberships.add(ip)
                return False
            return True

        for ip in new_ips:
            member(ip, socket.IP_ADD_MEMBERSHIP)

        if stale_ips is not None:
            for ip in stale_ips:
                member(ip, socket.IP_DROP_MEMBERSHIP)

    async def run(self):
        # Allow other processes to bind to the same multicast group and port.
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Bind to the multicast (group, port).
        # Binding to (INADDR_ANY, port) would also work, except
        # that in that case the socket would also receive the datagrams
        # destined to (any other address, MCAST_PORT).
        self.sock.bind(MCAST_ADDR)

        # Start the server.
        transport = None
        try:
            loop = asyncio.get_running_loop()
            on_con_lost = loop.create_future()
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: NotifyServerProtocol(self.process_datagram,
                                             on_con_lost),
                sock=self.sock)
            self.startup.set_result(None)
            await on_con_lost
            logger.debug("Future 'on_con_lost' is done.")
        finally:
            # Drop multicast group membership for all IP addresses.
            self.manage_membership(set())
            if transport is not None:
                transport.close()
            logger.info('End of the SSDP notify task')

# Network protocols.
class HttpClient:
    def __init__(self, url):
        urlobj = urllib.parse.urlsplit(url)
        self.host = urlobj.hostname
        self.port = urlobj.port if urlobj.port is not None else 80
        self.netloc = urlobj.netloc
        self.target = urlobj._replace(scheme='')._replace(netloc='').geturl()
        self.conn = h11.Connection(our_role=h11.CLIENT)

    async def send_request(self, method, headers=None, body=None):
        if headers is None:
            headers = []
        headers.extend([('Host', self.netloc), ('Connection', 'close')])

        request = h11.Request(method=method, target=self.target,
                                                        headers=headers)
        data = self.conn.send(request)
        self.writer.write(data)

        if body:
            data = self.conn.send(h11.Data(data=body))
            self.writer.write(data)

        data = self.conn.send(h11.EndOfMessage())
        if self.conn.our_state != h11.MUST_CLOSE:
            logger.error(f'HttpClient.send_request() our_state'
                           f' ({self.conn.our_state}) != MUST_CLOSE')

        self.writer.write(data)
        await self.writer.drain()

    async def receive_response(self):
        response = None
        body = bytearray()
        while True:
            event = self.conn.next_event()

            if event is h11.NEED_DATA:
                data = await self.reader.read(512)
                self.conn.receive_data(data)

            elif isinstance(event, h11.Response):
                response = event

            elif isinstance(event, h11.Data):
                body.extend(event.data)

            elif isinstance(event, h11.EndOfMessage):
                # Tell the h11 state machine that SERVER is closed.
                self.conn.receive_data(b'')
                continue

            elif isinstance(event, h11.ConnectionClosed):
                # Tell the h11 state machine that CLIENT is closed.
                # SERVER is closed in the EndOfMessage event that
                # triggers the ConnectionClosed event.
                self.conn.send(h11.ConnectionClosed())
                if self.conn.states != {h11.CLIENT: h11.CLOSED,
                                        h11.SERVER: h11.CLOSED}:
                    logger.error(f'HttpClient.receive_response()'
                                   f'CLIENT and SERVER are not both CLOSED')
                break

            else:
                logger.error(f'HttpClient.receive_response()'
                             f' unknown h11 event: {event}')

        return response, body, self.host

    async def __aenter__(self):
        self.reader, self.writer = await asyncio.open_connection(
                                                    self.host, self.port)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except ConnectionError:
            pass

        if isinstance(exc_value, h11.ProtocolError):
            raise UPnPInvalidHttpError(f'h11.{exc_value!r}') from None

class MsearchServerProtocol:
    """The MSEARCH asyncio server."""

    def __init__(self, ip):
        self.ip = ip
        self.transport = None
        self._result = []     # list of received (data, peer_addr, local_addr)
        self._closed = None

    def connection_made(self, transport):
        self.transport = transport
        self._closed = False

    def datagram_received(self, data, peer_addr):
        local_addr = self.transport.get_extra_info('sockname')
        self._result.append((data, peer_addr[0], local_addr[0]))

    def error_received(self, exc):
        logger.warning(f'Error received on {self.ip} by'
                       f' MsearchServerProtocol: {exc}')
        self.transport.abort()

    def connection_lost(self, exc):
        if exc:
            logger.debug(f'Connection lost on {self.ip} by'
                         f' MsearchServerProtocol: {exc!r}')
        self._closed = True

    def send_datagram(self, message):
        try:
            self.transport.sendto(message.encode(), MCAST_ADDR)
        except Exception as e:
            self.error_received(e)

    def get_result(self):
        return self._result

    def closed(self):
        return self._closed

class NotifyServerProtocol:
    """The NOTIFY asyncio server."""

    def __init__(self, process_datagram, on_con_lost):
        self.process_datagram = process_datagram
        self.on_con_lost = on_con_lost

    def connection_made(self, transport):
        pass

    def datagram_received(self, data, addr):
        try:
            self.process_datagram(data, addr[0], None)
        except Exception as exc:
            if not self.on_con_lost.done():
                self.on_con_lost.set_result(True)
            self.error_received(exc)

    def error_received(self, exc):
        log_exception(logger,
                      f'Error received by NotifyServerProtocol: {exc!r}')

    def connection_lost(self, exc):
        if exc:
            logger.warning(f'Connection lost by NotifyServerProtocol: {exc!r}')
