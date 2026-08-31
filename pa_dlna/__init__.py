"""Forward pulseaudio streams to DLNA devices."""

import sys
import logging

# Abort early when one of those packages is missing.
try:
    import h11
    import psutil
except ImportError as e:
    sys.exit(f'This package is required: {e!r}')

__version__ = '1.2.1'
MIN_PYTHON_VERSION = (3, 8)
MIN_LIBPULSE_VERSION = '0.7'

# Systemd log level set between WARNING and ERROR.
SYSTEMD_LOG_LEVEL = logging.WARNING + 5

_version = sys.version_info[:2]
if _version < MIN_PYTHON_VERSION:
    print(f'error: the python version must be at least'
          f' {MIN_PYTHON_VERSION}', file=sys.stderr)
    sys.exit(1)
