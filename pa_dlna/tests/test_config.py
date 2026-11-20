"""Encoders configuration test cases."""

import io
from unittest import mock
from contextlib import redirect_stdout
from configparser import ParsingError

# Load the tests in the order they are declared.
from . import load_ordered_tests as load_tests

from . import LoggingTestCase, requires_resources
from ..config import UserConfig
from ..encoders import select_encoder

UDN = 'uuid:ffffffff-ffff-ffff-ffff-ffffffffffff'

class Encoder:
    def __init__(self):
        self.selection = ['SomeEncoder']
        self.args = None
        self.option = 1

    @property
    def available(self):
        if hasattr(self, '_available'):
            return self._available
        return True

    def set_args(self):
        raise NotImplementedError

class StandAloneEncoder(Encoder):
    def __init__(self):
        super().__init__()

class SomeEncoder(StandAloneEncoder):
    def __init__(self):
        StandAloneEncoder.__init__(self)

    def set_args(self):
        self.args = f'command line: {self.option}'

class encoders_module:
    def __init__(self, root=Encoder, encoder=SomeEncoder):
        self.ROOT_ENCODER = root
        self.SomeEncoder = encoder

@requires_resources('os.devnull')
class DefaultConfig(LoggingTestCase):
    """Default configuration tests."""

    def test_invalid_section(self):
        name = 'InvalidEncoder'
        class _Encoder(Encoder):
            def __init__(self):
                super().__init__()
                self.selection = [name]

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module(root=_Encoder)),\
                self.assertRaises(ParsingError) as cm:
            from ..config import DefaultConfig
            DefaultConfig()

        self.assertEqual(cm.exception.args[0],
                         f"'{name}' is not a valid class name")

@requires_resources(('os.devnull', 'ffmpeg'))
class UserConfigTests(LoggingTestCase):
    """User configuration tests."""

    def test_invalid_value(self):
        value = 'string'
        pa_dlna_conf = f"""
        [SomeEncoder]
          option = {value}
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertRegex(cm.exception.args[0],
                         f"SomeEncoder.option: invalid .*'{value}'")

    def test_option_negative_value(self):
        value = -1
        pa_dlna_conf = f"""
        [SomeEncoder]
          option = {value}
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertRegex(cm.exception.args[0],
                         f'SomeEncoder.option: {value} is negative')

    def test_invalid_option(self):
        pa_dlna_conf = """
        [SomeEncoder]
          invalid = 1
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertEqual(cm.exception.args[0],
                         "Unknown option 'SomeEncoder.invalid'")

    def test_default_conf(self):
        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open()) as m_open:
            m_open.side_effect = FileNotFoundError()
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['SomeEncoder'].__dict__,
                         {'args': 'command line: 1', 'option': 1})

    def test_user_conf(self):
        pa_dlna_conf = """
        [SomeEncoder]
          option = 2
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['SomeEncoder'].__dict__,
                         {'args': 'command line: 2', 'option': 2})

    def test_customize_args_option(self):
        pa_dlna_conf = """
        [FFMpegMp3Encoder]
          bitrate = 320
          args = foo
        """

        with mock.patch('builtins.open', mock.mock_open(
                                                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['FFMpegMp3Encoder'].args, 'foo')

    def test_command_qscale(self):
        pa_dlna_conf = """
        [FFMpegMp3Encoder]
          bitrate = 0
          qscale = 2
        """

        with mock.patch('builtins.open', mock.mock_open(
                                                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        arg = '-qscale:a'
        command  = cfg.encoders['FFMpegMp3Encoder'].command
        self.assertTrue(arg in command)
        index = command.index(arg)
        self.assertEqual(command[index+1], '2')

    def test_default_sample_formats(self):
        configs = (
            ('FFMpegMp3Encoder', 's16le'),
            ('FFMpegL16WavEncoder', 's16be'),
            ('L16Encoder', 's16be'),
            )

        for encoder, format in configs:
            pa_dlna_conf = f'[{encoder}]'
            with self.subTest(pa_dlna_conf=pa_dlna_conf, format=format),\
                    mock.patch('builtins.open', mock.mock_open(
                                                    read_data=pa_dlna_conf)):
                cfg = UserConfig()
                self.assertEqual(cfg.encoders[encoder].sample_format, format)

    def test_mp3_sample_format(self):
        pa_dlna_conf = """
        [FFMpegMp3Encoder]
          sample_format = s32le
        """

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['FFMpegMp3Encoder'].sample_format,
                         's32le')

    def test_l16_sample_format(self):
        pa_dlna_conf = """
        [L16Encoder]
        """

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertEqual(cfg.encoders['L16Encoder'].sample_format, 's16be')

    def test_l16_udn_sample_format(self):
        pa_dlna_conf = """
        [L16Encoder.uuid:9ab0c000]
        """

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertEqual(cfg.udns['uuid:9ab0c000'].sample_format, 's16be')

    def test_not_available(self):
        class UnAvailableEncoder(StandAloneEncoder):
            def __init__(self):
                super().__init__()
                self._available = False

            def set_args(self):
                pass

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module(encoder=UnAvailableEncoder)),\
                mock.patch('builtins.open', mock.mock_open()) as m_open,\
                redirect_stdout(io.StringIO()) as output:
            m_open.side_effect = FileNotFoundError()
            cfg = UserConfig()
            cfg.print_internal_config()

        self.assertEqual(cfg.encoders, {})
        self.assertIn('No encoder is available\n', output.getvalue())

    def test_invalid_section(self):
        pa_dlna_conf = """
        [SomeEncoder.]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertEqual(cm.exception.args[0],
                         "'SomeEncoder.' is not a valid section")

    def test_not_exists(self):
        pa_dlna_conf = """
        [DEFAULT]
          selection = UnknownEncoder
        [UnknownEncoder]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertEqual(cm.exception.args[0]
                         , "'UnknownEncoder' encoder does not exist")

    def test_invalid_encoder(self):
        pa_dlna_conf = """
        [DEFAULT]
          selection = UnknownEncoder
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                self.assertRaises(ParsingError) as cm:
            UserConfig()

        self.assertEqual(cm.exception.args[0], "'UnknownEncoder' in the"
                         ' selection is not a valid encoder')

    def test_udn_section(self):
        pa_dlna_conf = f"""
        [SomeEncoder.{UDN}]
        """

        with mock.patch('pa_dlna.config.encoders_module',
                        new=encoders_module()),\
                mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)),\
                redirect_stdout(io.StringIO()) as output:
            UserConfig().print_internal_config()

        self.assertIn(f"{{'{UDN}': {{'_encoder': 'SomeEncoder'",
                      output.getvalue())

    def test_update_args_option(self):
        pa_dlna_conf = """
        [DEFAULT]
        selection =
            FFMpegMp3Encoder,

        [FFMpegMp3Encoder]
        bitrate = 320
        """

        with mock.patch('builtins.open', mock.mock_open(
                                                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertIn('-b:a 320k', cfg.encoders['FFMpegMp3Encoder'].args)

    def test_udn_update_args_option(self):
        pa_dlna_conf = f"""
        [FFMpegMp3Encoder.{UDN}]
        bitrate = 320
        """

        with mock.patch('builtins.open', mock.mock_open(
                                                    read_data=pa_dlna_conf)):
            cfg = UserConfig()

        self.assertIn('-b:a 320k', cfg.udns[UDN].args)

@requires_resources('os.devnull')
class Encoders(LoggingTestCase):
    """Encoders tests."""

    def l16_mime_type(self, mime_type, rate=0, channels=0, udn=None):

        pinfo = {'Sink': f'http-get:*:{mime_type}:DLNA.ORG_PN=LPCM'}
        config = UserConfig()

        if udn is not None:
            self.assertEqual(config.encoders, {})
            self.assertIn(udn, config.udns)
        else:
            # Set the attributes of the L16Encoder instance.
            l16 = config.encoders['L16Encoder']
            l16.rate = rate
            l16.channels = channels

        res = select_encoder(config, 'Renderer name', pinfo, udn)

        if res is not None:
            encoder, mtype, protocol_info = res
            self.assertEqual(encoder.__class__.__name__, 'L16Encoder')
            self.assertEqual(mtype, mime_type)
            self.assertEqual(protocol_info,
                             f'http-get:*:{mime_type}:DLNA.ORG_PN=LPCM')

        return res

    def test_select_L16(self):
        rate_channels = [(44100, 2),
                         (44100, 1),
                         (88200, 2)]
        mime_types = ['audio/L16;channels={channels};rate={rate}',
                      'audio/l16;rate={rate};channels={channels}']
        for rate, channels in rate_channels:
            for mtype in mime_types:
                mtype = mtype.format(rate=rate, channels=channels)
                with self.subTest(mtype=mtype),\
                     mock.patch('builtins.open', mock.mock_open()) as m_open:
                    m_open.side_effect = FileNotFoundError()
                    self.l16_mime_type(mtype, rate, channels)

    def test_select_udn(self):
        pa_dlna_conf = f"""
        [DEFAULT]
          selection =
        [L16Encoder.{UDN}]
        """

        with mock.patch('builtins.open', mock.mock_open(
                    read_data=pa_dlna_conf)):
            res = self.l16_mime_type('audio/L16;channels=2;rate=44100',
                                     udn=UDN)
            self.assertNotEqual(res, None)

    def test_bad_mtype(self):
        pa_dlna_conf = f"""
        [DEFAULT]
          selection =
        [L16Encoder.{UDN}]
        """

        mime_types = [
            'audio/L16;channels=2;rate=88200',  # 88200 is invalid param
            'audio/FOO;channels=2;rate=44100',  # not L16 mime type
            'audio/L16;channels=2;rate=FOO'     # wrong param value
        ]

        for mtype in mime_types:
            with self.subTest(mtype=mtype),\
                    mock.patch('builtins.open', mock.mock_open(
                        read_data=pa_dlna_conf)):
                res = self.l16_mime_type(mtype, udn=UDN)
                self.assertEqual(res, None)

if __name__ == '__main__':
    unittest.main(verbosity=2)
