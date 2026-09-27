.. _configuration:

Configuration
=============

The configuration is defined by the :ref:`default_config` [#]_ overriden by the
:ref:`user_configuration` file if it exists. It is used in the following two
stages of the ``pa-dlna`` process:

    - The selection of the encoder and audio mime-type.
    - The forwarding of an audio stream.

Encoder selection
-----------------

``pa-dlna`` fetches the DLNA device supported mime-types using the
``GetProtocolInfo`` UPnP command [#]_ and selects the first encoder/mime-type in
the configured ``selection`` option that matches an item of the list returned by
``GetProtocolInfo``.

If not already existing, an HTTP server is then started that answers requests on
the local IP address where the DLNA device has been discovered.

.. _`streaming`:

Streaming
---------

When PulseAudio (actually libpulse) notifies ``pa-dlna`` of the existence of a
new stream from a source to the DLNA sink, it sends a ``SetAVTransportURI`` or
``SetNextAVTransportURI`` [#]_ UPnP SOAP [#]_ action to the device. This command
holds:

    - The stream metadata.
    - The selected mime-type.
    - The URL to be used by the device to fetch the stream by initiating an HTTP
      GET for this URL.

Upon responding to the HTTP GET request ``pa-dlna`` forks the ``parec`` process
and the selected encoder process [#]_ using the configured options. The output
of the ``parec`` process is piped to the encoder process, the output of the
encoder process is written to the HTTP socket.

It is possible to test an encoder configuration without using a DLNA
device with the help of the ffplay program from the ffmpeg suite and a tool that
retrieves HTTP files such as curl or wget. Here is an example with the
``L16Encoder``:

    - Set the L16Encoder at the highest priority in the pa-dlna.conf file.
    - Run pa-dlna with the ``test-devices`` command line option [#]_::

        $ pa-dlna --test-devices audio/L16\;rate=44100\;channels=2
    - Start a music player application and play some track.
    - Associate this source with the ``DLNATest_L16 - 0ab65`` DLNA sink in
      pavucontrol.
    - Fetch the stream with curl as a file named ``output`` using the URL
      printed by the logs [#]_::

        $ curl http://127.0.0.1:8080/audio-content/uuid:e7fa8886-6d97-a009-b6b6-6b1171b0ab65 -o output

    - Play the ``output`` file with the command::

        $ ffplay -f s16be -ac 2 -ar 44100 output

Encoders configuration
----------------------

The encoders configuration is defined by the :ref:`default_config` that may be
overriden by the user's ``pa-dlna.conf`` file.

The ``pa-dlna.conf`` file also allows the specification of the encoder and its
options for a given DLNA device with a section named [EncoderName.UDN]. In this
case the selection of the encoder using the ``selection`` option is by-passed
and EncoderName is the selected encoder. UDN is the udn [#]_ of the device as
printed by the logs or by the ``upnp-cmd`` command line tool.

The default configuration is structured as an `INI file`_, more precisely as
text that may be parsed by the `configparser`_ Python module. The user's
configuration file is also an INI file and obeys the same rules as the default
configuration:

    * A section is either [DEFAULT], [EncoderName] or [EncoderName.UDN]. The
      options defined in the [DEFAULT] section apply to all the other sections
      and are overriden when also defined in the [EncoderName] or
      [EncoderName.UDN] sections. There is an exception with the ``selection``
      option that is only meaningful in a [DEFAULT] section and ignored in all
      the other sections.
    * The ``selection`` option is an ordered comma separated list of
      encoders. This list is used to select the first encoder matching one of
      the mime-types supported by a discovered DLNA device when there is no
      specific [EncoderName.UDN] configuration for the given device.
    * The options defined in the user's ``pa-dlna.conf`` file override the
      options of the default configuration.
    * Section names and options are case sensitive.
    * Boolean values are resticted to ``yes`` or ``no``.

.. _user_configuration:

User configuration
------------------

The full path name of the  user's ``pa-dlna.conf`` file is determined by
``pa-dlna`` as follows:

    * If the ``XDG_CONFIG_HOME`` environment variable is set, the path name is
      ``$XDG_CONFIG_HOME/pa-dlna/pa-dlna.conf``.
    * Otherwise the path name is ``$HOME/.config/pa-dlna/pa-dlna.conf``.

When ``pa-dlna.conf`` is not found, the program uses the default configuration.
Otherwise it uses the default configuration with its options overriden by the
user's configuration and with the added [EncoderName.UDN] sections.

Here is an example of a ``pa-dlna.conf`` file::

    [DEFAULT]
    selection =
        Mp3Encoder,
        FFMpegMp3Encoder,
        FFMpegFlacEncoder,

    [FFMpegFlacEncoder]
    track_metadata = no

    [FFMpegMp3Encoder]
    bitrate = 320

    [FFMpegMp3Encoder.uuid:9ab0c000-f668-11de-9976-00a0de98381a]

In this example:

    * The DLNA device whose udn is ``uuid:9ab0c000-f668-11de-9976-00a0de98381a``
      uses the FFMpegMp3Encoder with the default bitrate.
    * The other devices may use the three encoders of the selection, the
      preferred one being the Mp3Encoder with the default bitrate.
    * The FFMpegMp3Encoder is only used if the Mp3Encoder (the lame encoder) is
      not available and in that case it runs with a bitrate of 320 Kbps.
    * The FFMpegFlacEncoder is used when a DLNA device does not support the
      'audio/mp3' and 'audio/mpeg' mime types and in that case its
      track_metadata option is not set.
    * If a DLNA device does not support the mp3 or the flac mime types, then it
      cannot be used even though the device would support one of the other mime
      types defined in the overriden default configuration.

One can verify what is the actual configuration used by ``pa-dlna`` by running
the program with the ``--dump-internal`` command line option. A Python
dictionary is printed with keys being ``EncoderName`` or ``UDN`` and the values
a dictionary of their options. The ``EncoderName`` keys are ordered according to
the ``selection`` option.

PulseAudio options
------------------

Options used by the ``parec`` and encoder programs (see how those programs are
used in the :ref:`streaming` section):

  *sample_format*
    The default value is ``s16le``.

    The encoders supporting the ``audio/L16`` mime types (i.e. uncompressed
    audio data as defined by `RFC 2586`_) have this option set to ``s16be`` as
    specified by the RFC and it cannot be modified by the user.

    See the Pulseaudio supported `sample formats`_.

  *rate*
    The Pulseaudio sample rate (default: 44100).

  *channels*
    The number of audio channels (default: 2).

Common options
--------------

  *args*
    The ``args`` option is the encoder program's command line. When the ``args``
    option is None, the encoder command line is built from the Pulseaudio
    options and the encoder's specific options.

    As all the other options (except ``sample_format`` in some cases, see above)
    it may be overriden by the user.

  *track_metadata*
    * When ``yes``, each track is streamed in its own HTTP session allowing the
      DLNA device to get each track meta data as described in the :ref:`meta
      data` section.

      This is the default.
    * When ``no``, there is only one HTTP session for all the tracks. Set this
      option to ``no`` when the logs show ERROR entries upon tracks changes.

  *soap_minimum_interval*
    UPnP SOAP requests that start a track or switch to the next track are spread
    out at ``soap_minimum_interval`` seconds to avoid the problem described at
    `issue #16`_. This applies only to the SOAP requests that change tracks:
    SetAVTransportURI and SetNextAVTransportURI.

    The default is 5 seconds.

Encoder specific options
------------------------

Encoder specific options (for example ``bitrate``) are listed in
:ref:`default_config` with their default value. They are used to build the
encoder command line when ``args`` is None.

.. _INI file: https://en.wikipedia.org/wiki/INI_file
.. _configparser:
        https://docs.python.org/3/library/configparser.html#supported-ini-file-structure
.. _RFC 2586:
    https://datatracker.ietf.org/doc/html/rfc2586
.. _sample formats:
    https://www.freedesktop.org/wiki/Software/PulseAudio/Documentation/User/SupportedAudioFormats/
.. _issue #16: https://gitlab.com/xdegaye/pa-dlna/-/issues/16

.. rubric:: Footnotes

.. [#] The default configuration is printed by the command: ```$ pa-dlna
       --dump-default```
.. [#] The ``GetProtocolInfo`` command in the ``ConnectionManager`` service menu
       of the ``upnp-cmd`` command line tool prints this same list.
.. [#] The ``SetNextAVTransportURI`` is used when the ``track_metadata`` option
       is set.
.. [#] Simple Object Access Protocol. A remote-procedure call mechanism based on
       XML that sends commands and receives values over HTTP.
.. [#] Except when the audio/L16 mime type is selected.
.. [#] Note that the ``;`` character must be escaped on the command line or the
       value of the ``--test-devices`` option must be quoted.
.. [#] DLNATest device sink names and URLs are built using the sha1 of the audio
       mime type and therefore are consistent across ``pa-dlna`` sessions.
.. [#] UDN: Unique Device Name. Universally-unique identifier of an UPnP device.
