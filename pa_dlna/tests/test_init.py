"""Command line test cases."""

import sys
import os
import io
import struct
import ipaddress
import logging
import unittest
import tempfile
import pathlib
try:
    import termios
    import pty
except ImportError:
    pass
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import requires_resources, LoggingTestCase, setUpModule
from ..init import parse_args, padlna_main, UPnPApplication, disable_xonxoff
from ..encoders import Encoder
from ..config import user_config_pathname

# b'\x13' is the Stop character <Ctrl-S>.
# b'\x11' is the Start character <Ctrl-Q>.
XON_XOFF_BYTES_ARRAY = b'A \x13 \x11 B\n'

def echo_xonxoff_array():
    txt = sys.stdin.readline()
    print(txt, end='')

    # When XON/XOFF is enabled, the Stop and Start characters are not passed
    # as input and the following condition evaluates to True.
    if txt.encode() != XON_XOFF_BYTES_ARRAY:
        sys.exit(1)

@requires_resources('os.devnull')
class Init(LoggingTestCase):
    def test_python_version(self):
        import sys
        import importlib
        import pa_dlna
        from .. import MIN_PYTHON_VERSION

        version = (MIN_PYTHON_VERSION[0], MIN_PYTHON_VERSION[1] - 1)
        try:
            with mock.patch.object(sys, 'version_info', version),\
                 redirect_stderr(io.StringIO()) as output,\
                 self.assertRaises(SystemExit) as cm:

                pa_dlna = importlib.reload(pa_dlna)

            self.assertEqual(cm.exception.args[0], 1)
            self.assertRegex(output.getvalue(),
                             f'^error.*{MIN_PYTHON_VERSION}')
        finally:
            pa_dlna = importlib.reload(pa_dlna)

    @requires_resources('pty')
    def test_disable_xonxoff(self):
        # Spawn a process to test disable_xonxoff().

        pid, master_fd = pty.fork()
        if pid == 0:
            module = 'pa_dlna.tests.test_init'
            argv = [sys.executable, '-c',
                    f'import {module}; {module}.echo_xonxoff_array()']
            os.execl(argv[0], *argv)

        # Note that the pty module imports the tty module that imports the
        # termios module. So it is safe to assume that if pty is available,
        # then termios is also available and 'restore_termios' is not None.
        restore_termios = disable_xonxoff(master_fd)
        old_attr = None
        try:
            # No ECHO and do not map NL to CR NL on output.
            old_attr = termios.tcgetattr(master_fd)
            new_attr = termios.tcgetattr(master_fd)
            new_attr[1] = new_attr[1] & ~termios.ONLCR
            new_attr[3] = new_attr[3] & ~termios.ECHO
            termios.tcsetattr(master_fd, termios.TCSANOW, new_attr)

            os.write(master_fd, XON_XOFF_BYTES_ARRAY)
            data = os.read(master_fd, 1024)
            status = os.waitpid(pid, 0)[1]
            self.assertEqual(data, XON_XOFF_BYTES_ARRAY)
            self.assertEqual(status, 0)
        finally:
            if old_attr is not None:
                termios.tcsetattr(master_fd, termios.TCSANOW, old_attr)
            if restore_termios:
                restore_termios()
            os.close(master_fd)

    def test_libpulse_version(self):
        from ..init import require_libpulse_version

        min_version = '9999'
        with self.assertRaises(SystemExit) as cm:
            require_libpulse_version(min_version)
        self.assertRegex(cm.exception.args[0],
                    f"libpulse version '{min_version}' .* required")

@requires_resources('os.devnull')
class Argv(LoggingTestCase):
    """Command line tests."""

    def test_no_args(self):
        options, _ = parse_args(self.__doc__, argv=[])
        self.assertEqual(options, {'dump_default': False,
                                   'dump_internal': False,
                                   'ip_addresses': [],
                                   'log_aio': False,
                                   'logfile': None,
                                   'loglevel': None,
                                   'msearch_interval': 60,
                                   'msearch_port': 0,
                                   'nics': [],
                                   'nolog_upnp': False,
                                   'port': 8080,
                                   'systemd': False,
                                   'test_devices': [],
                                   'ttl': b'\x02',
                                   'clients_uuids': None,
                                   'applications': None})

    def test_ip_addresses(self):
        options, _ = parse_args(self.__doc__,
                                argv=['--ip-addresses', '192.168.0.1'])
        self.assertEqual(options['ip_addresses'], ['192.168.0.1'])

    def test_invalid_ip_addresses(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__,
                                argv=['--ip-addresses', '192.168.0.999'])
        self.assertEqual(cm.exception.args[0], 2)
        self.assertTrue(isinstance(cm.exception.__context__,
                                   ipaddress.AddressValueError))

    def test_ttl(self):
        options, _ = parse_args(self.__doc__, argv=['--ttl', '255'])
        self.assertEqual(options['ttl'], b'\xff')

    def test_invalid_ttl(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--ttl', '256'])
        self.assertEqual(cm.exception.args[0], 2)
        self.assertTrue(isinstance(cm.exception.__context__, struct.error))

    def test_mtypes(self):
        options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                ',,audio/mp3,,audio/mpeg'])
        self.assertEqual(options['test_devices'], ['audio/mp3', 'audio/mpeg'])

    def test_same_mtypes(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                    'audio/mp3, audio/mp3'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_invalid_mtypes(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--test-devices',
                                                          'foo/mp3'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_two_dumps(self):
        with self.assertRaises(SystemExit) as cm:
            options, _ = parse_args(self.__doc__, argv=['--dump-default',
                                                          '--dump-internal'])
        self.assertEqual(cm.exception.args[0], 2)

    def test_not_writable(self):
        with mock.patch('builtins.open', mock.mock_open()) as m_open,\
                redirect_stderr(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:

            m_open.side_effect = OSError
            options, _ = parse_args(self.__doc__,
                                    argv=['--clients-uuids', 'foo'])

        m_open.assert_called_once()
        self.assertEqual(cm.exception.args[0], 2)
        self.assertRegex(output.getvalue(),
                         'pa-dlna: error: foo is not writable: OSError()')

    def test_not_readable(self):
        if os.geteuid() == 0:
            self.skipTest('cannot run test as root')

        with tempfile.NamedTemporaryFile() as f,\
                redirect_stderr(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:

            path = pathlib.Path(f.name)
            path.chmod(0o222)
            options, _ = parse_args(self.__doc__,
                                    argv=['--clients-uuids', str(path)])

        self.assertEqual(cm.exception.args[0], 2)
        self.assertRegex(output.getvalue(),
                         r'pa-dlna: error: \[Errno 13] Permission denied')

    def test_invalid_header(self):
        with tempfile.NamedTemporaryFile(mode='w') as f,\
                redirect_stderr(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:

            f.write('[foo]\n')
            f.flush()
            options, _ = parse_args(self.__doc__,
                                    argv=['--clients-uuids', f.name])

        self.assertEqual(cm.exception.args[0], 2)
        self.assertRegex(output.getvalue(),
                         'pa-dlna: error: Invalid default section header')

    def test_no_header(self):
        with tempfile.NamedTemporaryFile(mode='w') as f,\
                redirect_stderr(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:

            # emacs hideshow folding is confused by the open bracket.
            # The hexadecimel ascii value of the open bracket is '5b'.
            # Use unicode instead of writing directly '[foo\n'.
            f.write('\u005bfoo\n')
            f.flush()
            options, _ = parse_args(self.__doc__,
                                    argv=['--clients-uuids', f.name])

        self.assertEqual(cm.exception.args[0], 2)
        self.assertRegex(output.getvalue(),
                    'ConfigParser error: File contains no section headers')

    def test_log_options(self):
        with mock.patch('pa_dlna.init.setup_logging') as setup_logging:
            options, _ = parse_args(self.__doc__, argv=['--nolog-upnp',
                                                        '--log-aio'])
        self.assertEqual(options['nolog_upnp'], True)
        self.assertEqual(options['log_aio'], True)
        setup_logging.assert_called_once()

    def test_logfile(self):
        with mock.patch('builtins.open', mock.mock_open()) as m:
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])
        m.assert_called_once()
        self.assertEqual(logfile_hdler.level, logging.DEBUG)

    def test_failed_logfile(self):
        error_msg = 'Test cannot open logfile'
        with mock.patch('builtins.open', mock.mock_open()) as m_open,\
                self.assertRaises(SystemExit) as cm:
            m_open.side_effect = OSError(error_msg)
            options, logfile_hdler = parse_args(
                self.__doc__, argv=['--logfile', '/dummy/file/name'])

        self.assertEqual(cm.exception.args[0], 2)
        m_open.assert_called_once()
        self.assertRegex(self.logging_output[-1], f'OSError.*{error_msg}')

@requires_resources('os.devnull')
class Main(LoggingTestCase):
    """padlna_main() tests."""

    def test_main(self):
        clazz = mock.MagicMock()
        coro = mock.AsyncMock()
        exit_code = 'foo'

        clazz.__name__ = 'AVControlPoint'
        app = clazz()
        app.run_control_point = coro
        coro.return_value = exit_code

        with mock.patch('pa_dlna.init.UserConfig') as cfg,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna'])

        self.assertEqual(cm.exception.args[0], exit_code)
        cfg.assert_called_once()
        self.assertEqual(f'INFO:init:End of {app}', self.logging_output[-1])
        app.run_control_point.assert_called_once()
        coro.assert_awaited()

    def test_PermissionError(self):
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with mock.patch('builtins.open', mock.mock_open()) as m_open,\
                self.assertRaises(SystemExit) as cm:
            m_open.side_effect = PermissionError()
            padlna_main(clazz, self.__doc__, argv=['pa-dlna'])

        self.assertEqual(cm.exception.args[0], 1)
        self.assertEqual(self.logging_output[-1], 'ERROR:init:PermissionError()')

    def test_upnp_cmd(self):
        clazz = mock.MagicMock()
        coro = mock.AsyncMock()
        exit_code = 'foo'

        clazz.__name__ = 'UPnPControlCmd'
        app = clazz()
        app.run_control_point = coro
        app.run.return_value = exit_code

        with self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['upnp-cmd'])

        self.assertEqual(cm.exception.args[0], exit_code)
        self.assertEqual(f'INFO:init:End of {app}', self.logging_output[-1])
        app.run_control_point.assert_called_once()
        app.run.assert_called_once()

    def test_upnpapplication(self):
        app = UPnPApplication(logfile='foo')
        self.assertEqual(app.logfile, 'foo')

    def test_defaultconfig(self):
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with redirect_stdout(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna',
                                                   '--dump-default'])
        self.assertEqual(cm.exception.args[0], 0)
        doc = '# ' + Encoder.__doc__.split('\n')[0]
        self.assertEqual(output.getvalue().split('\n')[0], doc)

    def test_internalconfig(self):
        pa_dlna_conf = """
        [DEFAULT]
        selection = L16Encoder
        """
        clazz = mock.MagicMock()
        clazz.__name__ = 'AVControlPoint'

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)) as conf,\
                redirect_stdout(io.StringIO()) as output,\
                self.assertRaises(SystemExit) as cm:
            padlna_main(clazz, self.__doc__, argv=['pa-dlna',
                                                   '--dump-internal'])

        self.assertEqual(cm.exception.args[0], 0)
        conf.assert_called_once_with(user_config_pathname())
        self.assertIn("'L16Encoder': {'_mime_types': ['audio/l16']",
                      output.getvalue())

if __name__ == '__main__':
    unittest.main(verbosity=2)
