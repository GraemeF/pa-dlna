"""Http server test cases."""

import re
import asyncio
import logging
import h11
from http import HTTPStatus
from unittest import IsolatedAsyncioTestCase, mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, find_in_logs, search_in_logs
from .streams import (BLKSIZE, run_curl, new_renderer, play_track, Renderer,
                      ControlPoint)
from ..encoders import FFMpegEncoder, L16Encoder
from ..http_server import HTTPServer, Track, H11Wrapper

async def start_http_server(allow_from=True):
    renderer = await new_renderer('audio/mp3')

    # Start the http server.
    control_point = renderer.control_point
    http_server = HTTPServer(control_point,
                             renderer.root_device.local_ipaddress,
                             control_point.port)
    if allow_from:
        http_server.allow_from(renderer.root_device.peer_ipaddress)
    asyncio.create_task(http_server.run(), name='http_server')
    await http_server.startup

    # Tests using that function fail randomly on GitLab and Python 3.11 with
    # the curl error:
    #   CURLE_COULDNT_CONNECT (7) Failed to connect() to host or proxy.
    # When the http_server.startup future is done, the Server._start_serving()
    # method of asyncio's base_events module has added the socket to the
    # asyncio loop, but the http server is not yet ready to accept
    # connections. Therefore we wait some loop iterations.
    for i in range(10):
        await asyncio.sleep(0)

    return renderer

@requires_resources('curl')
class PlayTrackTests(IsolatedAsyncioTestCase):
    """Playing a track test cases."""

    async def test_play_mp3(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            assert not isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.stop_track()
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

        # Issue #5:
        # 'The parec command line length keeps increasing at each new track'.
        self.assertEqual(renderer.control_point.parec_cmd,
                         ControlPoint().parec_cmd)

    async def test_play_aiff(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.stop_track()
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_play_aiff_255(self):
        # Test that an FFMpegEncoder encoder exiting with an exit_status of
        # 255 is reported as 'Terminated'.
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['FFMpegEncoder', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/aiff', transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, FFMpegEncoder)
            await renderer.stream_sessions.processes.encoder_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                'Exit status of encoder process: Terminated'))
        self.assertTrue(find_in_logs(m_logs.output, 'encoder',
                                'encoder stub return_code: 255'))

    async def test_play_l16(self):
        # Test playing track with no encoder.
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            mime_type = 'audio/l16;rate=44100;channels=2'
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track(mime_type, transactions,
                                                   logs=m_logs)

            assert isinstance(renderer.encoder, L16Encoder)
            await renderer.stream_sessions.processes.parec_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_close_session(self):
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            transactions = ['ignore', 16 * BLKSIZE]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            await renderer.stream_sessions.close_session()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_partial_read(self):
        # Check use of IncompleteReadError in Track.write_track().
        with self.assertLogs(level=logging.DEBUG) as m_logs:
            data_size = 16 * BLKSIZE + 1
            transactions = ['dont_sleep', data_size]
            curl_task, renderer = await play_track('audio/mp3', transactions,
                                                   logs=m_logs)

            await renderer.stream_sessions.processes.encoder_task
            await renderer.stream_sessions.processes.close()
            returncode, length = await curl_task

        self.assertEqual(returncode, 0)
        self.assertEqual(length, sum(transactions[1:]))

    async def test_ConnectionError(self):
        async def no_more_processes():
            while True:
                if renderer.stream_sessions.processes is None:
                    break
                await asyncio.sleep(0.1)

        with mock.patch.object(Track, 'write_track') as wtrack,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            wtrack.side_effect = ConnectionError()
            curl_task, renderer = await play_track('audio/mp3',
                ['ignore', BLKSIZE], wait_for_completion=False, logs=m_logs)
            returncode, length = await curl_task

            await asyncio.wait_for(no_more_processes(), 1.0)

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                        re.compile('HTTP socket is closed: ConnectionError')))

    async def test_Exception(self):
        with mock.patch.object(Track, 'write_track') as wtrack,\
                self.assertLogs(level=logging.INFO) as m_logs:
            wtrack.side_effect = RuntimeError()
            curl_task, renderer = await play_track('audio/mp3',
                ['ignore', BLKSIZE], wait_for_completion=False, logs=m_logs)
            returncode, length = await curl_task

            # Sleep to let the logger in the log_unhandled_exception decorator
            # log the exception before asyncio termination. Otherwise the
            # log message is printed on stderr after the unittest test has
            # terminated. Using asyncSetUp() and asyncTearDown() does not help.
            await asyncio.sleep(0.5)

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                       re.compile(r'RuntimeError\(\)')))

    async def test_disable_with_encoder(self):
        with mock.patch.object(Renderer, 'disable_root_device') as disable,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            curl_task, renderer = await play_track('audio/mp3', ['OSError'],
                                                   logs=m_logs)
            returncode, length = await curl_task

            await renderer.stream_sessions.processes.encoder_task
            disable.assert_called_once()
            await renderer.close()

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     'Exit status of encoder process: 1'))

    async def test_disable_with_parec(self):
        with mock.patch.object(Renderer, 'disable_root_device') as disable,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            mime_type = 'audio/l16;rate=44100;channels=2'
            curl_task, renderer = await play_track(mime_type, ['OSError'],
                                                   logs=m_logs)

            await renderer.stream_sessions.processes.parec_task
            await renderer.close()
            returncode, length = await curl_task
            disable.assert_called_once()

        self.assertEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     'Exit status of parec process: 1'))

    async def test_shutdown_cancelled(self):
        task_name = 'track_test'
        with mock.patch.object(
                H11Wrapper, 'shutdown_and_clean_up') as h11_shutdown,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            h11_shutdown.side_effect = asyncio.CancelledError()
            h11_wrapper = H11Wrapper(None, None)
            track = Track(None, h11_wrapper, task_name)
            task = asyncio.create_task(track.shutdown())
            await asyncio.wait_for(task, timeout=1)

        self.assertTrue(find_in_logs(m_logs.output, 'http',
                        f'{task_name}: Got CancelledError at Track shutdown'))

@requires_resources('curl')
class HTTPServerTests(IsolatedAsyncioTestCase):
    """Http server test cases."""

    def skip_if_curl_cannot_connect(self, returncode):
        # Curl fails to connect to the http server under the following
        # conditions:
        #   - only when run with coverage.py
        #   - only on Python 3.11
        #   - on GitLab CI/CD
        if returncode == 7:
            self.skipTest('CURLE_COULDNT_CONNECT (7) Failed to connect() to'
                          ' host or proxy')

    async def test_not_allowed(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server(allow_from=False)

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertNotEqual(returncode, 0)
        self.assertEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                    re.compile('Discarded.*not allowed')))

    async def test_renderer_not_found(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()

            # Start curl.
            curl_task = asyncio.create_task(run_curl(
                                                renderer.current_uri + 'fff'))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                            re.compile('Cannot find a matching renderer')))

    async def test_http_version(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri,
                                                     http_version='http1.0'))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                                    re.compile('Http error: 505')))

    async def test_is_playing(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()
            renderer.stream_sessions.is_playing = True

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.skip_if_curl_cannot_connect(returncode)
        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
            re.compile('Cannot start DLNATest.* stream .already running')))

    async def test_None_nullsink(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()
            renderer.nullsink = None

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.skip_if_curl_cannot_connect(returncode)
        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(search_in_logs(m_logs.output, 'http',
                            re.compile('DLNATest.* temporarily disabled')))

    async def test_HEAD_method(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri,
                                                     extra_args=['--head']))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     'HTTP/1.1 HEAD request from 127.0.0.1'))

    async def test_POST_method(self):
        with self.assertLogs(level=logging.INFO) as m_logs:
            renderer = await start_http_server()

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri,
                                    extra_args=['--data-raw', 'Hello there']))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertTrue(find_in_logs(m_logs.output, 'http',
                                     f'Http error: {HTTPStatus.BAD_REQUEST}'))

    async def test_next_event_exception(self):
        with mock.patch.object(H11Wrapper, 'next_event') as next_event,\
                self.assertLogs(level=logging.DEBUG) as m_logs:
            next_event.side_effect = h11.RemoteProtocolError('Forbidden',
                                    error_status_hint=HTTPStatus.FORBIDDEN)
            renderer = await start_http_server()

            # Start curl.
            curl_task = asyncio.create_task(run_curl(renderer.current_uri,))
            returncode, length = await asyncio.wait_for(curl_task, timeout=1)

        self.assertEqual(returncode, 0)
        self.assertNotEqual(length, 0)
        self.assertTrue(find_in_logs(m_logs.output,
                'http', f'Http error: {HTTPStatus.FORBIDDEN} - Forbidden'))

if __name__ == '__main__':
    unittest.main(verbosity=2)
