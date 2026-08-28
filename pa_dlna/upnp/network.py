"""Networking utilities."""

import asyncio
import socket
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
class UPnPInvalidHttpError(UPnPError): pass
class UPnPNoHTTPResponseError(UPnPError): pass

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

async def parse_chunked_body(reader, url='', http_chunk_size=HTTP_CHUNK_SIZE):
    """Parse a chunked encoding body.

    This is mostly code from aiohttp.htt_parser.HttpPayloadParser after fixing
    issue https://github.com/aio-libs/aiohttp/issues/10355.
    """

    state = ChunkState.PARSE_CHUNKED_SIZE
    chunk_size = 0
    chunk_tail = b''
    body = io.BytesIO()

    # RFC 2616 https://datatracker.ietf.org/doc/html/rfc2616#section-3.6.1.
    while True:
        chunk = await reader.read(http_chunk_size)
        if chunk == b'':
            if state not in (ChunkState.PARSE_MAYBE_TRAILERS,
                             ChunkState.PARSE_TRAILERS):
                logger.warning(f"Missing last-chunk from '{url}'")
            if chunk_tail:
                tail = trim_bytes_to_string(chunk_tail)
                logger.warning(f'Trailing chunk from {url}: {tail}')
            break

        if chunk_tail:
            chunk = chunk_tail + chunk
            chunk_tail = b''

        while chunk:

            # Read next chunk size.
            if state == ChunkState.PARSE_CHUNKED_SIZE:
                pos = chunk.find(SEP)
                if pos >= 0:
                    # Strip chunk-extensions.
                    i = chunk.find(CHUNK_EXT, 0, pos)
                    size_b = chunk[:i] if i >= 0 else chunk[:pos]
                    size_b = size_b.strip()

                    if not re.fullmatch(HEXDIGITS, size_b):
                        size = trim_bytes_to_string(chunk[:pos])
                        raise UPnPInvalidHttpError(
                                    f'Not a chunk size: {size!r} from {url}')
                    size = int(size_b, 16)

                    chunk = chunk[pos+len(SEP):]
                    if size == 0:
                        state = ChunkState.PARSE_MAYBE_TRAILERS
                    else:
                        state = ChunkState.PARSE_CHUNKED_CHUNK
                        chunk_size = size
                else:
                    chunk_tail = chunk
                    break

            # Read the chunk.
            if state == ChunkState.PARSE_CHUNKED_CHUNK:
                required = chunk_size
                chunk_size = max(required - len(chunk), 0)
                body.write(chunk[:required])

                if chunk_size:
                    break
                chunk = chunk[required:]
                state = ChunkState.PARSE_CHUNKED_CHUNK_EOF

            # Toss the CRLF at the end of the chunk.
            if state == ChunkState.PARSE_CHUNKED_CHUNK_EOF:
                if chunk[:len(SEP)] == SEP:
                    state = ChunkState.PARSE_CHUNKED_SIZE
                    chunk = chunk[len(SEP):]
                else:
                    length = len(chunk)
                    if length and chunk[0] != SEP[0] or length > 1:
                        chunk = trim_bytes_to_string(chunk)
                        raise UPnPInvalidHttpError(
                            f'Missing CRLF at chunk end: {chunk!r} from {url}')
                    # Get the CRLF or the missing LF in the next chunk.
                    chunk_tail = chunk
                    break

            # If stream does not contain trailer, after 0\r\n
            # we should get another \r\n otherwise
            # trailers needs to be skipped until \r\n\r\n.
            if state == ChunkState.PARSE_MAYBE_TRAILERS:
                head = chunk[:len(SEP)]
                if head == SEP:
                    # End of stream.
                    break
                # Both CR and LF, or only LF may not be received yet. It is
                # expected that CRLF or LF will be shown at the very first
                # byte next time, otherwise trailers should come. The last
                # CRLF which marks the end of response might not be
                # contained in the same TCP segment which delivered the
                # size indicator.
                if not head or head == SEP[0]:
                    chunk_tail = head
                    break
                state = ChunkState.PARSE_TRAILERS

            # Read and discard trailer up to the CRLF terminator
            if state == ChunkState.PARSE_TRAILERS:
                pos = chunk.find(SEP)
                if pos >= 0:
                    chunk = chunk[pos+len(SEP):]
                    state = ChunkState.PARSE_MAYBE_TRAILERS
                else:
                    chunk_tail = chunk
                    break

    return body.getvalue()

async def http_request(url, headers=None, post_body=None):
    """An HTTP 1.0 GET or POST request.

    headers:    list of tuples [(header name, header value), ...]
    post_body:  bytes object
    """

    def headers_to_string(_tuple):
        return _tuple[0] + ': ' + _tuple[1]

    def get_from_dict(headers, name):
        names = {name.upper(), name.lower()}
        res = names.intersection(headers.keys())
        if len(res):
            # Should raise an exception if len(res) == 2.
            return headers[res.pop()]

    method = 'POST' if post_body else 'GET'
    writer = None
    try:
        urlobj = urllib.parse.urlsplit(url)
        host = urlobj.hostname
        port = urlobj.port if urlobj.port is not None else 80
        reader, writer = await asyncio.open_connection(host, port)

        # Send the request.
        target = urlobj._replace(scheme='')._replace(netloc='').geturl()
        request = f"{method} {target or '/'} HTTP/1.0\r\n"

        host_header = ('Host', urlobj.netloc)
        if headers is None:
            headers = [host_header]
        else:
            headers.append(('Host', urlobj.netloc))
        request += '\r\n'.join(map(headers_to_string, headers)) + '\r\n\r\n'

        writer.write(request.encode('latin-1'))
        if post_body:
            writer.write(post_body)
        await writer.drain()

        # Parse the http response.
        response = []
        while True:
            line = await reader.readline()
            if not line:
                break

            line = line.decode('latin1').rstrip()
            if line:
                response.append(line)
            else:
                break

        if not response:
            raise UPnPNoHTTPResponseError(f'No http response from {host}')

        header_dict = http_header_as_dict(response[1:])
        transfer_encoding = get_from_dict(header_dict, 'TRANSFER-ENCODING')
        if transfer_encoding is not None:
            if transfer_encoding.lower() == 'chunked':
                body = await parse_chunked_body(reader, url=url)
                return response, body, host
            else:
                logger.error(f"HTTP 1.0 does not support '{transfer_encoding}'"
                             f" Transfer-Encoding")
            return response, b'', host

        content_length = get_from_dict(header_dict, 'CONTENT-LENGTH')
        if content_length is not None:
            content_length = int(content_length)
            if content_length == 0:
                logger.warning(f'Got content_length = 0 from {url}')
                return response, b'', host

        body = await reader.read()

        # Check that we have received the whole body.
        if content_length is not None:
            if len(body) != content_length:
                raise UPnPInvalidHttpError(f'Content-Length and actual length'
                                f' mismatch ({content_length} != {len(body)})'
                                f' from {host}')
        if not body:
            logger.warning(f'Got empty body from {url}')
        return response, body, host

    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass

async def http_get(url):
    """An HTTP 1.0 GET request."""

    response, body, host = await http_request(url)
    start_line = response[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', start_line) is None:
        raise UPnPInvalidHttpError(f"Header={response}, Body={body}"
                                   f" from {host}")
    return body

async def http_soap(url, headers, body):
    """HTTP 1.0 POST request used to submit a SOAP action."""

    response, body, host = await http_request(url, headers, body)
    start_line = response[0]
    if re.match(r'HTTP/1\.(0|1) 200 ', start_line) is not None:
        is_fault = False
    # HTTP/1.0 500 Internal Server Error.
    elif re.match(r'HTTP/1\.(0|1) 500 ', start_line) is not None:
        is_fault = True
    else:
        raise UPnPInvalidHttpError(f"Header={response}, Body={body}"
                                   f" from {host}")
    return is_fault, body

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
