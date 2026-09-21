"""Utilities for starting an UPnPApplication."""

import sys

# Abort early when one of those packages is missing.
try:
    import h11
    import psutil
except ImportError as e:
    sys.exit(f'This package is required: {e!r}')

import os
import argparse
import ipaddress
import logging
import asyncio
import threading
import struct
import atexit
import configparser
from pathlib import Path
try:
    import systemd as systemd_module
    from systemd import journal, daemon
except ImportError:
    systemd_module = None
try:
    import termios
except ImportError:
    termios = None

from . import __version__, MIN_LIBPULSE_VERSION, SYSTEMD_LOG_LEVEL
from .config import DefaultConfig, UserConfig
from .pulseaudio import APPS_TITLE, APPS_HEADER

logger = logging.getLogger('init')

def require_libpulse_version(version):
    from libpulse.libpulse import __version__ as libpulse_version
    if libpulse_version.startswith('v') or libpulse_version < version:
        sys.exit(f"Error:\nThe libpulse version '{version}' or more recent"
                 f" is required.\n"
                 f"The libpulse installed version is '{libpulse_version}'.")

def disable_xonxoff(fd):
    """Disable XON/XOFF flow control on output."""

    def restore_termios():
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old_attr)
        except termios.error as e:
            print(f'Error failing to restore termios: {e!r}', file=sys.stderr)

    if termios is not None and os.isatty(fd):
        try:
            old_attr = termios.tcgetattr(fd)
            new_attr = termios.tcgetattr(fd)
            new_attr[0] = new_attr[0] & ~termios.IXON
            termios.tcsetattr(fd, termios.TCSANOW, new_attr)
            logger.debug('Disabling XON/XOFF flow control on output')
            return restore_termios
        except termios.error:
            pass

# Parsing arguments utilities.
class FilterDebug:

    def filter(self, record):
        """Ignore DEBUG logging messages."""
        if record.levelno != logging.DEBUG:
            return True

def setup_logging(options, default_loglevel):

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    options_systemd = options.get('systemd')
    if options_systemd and systemd_module is not None:
        handler = journal.JournalHandler(SYSLOG_IDENTIFIER='pa-dlna')
        formatter = logging.Formatter(fmt='%(message)s')
    else:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
                            fmt='%(name)-7s %(levelname)-7s %(message)s')

    options_loglevel = options.get('loglevel')
    if options_systemd and not options_loglevel:
        handler.setLevel(SYSTEMD_LOG_LEVEL)
    else:
        loglevel = options_loglevel if options_loglevel else default_loglevel
        handler.setLevel(getattr(logging, loglevel.upper()))

    handler.setFormatter(formatter)
    root.addHandler(handler)

    if options['nolog_upnp']:
        logging.getLogger('upnp').addFilter(FilterDebug())
        logging.getLogger('network').addFilter(FilterDebug())
    if not options['log_aio']:
        logging.getLogger('asyncio').addFilter(FilterDebug())

    # Add a file handler set at the debug level.
    if options['logfile'] is not None:
        logfile = os.path.expanduser(options['logfile'])
        try:
            logfile_hdler = logging.FileHandler(logfile, mode='w')
        except OSError as e:
            logging.error(f'cannot setup the log file: {e!r}')
        else:
            logfile_hdler.setLevel(logging.DEBUG)
            formatter = logging.Formatter(
                fmt='%(asctime)s %(name)-7s %(levelname)-7s %(message)s')
            logfile_hdler.setFormatter(formatter)
            root.addHandler(logfile_hdler)
            return logfile_hdler

    return None

def get_applications(clients_uuids_path, parser):
    if not clients_uuids_path:
        return None

    path = Path(clients_uuids_path)
    path = path.expanduser()
    if path.is_file():
        cfg_parser = CaseConfigParser(default_section=APPS_TITLE,
                                      delimiters=('->', ),
                                      empty_lines_in_values=False)
        try:
            with open(path) as f:
                    cfg_parser.read_file(f)
        except configparser.Error as e:
            parser.error(f'ConfigParser error: {e}')
        except OSError as e:
            parser.error(e)

        # Use the default section of 'cfg_parser' to store the data in
        # the 'applications' dict.
        applications = cfg_parser.defaults()
        if not applications:
            sections =  cfg_parser.sections()
            if sections:
                parser.error(f'Invalid default section header in {path}.\n'
                             f"Instead of '[{sections[0]}]'"
                             f" it must be: '[{APPS_TITLE}]'")
        return applications

    else:
        # Make sure that path is writable by writing the header.
        try:
            with open(path, 'w') as f:
                f.write(APPS_HEADER)
                return dict()
        except OSError as e:
            parser.error(f'{path} is not writable: {e!r}')

def parse_args(doc, pa_dlna=True, argv=sys.argv[1:]):
    """Parse the command line.

    UPnP discovery is run on all the networks (except the loopbak interface
    'lo') when the '--ip-addresses' and '--nics' command line arguments are
    not used or empty. Otherwise both arguments may be used indifferently or
    even jointly.
    """

    def pack_B(ttl):
        try:
            ttl = int(ttl)
            return struct.pack('B', ttl)
        except (struct.error, ValueError) as e:
            parser.error(f"Bad 'ttl' argument: {e!r}")

    def mime_types(mtypes):
        mtypes = [y for y in (x.strip() for x in mtypes.split(',')) if y]
        if len(set(mtypes)) != len(mtypes):
            parser.error('The mime types in MIME-TYPES must be different')
        for mtype in mtypes:
            mtype_split = mtype.split('/')
            if len(mtype_split) != 2 or mtype_split[0] != 'audio':
                parser.error(f"'{mtype}' is not an audio mime type")
        return mtypes

    def ipv4_addresses(ip_addresses):
        ipv4_addrs = []
        for addr in (x.strip() for x in ip_addresses.split(',')):
            if addr:
                try:
                    ipaddress.IPv4Address(addr)
                except ValueError as e:
                    parser.error(e)
                ipv4_addrs.append(addr)
        return ipv4_addrs

    parser = argparse.ArgumentParser(description=doc,
                        epilog=' '.join(parse_args.__doc__.split('\n')[2:]))
    prog = 'pa-dlna' if pa_dlna else 'upnp-cmd'
    parser.prog = prog
    parser.add_argument('--version', '-v', action='version',
                        version='%(prog)s: version ' + __version__)
    parser.add_argument('--ip-addresses', '-a', default='',
                        type=ipv4_addresses,
                        help='IP_ADDRESSES is a comma separated list of the'
                        ' local IPv4 addresses of the networks where UPnP'
                        " devices may be discovered (default: '%(default)s')")
    parser.add_argument('--nics', '-n', default='',
                        help='NICS is a comma separated list of the names of'
                        ' network interface controllers where UPnP devices'
                        " may be discovered such as 'wlan0,enp5s0' for"
                        " example (default: '%(default)s')")
    parser.add_argument('--msearch-interval', '-m', type=int, default=60,
                        help='set the time interval in seconds between the'
                        ' sending of the MSEARCH datagrams used for UPnP'
                        ' device discovery (default: %(default)s)')
    parser.add_argument('--msearch-port', '-p', type=int, default=0,
                        help='set the local UDP port for receiving MSEARCH'
                        ' response messages from UPnP devices, a value of'
                        " '0' means letting the operating system choose an"
                        ' ephemeral port (default: %(default)s)')
    parser.add_argument('--ttl', type=pack_B, default=b'\x02',
                        help='set the IP packets time to live to TTL'
                        ' (default: 2)')
    if pa_dlna:
        parser.add_argument('--port', type=int, default=8080,
                            help='set the TCP port on which the HTTP server'
                            ' handles DLNA requests (default: %(default)s)')
        parser.add_argument('--dump-default', '-d', action='store_true',
                            help='write to stdout (and exit) the default'
                            ' built-in configuration')
        parser.add_argument('--dump-internal', '-i', action='store_true',
                            help='write to stdout (and exit) the'
                            ' configuration used internally by the program on'
                            ' startup after the pa-dlna.conf user'
                            ' configuration file has been parsed')
        parser.add_argument('--clients-uuids', metavar='PATH',
                            help='PATH name of a file where are stored the'
                            ' associations between client applications and'
                            ' their DLNA device uuid'
                            )
        parser.add_argument('--loglevel', '-l',
                            choices=('debug', 'info', 'warning', 'error'),
                            help='set the log level of the stderr logging'
                            ' console (default: info)')
        parser.add_argument('--systemd', action='store_true',
                            help='run as a systemd service unit')
    parser.add_argument('--logfile', '-f', metavar='PATH',
                        help='add a file logging handler set at '
                        "'debug' log level whose path name is PATH")
    parser.add_argument('--nolog-upnp', '-u', action='store_true',
                        help="ignore UPnP log entries at 'debug' log level")
    parser.add_argument('--log-aio', '-y', action='store_true',
                        help='do not ignore asyncio log entries at'
                        " 'debug' log level; the default is to ignore those"
                        ' verbose logs')
    if pa_dlna:
        parser.add_argument('--test-devices', '-t', metavar='MIME-TYPES',
                            type=mime_types, default='',
                            help='MIME-TYPES is a comma separated list of'
                            ' distinct audio mime types. A DLNATestDevice is'
                            ' instantiated for each one of these mime types'
                            ' and registered as a virtual DLNA device. Mostly'
                            ' for testing.')

    # Options as a dict.
    options = vars(parser.parse_args(argv))

    dump_default = options.get('dump_default')
    dump_internal = options.get('dump_internal')
    if dump_default and dump_internal:
        parser.error(f"Cannot set both '--dump-default' and "
                     f"'--dump-internal' arguments simultaneously")
    if dump_default or dump_internal:
        return options, None

    default_loglevel = 'info' if pa_dlna else 'error'
    logfile_hdler = setup_logging(options, default_loglevel)
    if options['logfile'] is not None and logfile_hdler is None:
        logging.shutdown()
        sys.exit(2)

    logger.info('pa-dlna version ' + __version__)
    logger.info('Python version ' + sys.version)
    options['nics'] = [nic for nic in
                       (x.strip() for x in options['nics'].split(',')) if nic]
    logger.info(f'Options {options}')

    if 'clients_uuids' in options:
        options['applications'] = get_applications(options['clients_uuids'],
                                                   parser)
    return options, logfile_hdler

# Classes.
class CaseConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr):
        return optionstr

class ControlPointAbortError(Exception): pass

class UPnPApplication:
    """An UPnP application."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    async def run_control_point(self):
        raise NotImplementedError

    def __str__(self):
        raise NotImplementedError

# The main function.
def padlna_main(clazz, doc, argv=sys.argv):

    def run_in_thread(coro):
        """Run the UPnP control point in a thread."""

        cp_thread = threading.Thread(target=asyncio.run, args=[coro])
        cp_thread.start()
        return cp_thread

    assert clazz.__name__ in ('AVControlPoint', 'UPnPControlCmd')
    pa_dlna = True if clazz.__name__ == 'AVControlPoint' else False
    if pa_dlna:
        require_libpulse_version(MIN_LIBPULSE_VERSION)

    # Parse the arguments.
    options, logfile_hdler = parse_args(doc, pa_dlna, argv[1:])

    systemd = options.get('systemd')
    if systemd and systemd_module is None:
        raise RuntimeError('Cannot import the systemd module, the'
                           " 'python-systemd' package is missing")

    # Instantiate the UPnPApplication.
    if pa_dlna:
        # Get the encoders configuration.
        try:
            # Add the 'delimiters' option to fix in Python 3.14:
            #    configparser.InvalidWriteError: Cannot write key that
            #    contains the ':' delimiter.
            # See https://github.com/python/cpython/pull/129270
            if options['dump_default']:
                DefaultConfig(delimiters=('=',)).write_parser(sys.stdout)
                sys.exit(0)

            config = UserConfig(systemd=systemd)
            if options['dump_internal']:
                config.print_internal_config()
                sys.exit(0)
        except Exception as e:
            logger.error(f'{e!r}')
            sys.exit(1)
        app = clazz(config=config, **options)
    else:
        app = clazz(**options)

    # Run the UPnPApplication instance.
    loglevel = SYSTEMD_LOG_LEVEL if systemd else logging.INFO
    logger.log(loglevel, f'Starting {app}')
    exit_code = 1
    try:
        if pa_dlna:
            try:
                if systemd:
                    daemon.notify('READY=1')
                else:
                    try:
                        fd = sys.stdin.fileno()
                    except OSError as e:
                        # 'stdin is pseudofile, has no fileno()' when run by
                        # pytest
                        pass
                    else:
                        restore_termios = disable_xonxoff(fd)
                        if restore_termios is not None:
                            atexit.register(restore_termios)
                exit_code = asyncio.run(app.run_control_point())
            finally:
                if systemd:
                    daemon.notify('STOPPING=1')
        else:
            # Run the control point of upnp-cmd in a thread.
            event = threading.Event()
            cp_thread = run_in_thread(app.run_control_point(event))
            exit_code = app.run(cp_thread, event)
    finally:
        logger.log(loglevel, f'End of {app}')
        if logfile_hdler is not None:
            logfile_hdler.flush()
        logging.shutdown()
        sys.exit(exit_code)
