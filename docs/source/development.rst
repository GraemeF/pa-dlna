Development
===========

Contributing to pa-dlna
-----------------------

Everyone should be presumed to be contributing in good faith.

AI policy
"""""""""
Contributions that include LLM-generated content cannot exceed around 15 lines
of code.

This limit is the same as the one established by the `GCC steering
committee`_. The purpose of this pa-dlna limit is to avoid having to deal with the
submissions of code bloats such as the 965 AI generated changes in `MR !33`_
based on groundless premisses in `issue #60`_.

Creating a MR
"""""""""""""

Prerequisites to the creation of a MR (merge request):

 - The MR MUST be the implementation of a fix or of an enhancement that has been
   described in a pre-existing issue which is either a bug report or an
   enhancement proposal.
 - A consensus MUST have been established in this issue about the way the bug
   should de fixed or the enhancement implemented.

.. _design:

Design
------

.. _meta data:

Meta Data
"""""""""

This feature is enabled on a per encoder or per device basis with the
``track_metadata`` option set to ``yes``. It is enabled by default.

When ``pa-dlna`` receives a ``change`` event from pulseaudio and this event is
related to a change to the meta data as for example when a new track starts with
a new song, the following sequence of events occurs:

 * ``pa-dlna``:

   + Writes the last chunk to the HTTP socket (see `Chunked Transfer Coding`_)
     and sends a ``SetNextAVTransportURI`` SOAP action with the new meta data.
   + Upon receiving the HTTP GET request from the device, instantiates a new
     Track and starts a task to run the pulseaudio stream.

 * The DLNA device:

   + Gets the ``SetNextAVTransportURI`` with the new meta data and sends a GET
     request to start a new HTTP session for the next track while still playing
     the current track from its read buffer.
   + Still playing the current track, pre-loads the read buffer of the new HTTP
     session.
   + Upon receiving the last chunk for the current track, starts playing the
     next track.

This way, the last part of the current track is not truncated by the amount of
latency introduced by the device's read buffer and the delay introduced by
filling the read buffer of the next track is minimized.

Asyncio Tasks
"""""""""""""

Task names in **bold** characters indicate that there is one such task for each
DLNA device, when in *italics* that there may be such tasks for each DLNA
device.

  UPnPControlPoint tasks:

    ================      ======================================================
    ssdp notify           Monitor reception of NOTIFY SSDPs.
    ssdp msearch          Send MSEARCH SSDPs at regular intervals.
    **root devices**      Implement control of the aging of an UPnP root device.
    ================      ======================================================

  AVControlPoint tasks:

    ================      ======================================================
    main                  Instantiate the UPnPControlPoint that starts the UPnP
                          tasks.
                          |br| Create the pulse task, the http_server task, the
                          renderer tasks.
                          |br| Create the shutdown task.
                          |br| Handle UPnP notifications.

    pulse                 Monitor pulseaudio sink-input events.
    *maybe_stop*          Handle a ``remove`` pulse event.
    *http_server*         Serve DLNA HTTP requests, one task per IP address.
                          |br| Start the client_connected tasks.
    **renderers**         Act upon pulseaudio events.
                          |br| Run UPnP SOAP actions.
    abort                 Abort the pa-dlna program.
    shutdown              Wait on event pushed by the signal handlers.
    ================      ======================================================

  HTTPServer tasks:

    ==================    ======================================================
    *client_connected*    HTTPServer callback wrapped by asyncio in a task.
                          |br| Start the StreamSession tasks:
                          |br| ``parec | encoder program | HTTP socket``.
    ==================    ======================================================

  StreamSession tasks:

    ====================    ====================================================
    *parec process*         Start the parec process and wait for its exit.
    *parec log_stderr*      Log the parec process stderr.
    *encoder process*       Start the encoder process and wait for its exit.
    *encoder log_stderr*    Log the encoder process stderr.
    *track*                 Write the audio stream to the HTTP socket.
    ====================    ====================================================

  Track tasks:

    ==============        ======================================================
    *shutdown*            Write the last chunk and close the HTTP socket.
    ==============        ======================================================

DLNA Device Registration
""""""""""""""""""""""""

For a new DLNA device to be registered, ``pa-dlna`` must establish the **local**
network address to be used in the URL that must be  advertised to the DLNA
device in the ``SetAVTransportURI`` and ``SetNextAVTransportURI`` SOAP actions,
so that the DLNA device may initiate the HTTP session and start the
streaming. This depends on which event triggered this registration:

  Reception of the  unicast response to an UPnP MSEARCH SSDP.
    The destination address of the SSDP response is the address that is being
    looked for.

    MSEARCH SSDP are sent by ``pa-dlna`` every 60 seconds (default).

  Reception of an UPnP NOTIFY SSDP, broadcasted by the device [#]_.
    The DLNA device can be registered only if the source address of this packet
    belongs to one of the subnets of the network interfaces. That is, the DLNA
    device and the host belong to the same subnet on this interface and the
    local IP address on this subnet is the address that is being looked for.

    The `UPnP Device Architecture`_ specification does not specify the
    periodicity of NOTIFY SSDPs sent by DLNA devices.

Development process [#]_
------------------------

Requirements
""""""""""""

Development:
    * `curl`_ and `ffmpeg`_ are used by some tests of the test suite. When
      missing, those tests are skipped. `curl`_ is also needed when releasing a
      new version to fetch the GitLab test coverage badge.
    * `ffmpeg`_, the `Upmpdcli`_ DLNA Media Renderer, the `MPD`_ Music Player
      Daemon and a running Pulseaudio or PipeWire sound server are needed to run
      the tests of the ``test_tracks`` Python module (otherwise those tests are
      skipped).

      An audio track sourced by ffmpeg is streamed by pa-dlna to the Upmpdcli
      DLNA that outputs the stream to MPD, which in turn outputs the stream to a
      PulseAudio/PipeWire sink created by ``test_tracks``. Monitoring the state
      of this sink allows checking that the audio track does follow this
      path. This scenario may be run at the debug log level with the following
      command::

        $ python -m pa_dlna.tests.test_tracks [EncoderName]

    * `pactl`_ is needed to run the tests that connect to the pulseaudio or
      pipewire sound server. When missing, those tests are skipped.
    * `docker`_ may be used to run the test suite in a pulseaudio or pipewire
      debian container. Follow the instructions written as comments in each of
      the ``Dockerfile.pulse`` and ``Dockerfile.pipewire`` Docker files.
    * `coverage`_ is used to get the test suite coverage.
    * `python-packaging`_ is used to set the development version name as conform
      to PEP 440.
    * `flit`_ is used to publish pa-dlna to PyPi and may be used to install
      pa-dlna locally.

      At the root of the pa-dlna git repository, use the following command to
      install pa-dlna locally::

        $ flit install --symlink [--python path/to/python]

      This symlinks pa-dlna into site-packages rather than copying it, so that
      you can test changes by running the ``pa-dlna`` and ``upnp-cmd``
      commands provided that the ``PATH`` environment variable holds
      ``$HOME/.local/bin``.

      Otherwise without using `flit`_, one can run those commands from the root
      of the repository as::

        $ python -m pa_dlna.pa_dlna
        $ python -m pa_dlna.upnp_cmd

Documentation:
    * `Sphinx`_ [#]_.
    * `Read the Docs theme`_.
    * Building the pdf documentation:

      - The latex texlive package group.
      - Imagemagick version 7 or more recent.

Documentation
"""""""""""""

To build locally the documentation follow these steps:

  - Generate the ``default-config.rst`` file::

      $ python -m tools.gendoc_default_config

  - Fetch the GitLab test coverage badge::

      $ curl -o images/coverage.svg "https://gitlab.com/xdegaye/pa-dlna/badges/master/coverage.svg?min_medium=85&min_acceptable=90&min_good=90"
      $ magick images/coverage.svg images/coverage.png

  - Build the html documentation and the man pages::

      $ make -C docs clean html man latexpdf

Updating development version
""""""""""""""""""""""""""""

Run the following commands to update the version name at `latest documentation`_
after a bug fix or a change in the features::

    $ python -m tools.set_devpt_version_name
    $ make -C docs clean html man latexpdf
    $ git commit -m "Update development version name"
    $ git push

Releasing
"""""""""

* Run the test suite from the root of the project [#]_::

    $ python -m unittest --verbose --catch --failfast

* Get the test suite coverage::

    $ coverage run --include="./*" -m unittest
    $ coverage report -m

* Update ``__version__`` in pa_dlna/__init__.py.
*  When this new release depends on a more recent libpulse release than
   previously:

  + Update ``MIN_LIBPULSE_VERSION`` in pa_dlna/__init__.py.
  + Update the minimum required libpulse version in pyproject.toml.

* Update docs/source/history.rst if needed.
* Build locally the documentation, see one of the previous sections.
* Commit the changes::

    $ git commit -m 'Version 1.n'
    $ git push

* Tag the release and push::

    $ git tag -a 1.n -m 'Version 1.n'
    $ git push --tags

* Publish the new version to PyPi::

    $ flit publish

.. include:: common.txt

.. _Chunked Transfer Coding:
    https://www.rfc-editor.org/rfc/rfc2616#section-3.6.1
.. _Read the Docs theme:
    https://docs.readthedocs.io/en/stable/faq.html#i-want-to-use-the-read-the-docs-theme-locally
.. _Sphinx: https://www.sphinx-doc.org/
.. _curl: https://curl.se/
.. _pactl: https://linux.die.net/man/1/pactl
.. _docker: https://docs.docker.com/build/guide/intro/
.. _`coverage`: https://pypi.org/project/coverage/
.. _flit: https://pypi.org/project/flit/
.. _unittest command line options:
    https://docs.python.org/3/library/unittest.html#command-line-options
.. _latest documentation: https://pa-dlna.readthedocs.io/en/latest/
.. _python-packaging: https://github.com/pypa/packaging
.. _ffmpeg: https://www.ffmpeg.org/ffmpeg.html
.. _Upmpdcli: https://www.lesbonscomptes.com/upmpdcli/
.. _MPD: https://mpd.readthedocs.io/en/latest/user.html
.. _`GCC steering committee`: https://lwn.net/Articles/1086041/
.. _`issue #60`: https://gitlab.com/xdegaye/pa-dlna/-/work_items/60
.. _`MR !33`: https://gitlab.com/xdegaye/pa-dlna/-/merge_requests/33

.. rubric:: Footnotes

.. [#] All sockets bound to the notify multicast address receive the datagram
       sent by a DLNA device, even though it has been received by only one
       interface at the physical layer.
.. [#] The shell commands in this section are all run from the root of the
       repository.
.. [#] Required versions at ``docs/requirements.txt``.
.. [#] See `unittest command line options`_.
