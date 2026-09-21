"""Forward pulseaudio streams to DLNA devices."""

import sys
import logging

__version__ = '2.0'
MIN_PYTHON_VERSION = (3, 11)
MIN_LIBPULSE_VERSION = '0.7'

# Systemd log level set between WARNING and ERROR.
SYSTEMD_LOG_LEVEL = logging.WARNING + 5

_version = sys.version_info[:2]
if _version < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)
