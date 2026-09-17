"""Util test cases."""

import asyncio
import logging
import re
from unittest import TestCase
from http import HTTPStatus

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import search_in_logs, HTTPRequestHandler
from ..network import http_get, UPnPInvalidHttpError
from ..util import shorten, log_unhandled_exception, AsyncioTasks

logger = logging.getLogger('test')

HOST = '127.0.0.1'
PORT = 9999
URL = f'http://{HOST}:{PORT}/'

class HTTPServer:
    """HTTP server responding with status code: 404 (Not Found)."""

    def __init__(self, message, exception=None):
        self.message = message
        self.exception = exception
        loop = asyncio.get_running_loop()
        self.startup = loop.create_future()

    async def client_connected(self, reader, writer):
        peername = writer.get_extra_info('peername')
        try:
            handler = HTTPRequestHandler(reader, writer, peername)
            await handler.set_rfile()
            handler.handle_one_request()
            handler.send_error(HTTPStatus.NOT_FOUND, self.message)
        finally:
            await writer.drain()
            try:
                writer.close()
                await writer.wait_closed()
            except ConnectionError:
                pass

    @log_unhandled_exception(logger)
    async def run(self):
        try:
            aio_server = await asyncio.start_server(self.client_connected,
                                                    HOST, PORT)
            async with aio_server:
                self.startup.set_result(None)
                await aio_server.serve_forever()
        finally:
            if self.exception:
                raise self.exception

class Util(TestCase):
    """Util test cases."""

    @staticmethod
    async def _loopback_get(message, exception=None):
        http_server = HTTPServer(message, exception)
        asyncio.create_task(http_server.run())
        await http_server.startup
        await asyncio.wait_for(http_get(URL), 1)

    def test_shorten(self):
        tests = [
            ('123456789abcdef', '123...def'),
            ('123456789',       '123456789'),
            ('123',             '123'),
            ]
        for text, expected in tests:
            with self.subTest(text=text, expected=expected):
                self.assertEqual(shorten(text, head_len=3, tail_len=3),
                                 expected)

    def test_log_unhandled_exception(self):
        with self.assertRaises(UPnPInvalidHttpError),\
                self.assertLogs(level=logging.ERROR) as m_logs:
            asyncio.run(self._loopback_get('foo', OSError()))

        self.assertTrue(search_in_logs(m_logs.output, 'test',
            re.compile(r'Exception .* HTTPServer.run\(\):\n *OSError')))

    def test_asyncio_tasks(self):
        async def coro():
            http_server = HTTPServer('foo')
            tasks.create_task(http_server.run(), name=task_name)
            self.assertEqual(list(t.get_name() for t in tasks), [task_name])

        task_name = 'http server'
        tasks = AsyncioTasks()
        asyncio.run(coro())
        self.assertEqual(list(t.get_name() for t in tasks), [])

    def test_http_logs(self):
        message = 'Le temps des cerises'
        with self.assertRaises(UPnPInvalidHttpError),\
                self.assertLogs(level=logging.ERROR) as m_logs:
            asyncio.run(self._loopback_get(message))

        self.assertTrue(search_in_logs(m_logs.output, 'tests',
                                       re.compile(message)))

if __name__ == '__main__':
    unittest.main(verbosity=2)
