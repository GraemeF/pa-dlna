.. image:: images/coverage.png
   :alt: [pa-dlna test coverage]

`pa-dlna`_ forwards audio streams to DLNA devices.

A Python project based on `asyncio`_, that uses `ctypes`_ to interface with the
``libpulse`` library and supports the PulseAudio and PipeWire [#]_ sound
servers.

`pa-dlna`_ is composed of the following components:

 * The ``pa-dlna`` program forwards PulseAudio streams to DLNA devices.
 * The ``upnp-cmd`` is an interactive command line tool for introspection and
   control of UPnP devices [#]_.
 * The UPnP Python sub-package is used by both commands.

The documentation is hosted at `Read the Docs`_:

 - The `stable documentation`_ of the last released version.
 - The `latest documentation`_ of the current GitLab development version.

To access the documentation as a pdf document one must click on the icon at the
down-right corner of any page. It allows to switch between stable and latest
versions and to select the corresponding pdf document.

Requirements
------------

Python version 3.11 or more recent.

psutil and h11
""""""""""""""

The UPnP sub-package  and therefore the ``upnp-cmd`` and ``pa-dlna`` commands
depend on the `psutil`_ and `h11`_ Python packages. These packages are available
in most distributions as ``python3-psutil`` or ``python-psutil`` and
``python3-h11`` or ``python-h11``. They will be installed by ``pip`` as a
dependency of ``pa-dlna`` if not already installed as a package of the
distribution.

libpulse
""""""""

`libpulse`_ is a Python asyncio interface to the Pulseaudio and Pipewire
``libpulse`` library. It was a sub-package of ``pa-dlna`` and has become a
full-fledged package on PyPi. It will be installed by ``pip`` as a dependency of
``pa-dlna``.

parec
"""""

`pa-dlna`_ uses the pulseaudio ``parec`` program [#]_. Depending on the linux
distribution it may be already installed as a dependency of pulseaudio or of
pipewire-pulse. If not, then the package that owns ``parec`` must be
installed. On archlinux the package name is ``libpulse``, on debian it is
`pulseaudio-utils`_.

systemd
"""""""

The `python-systemd`_ package is required to run the pa-dlna systemd service
unit. It is packaged by almost all Linux distributions but under different
names. To install the package from a Linux distribution or from PYPi, see the
``Installation section`` on the main page of the `python-systemd git
repository`_.

Encoders
""""""""

No other dependency is required by `pa-dlna`_ when the DLNA devices support raw
PCM L16 (:rfc:`2586`) [#]_.

Optionally, encoders compatible with the audio mime types supported by the
devices may be used. ``pa-dlna`` currently supports the `ffmpeg`_ (mp3, wav,
aiff, flac, opus, vorbis, aac), the `flac`_ and the `lame`_ (mp3) encoders. The
list of supported encoders, whether they are available on this host and their
options, is printed by the command that prints the default configuration::

  $ pa-dlna --dump-default

pavucontrol
"""""""""""

Optionally, one may install the ``pavucontrol`` package for easier management of
associations between sound sources and DLNA devices.

Installation
------------

pipewire as a pulseaudio sound server
"""""""""""""""""""""""""""""""""""""

The ``pipewire``, ``pipewire-pulse`` and ``wireplumber`` packages must be
installed and the corresponding programs started. If you are switching from
pulseaudio, make sure to remove ``/etc/pulse/client.conf`` or to comment out the
setting of ``default-server`` in this file as pulseaudio and pipewire do not use
the same unix socket path name.

The ``parec`` 's package includes the ``pactl`` program. One may check that the
installation of pipewire as a pulseaudio sound server is successfull by running
the command::

  $ pactl info

pa-dlna
"""""""

Install ``pa-dlna`` with pip::

  $ python -m pip install pa-dlna

Configuration
-------------

A ``pa-dlna.conf`` user configuration file overriding the default configuration
may be used to:

 * Change the preferred encoders ordered list used to select an encoder.
 * Configure encoder options.
 * Set an encoder for a given device and configure the options for this device.
 * Configure the *sample_format*, *rate* and *channels* parameters of the
   ``parec`` program used to forward PulseAudio streams, for a specific device,
   for an encoder type or for all devices.

See the `configuration`_ section of the pa-dlna documentation.

.. _pa-dlna: https://gitlab.com/xdegaye/pa-dlna
.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _ctypes: https://docs.python.org/3/library/ctypes.html
.. _pulseaudio-utils: https://packages.debian.org/bookworm/pulseaudio-utils
.. _pa-dlna issue 15: https://gitlab.com/xdegaye/pa-dlna/-/issues/15
.. _Wireplumber issue 511:
        https://gitlab.freedesktop.org/pipewire/wireplumber/-/issues/511
.. _Read the Docs: https://about.readthedocs.com/
.. _stable documentation: https://pa-dlna.readthedocs.io/en/stable/
.. _latest documentation: https://pa-dlna.readthedocs.io/en/latest/
.. _psutil: https://pypi.org/project/psutil/
.. _h11: https://pypi.org/project/h11/
.. _`ConnectionManager:3 Service`:
        http://upnp.org/specs/av/UPnP-av-ConnectionManager-v3-Service.pdf
.. _ffmpeg: https://www.ffmpeg.org/ffmpeg.html
.. _flac: https://xiph.org/flac/
.. _lame: https://lame.sourceforge.io/
.. _configuration: https://pa-dlna.readthedocs.io/en/stable/configuration.html
.. _pipewire-pulse: https://docs.pipewire.org/page_man_pipewire_pulse_1.html
.. _libpulse: https://pypi.org/project/libpulse/
.. _pa-dlna command: https://pa-dlna.readthedocs.io/en/stable/pa-dlna.html
.. _python-systemd: https://www.freedesktop.org/software/systemd/python-systemd/
.. _python-systemd git repository: https://github.com/systemd/python-systemd

.. [#] When using PipeWire with the Wireplumber session manager, ``pa-dlna``
       must be started before the audio streams that are routed to DLNA
       devices. Re-starting those audio  streams fixes the problem. See `pa-dlna
       issue 15`_ and `Wireplumber issue 511`_.

       A workaround may be used with the ``--clients-uuids`` command line
       option, see the `pa-dlna command`_ documentation.

.. [#] The ``pa-dlna`` and ``upnp-cmd`` programs can be run simultaneously.

.. [#] The ``parec`` program also uses the ``libpulse`` library which is
       included in ``parec`` 's package or is installed as a dependency. Note
       also that this package includes the ``pactl`` and ``pacmd`` programs.

.. [#] DLNA devices must support the HTTP GET transfer protocol and must support
       HTTP 1.1 as specified by Annex A.1 of the `ConnectionManager:3 Service`_
       UPnP specification.
