Release history
===============

Version 2.1
    - Add to gitlab CI/CD the tests streaming audio to ``upmpdcli``
      (`UPnP Audio Media Renderer based on MPD`_).
    - Fix AttributeError exception upon stopping a track and closing the DLNA
      device simultaneously (issue #66).

Version 2.0
    - Implement compliant HTTP 1.1 code with the `h11`_ package. This adds
      the **new requirement** for the h11 package.

      + Send HTTP 1.1 requests [#]_ and accept
        HTTP 1.1 or HTTP 1.0 responses with the `h11`_ package.
      + Implement the HTTP 1.1 servers that handle
        streaming audio [#]_  to the DLNA devices with the `h11`_ package.

    - Support now Python version 3.11 or more recent; versions 3.8, 3.9 and 3.10
      are no longer supported (version 3.10 is scheduled to reach ``End of
      life`` on october 2026).
    - Fix failures to change the tracks manually on the ``Strawberry`` music
      player when cross-fading is disabled (issue #65). This should also fix
      the problem reported in issue #48 whose fix implemented in pa-dlna version
      0.16 is not robust.
    - Fix the systematic delay of 5 seconds (the default) used to spread out
      SOAP requests when cross-fading is disabled on the ``Strawberry`` music
      player (see previous item). This delay is only enforced when tracks are
      switched too rapidly.
    - Add the ``Code of Conduct`` section to the documentation that lists the
      prerequisites to the creation of a merge request (MR) and states the
      pa-dlna **AI policy**.
    - Create the default MR template.

Version 1.2.1
    - Remove ``flit_core`` version upper limit in ``pyproject.toml``.

Version 1.2
  - Support Python version 3.14:

    + Fix warnings upon return/break/continue that exit a finally block (`PEP
      765`_).
    + Restrict the configparser delimiters set to ``'='`` when writing the
      default configuration file to fix the check (made starting with Python
      3.14) that triggers configparser.InvalidWriteError: *Cannot write key that
      contains the* ``':'`` *delimiter*. See `Cpython PR #129270`_.

  - Fix a crash upon a race condition that occurs when the DLNA device is about
    to be closed and is simultaneously discovered again by an SSDP
    notify/msearch datagram.
  - The UPnP device is now closed instead of being permanently disabled after a
    connection error ocurring while pa-dlna is streaming a track. It will be
    discovered again by the next SSDP_NOTIFY or SSDP_MSEARCH udp datagram after
    the connection is up again (issue #55).

Version 1.1
  - Fix the ``test_libpulse`` deadlock in ``pytest`` when another pa-dlna
    instance is already running. The corresponding test cases are skipped.
  - Fix the ``test_main`` failure and the warning about ``TestEncoder`` when the
    test suite is run by ``pytest``.
  - A run of the test suite aborts and prints a clear error message when the
    ``libpulse`` version in use is invalid (issue #52).

Version 1.0
  - Ignore spaces in the ``max-age`` setting of the ``CACHE-CONTROL`` field of
    SSDP_NOTIFY datagrams (issue #50).

Version 0.16
  - The required libpulse version is now ``0.7`` after the `error handling
    changes`_ made in the libpulse release.
  - Fix music player on KDE randomly raises exception while switching to next
    track (issue #49).
  - KDE music players (Juk, Elisa, Strawberry) misbehave by sending ``remove``
    pulse events just before switching to a next track. A work-around to this
    problem using a timer is implemented that discards those events (issue #48).
  - **[Sonos]** Accept HTTP 1.1 chunked encoding response to pa-dlna HTTP 1.0
    requests.

Version 0.15
  - The ``Transfer-Encoding`` HTTP 1.1 header in response to HTTP 1.0 GET
    requests is not supported (issue #47).
  - Ignore invalid subelements in ``Icons`` (issue #40).
  - Use ``friendlyName``, the name displayed by pavucontrol, as Renderer's name.
  - Add the pa-dlna systemd service unit.
  - Fix L16Encoder failing to set the correct mime type when the ``ProtocolInfo
    <contentFormat>`` entry is simply ``audio/L16`` without the rate parameter
    (issue #36).
  - Added a test framework that runs tests with Upmpdcli (a software DLNA
    MediaRenderer) and MPD on the PulseAudio or Pipewire sound server.
  - A pdf document is part of the pa-dlna documentation. To access the
    documentation as a pdf document one must click on the icon at the down-right
    corner of any page of the documentation on the web. It allows to switch
    between stable and latest versions and to select the corresponding pdf
    document.
  - Fix the development version name as PEP 440 conformant (issue #33).

Version 0.14
  - pa-dlna versioning conforms to PEP 440.
  - Exit with an error message when the ``libpulse`` version is older than the
    required one. The required libpulse version is currently ``0.5``.
  - **[Upmpdcli]** Fix cannot play on ``upmpdcli`` tracks whose metadata
    includes the ``&`` character (issue #30).
  - Add the ``--clients-uuids`` command line option that may be used as a work
    around to Wireplumber issue 511 (issue #15).

Version 0.13
  - The backtraces of unhandled exceptions that occur in asyncio tasks are
    logged at the debug log level. Otherwise these exceptions are just logged as
    an error with a message saying that the backtrace can be obtained by running
    the program at the debug log level.
  - **[Moode UPNP]** Fix libexpat called by ``upmpdcli`` fails parsing the
    DIDL-Lite xml strings (issue #29).

Version 0.12
  - Rename LibPulse.get_events() to get_events_iterator(). The change has been
    introduced by version 0.4 of the libpulse package (issue #26).
  - Handle exceptions raised while getting the sink after ``module-null-sink``
    has been loaded.
  - Fix a typo in the installation documentation.

Version 0.11
  - Import the libpulse package from Pypi.
  - Support Python version 3.12 - Fix some network mock tests by forcing the
    release of control to the asyncio loop.

Version 0.10
  - **[Teufel 3sixty]** Handle HTTP HEAD requests from DLNA devices. Some
    renderers fetch stream meta data via HEAD request before requesting actual
    media streams.
  - Fix crash upon parsing empty deviceList in device description.

Version 0.9
  - Support Pipewire version 1.0 and the previous version 0.3.
  - Log the name of the sound server and its version.

Version 0.8
  - Changing the volume level with ``pavucontrol`` does not interfere with the
    current audio stream.
  - **[Marantz NR1200]** Support multiple embedded MediaRenderers in a DLNA
    device.
  - The ``deviceList`` attribute of UPnPDevice is now a list instead of a
    dictionary.
  - Do not age an UPnP root device upon receiving a ``CACHE-CONTROL`` header
    with a value set to ``max-age=0``.

Version 0.7
  - Name ``libpulse`` the Python package, interface to the ``libpulse``
    library.
  - Document which TCP/UDP ports may not be blocked by a firewall.
  - Add the ``--msearch-port`` command line option.
  - Tests are run in GitLab CI/CD with Pulseaudio and with Pipewire.
  - Add Docker files to allow running the test suite in Pulseaudio and Pipewire
    debian containers.
  - Update the README with package requirements for linux distributions.
  - The ``psutil`` Python package must be installed now separately as this
    package is included by many distributions (debian, archlinux, fedora, ...).
  - Log the sound server name and version.

Version 0.6
  - **[Yamaha RN402D]** Spread out UPnP SOAP actions that start/stop a stream
    (issue #16).
  - Fix the ``args`` option in the [EncoderName.UDN] section of the user
    configuration is always None.
  - Log a warning when the sink-input enters the ``suspended`` state.
  - Fix assertion error upon ``exit`` Pulseaudio event (issue #14).
  - Support PipeWire. No change is needed to support PipeWire. The test suite
    runs successfully on PipeWire.
  - Fix no sound when pa-dlna is started while the track is already playing
    (issue #13).
  - Use the built-in libpulse package that uses ctypes to interface with the
    libpulse library and remove the dependency to ``pulsectl_asyncio``.
  - Wait for the http server to be ready before starting the renderer task. This
    also fixes the test_None_nullsink and test_no_path_in_request tests on
    GitLab CI/CD (issue #12).
  - Support Python 3.11.

Version 0.5
  - Log a warning upon an empty body in the HTTP response from a DLNA device
    (issue #11).
  - UPnP discovery is triggered by NICs [#]_ state changes (issue #10).
  - Add the ``--ip-addresses``, ``-a`` command line argument (issue #9).
  - Fix changing the ``args`` encoder option is ignored (issue #8).

Version 0.4
  - ``sample_format`` is a new encoder configuration option (issue #3).
  - The encoders sample format is ``s16le`` except for the ``audio/l16``
    encoder (issue #7).
  - The encoder command line is now updated with ``pa-dlna.conf`` user
    configuration (issue #6).
  - Fix the parec command line length keeps increasing at each new track when
    the encoder is set to track metadata (issue #5).
  - Fix failing to start a new stream session while the device is still playing
    when the encoder is set to not track metadata (issue #4).
  - Fix ``pa-dlna`` hangs when one types <Control-S> in the terminal where the
    program has been started (issue #2).

Version 0.3
  - The test coverage of ``pa-dlna`` is 95%.
  - UPnPControlPoint supports now the context manager protocol, not the
    asynchronous one.
  - UPnPControlPoint.get_notification() returns now QUEUE_CLOSED upon closing.
  - Fix some fatal errors on startup that were silent.
    Here are the  missing error messages that are now printed when one of those
    fatal errors occurs:

    + Error: No encoder is available.
    + Error: The pulseaudio 'parec' program cannot be found.
  - Fix curl: (18) transfer closed with outstanding read data remaining.
  - Fix a race condition upon the reception of an SSDP msearch response that
    occurs just after the reception of an SSDP notification and while the
    instantiation of the root device is not yet complete.
  - Failure to set SSDP multicast membership is reported only once.

Version 0.2
  - Test coverage of the UPnP package is 94%.
  - Fix unknown UPnPXMLFatalError exception.
  - The ``description`` commands of ``upnp-cmd`` don't prefix tags with a
    namespace.
  - Fix the ``description`` commands of ``upnp-cmd`` when run with Python 3.8.
  - Fix IndexError exception raised upon OSError in
    network.Notify.manage_membership().
  - Fix removing multicast membership when the socket is closed.
  - Don't print a stack traceback upon error parsing the configuration file.
  - Abort on error setting the file logging handler with ``--logfile PATH``.

Version 0.1
  - Publish the project on PyPi.

.. _`PEP 765`: https://peps.python.org/pep-0765/
.. _`Cpython PR #129270`: https://github.com/python/cpython/pull/129270
.. _`error handling changes`:
   https://libpulse.readthedocs.io/en/stable/history.html
.. _h11: https://pypi.org/project/h11/
.. _`UPnP Audio Media Renderer based on MPD`:
   https://www.lesbonscomptes.com/upmpdcli/

.. rubric:: Footnotes

.. [#] HTTP requests ``GET`` and ``POST`` used to carry the SOAP requests.
.. [#] HTTP 1.1 chunked transfer encoding is implemented by the `h11`_ package.
.. [#] Network Interface Controller.
